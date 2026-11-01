"""Worker: claims matches from Postgres, runs them, heartbeats the lease.

Failure handling:
- Lease lost (heartbeat fails, or a fenced turn write is refused): stop at once and
  write nothing. Another worker owns the match and resumes from the turn log.
- ProviderError (API down / rate limited past retries) or an unexpected bug: release
  the match for retry with exponential backoff; after N run attempts it is marked
  'failed' with last_error set.
- Worker killed (SIGKILL, crash, laptop lid): nothing to do. The lease expires and
  another worker re-claims the match.
- Graceful stop (SIGTERM / first Ctrl-C, see install_stop_signals): stop claiming, let
  each in-flight turn finish and persist, then hand the match back (claimable at once,
  run attempt refunded) and exit. A second signal gets the default behaviour.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import asyncpg

from arena.engine.match_runner import LeaseLost, MatchInterrupted, MatchRunner, MatchSpec
from arena.games.registry import get_game
from arena.providers.base import ProviderError
from arena.roster import PlayerFactory
from arena.store import queue
from arena.store.turns import PostgresTurnStore
from arena.telemetry import (
    MATCHES_CLAIMED,
    MATCHES_COMPLETED,
    MATCHES_FAILED,
    TracedPlayer,
    tracer,
)

log = logging.getLogger("arena.worker")


@dataclass(frozen=True)
class WorkerConfig:
    lease_seconds: float = 60.0
    heartbeat_seconds: float = 20.0
    max_run_attempts: int = 5
    idle_sleep_seconds: float = 2.0


def new_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


async def run_one(
    pool: asyncpg.Pool,
    factory: PlayerFactory,
    worker_id: str,
    cfg: WorkerConfig,
    stop: asyncio.Event | None = None,
    on_claim: Callable[[float], None] | None = None,
) -> bool:
    """Claim and run one match. Returns False if there was nothing to claim. `on_claim`
    gets the seconds each successful claim took."""
    started = time.perf_counter()
    claimed = await queue.claim(pool, worker_id, cfg.lease_seconds)
    if claimed is None:
        return False
    if on_claim is not None:
        on_claim(time.perf_counter() - started)
    MATCHES_CLAIMED.inc()
    log.info("claimed match %s (attempt %s)", claimed.id, claimed.run_attempts)
    attributes = {
        "arena.match": claimed.id,
        "arena.game": claimed.game,
        "arena.run_attempt": claimed.run_attempts,
    }
    # The run task copies this context, so turns and provider calls nest under the match.
    with tracer.start_as_current_span("match", attributes=attributes):
        await _run_claimed(pool, factory, worker_id, cfg, claimed, stop)
    return True


async def _run_claimed(
    pool: asyncpg.Pool,
    factory: PlayerFactory,
    worker_id: str,
    cfg: WorkerConfig,
    claimed: queue.ClaimedMatch,
    stop: asyncio.Event | None,
) -> None:
    try:
        game = get_game(claimed.game)
        built = [factory.build(pid) for pid in claimed.seats]
        runner = MatchRunner(
            game,
            [TracedPlayer(p, claimed.game) for p, _ in built],
            PostgresTurnStore(pool, worker_id),
            pricing=[price for _, price in built],
        )
        spec = MatchSpec(
            str(claimed.id), claimed.game, claimed.seed, claimed.seats, claimed.options
        )
    except Exception as e:  # bad config: retrying won't help, but keep the record
        log.exception("could not set up match %s", claimed.id)
        status = await queue.release_for_retry(
            pool, claimed.id, worker_id, f"setup: {e}", max_attempts=1, backoff_seconds=0
        )
        if status == "failed":
            MATCHES_FAILED.inc()
        return

    run_task = asyncio.create_task(runner.run(spec, stop))
    hb_task = asyncio.create_task(_heartbeat_until_lost(pool, claimed.id, worker_id, cfg))
    try:
        done, _ = await asyncio.wait({run_task, hb_task}, return_when=asyncio.FIRST_COMPLETED)
        if hb_task in done:  # lease lost while running
            run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, LeaseLost):
                await run_task
            log.warning("lost lease on match %s; abandoning", claimed.id)
            return

        outcome = run_task.result()  # raises if the match run failed
        if not await queue.complete(
            pool, claimed.id, worker_id, outcome.scores, outcome.placements
        ):
            log.warning("match %s finished but lease was gone; result not recorded", claimed.id)
        else:
            MATCHES_COMPLETED.inc()
            log.info(
                "match %s done: scores=%s turns=%s cost=$%.4f",
                claimed.id,
                outcome.scores,
                outcome.num_turns,
                outcome.cost_usd,
            )
    except LeaseLost:
        log.warning("turn write fenced off for match %s; abandoning", claimed.id)
    except MatchInterrupted:
        if await queue.release_on_shutdown(pool, claimed.id, worker_id):
            log.info("match %s handed back for shutdown", claimed.id)
    except ProviderError as e:
        await _release(pool, claimed, worker_id, f"provider: {e}", cfg)
    except Exception as e:
        log.exception("match %s crashed", claimed.id)
        await _release(pool, claimed, worker_id, f"{type(e).__name__}: {e}", cfg)
    finally:
        hb_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hb_task


async def _heartbeat_until_lost(
    pool: asyncpg.Pool, match_id: int, worker_id: str, cfg: WorkerConfig
) -> None:
    while True:
        await asyncio.sleep(cfg.heartbeat_seconds)
        if not await queue.heartbeat(pool, match_id, worker_id, cfg.lease_seconds):
            return


async def _release(
    pool: asyncpg.Pool, claimed: queue.ClaimedMatch, worker_id: str, error: str, cfg: WorkerConfig
) -> None:
    backoff = min(300.0, 10.0 * 2 ** (claimed.run_attempts - 1))
    status = await queue.release_for_retry(
        pool,
        claimed.id,
        worker_id,
        error,
        max_attempts=cfg.max_run_attempts,
        backoff_seconds=backoff,
    )
    if status == "failed":
        MATCHES_FAILED.inc()
    log.warning("match %s released (%s): %s", claimed.id, status, error)


async def run_worker(
    pool: asyncpg.Pool,
    factory: PlayerFactory,
    cfg: WorkerConfig,
    *,
    concurrency: int = 1,
    stop: asyncio.Event | None = None,
    exit_when_idle: bool = False,
    on_claim: Callable[[float], None] | None = None,
) -> None:
    """Run `concurrency` claim loops in this process until `stop` is set."""
    stop = stop or asyncio.Event()
    worker_id = new_worker_id()

    async def loop(slot: int) -> None:
        wid = f"{worker_id}/{slot}"
        while not stop.is_set():
            if not await run_one(pool, factory, wid, cfg, stop, on_claim):
                if exit_when_idle:
                    return
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), cfg.idle_sleep_seconds)

    await asyncio.gather(*(loop(i) for i in range(concurrency)))


def install_stop_signals(stop: asyncio.Event) -> Callable[[], None]:
    """Set `stop` on the first SIGTERM, SIGINT or SIGBREAK, then put the previous handlers back so
    a second signal stops the process the usual way. Returns a function that restores
    the previous handlers. Call from the event loop thread.

    Uses signal.signal rather than loop.add_signal_handler, which Windows lacks.
    """
    loop = asyncio.get_running_loop()
    sigs = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):  # Windows: the only stop another process can send
        sigs.append(signal.SIGBREAK)
    previous = {s: signal.getsignal(s) for s in sigs}

    def restore() -> None:
        for s, handler in previous.items():
            if handler is not None:
                signal.signal(s, handler)

    def handle(signum: int, _frame: object) -> None:
        log.warning(
            "%s: finishing in-flight turns, then exiting (signal again to force)",
            signal.Signals(signum).name,
        )
        restore()
        loop.call_soon_threadsafe(stop.set)

    for s in sigs:
        signal.signal(s, handle)
    return restore
