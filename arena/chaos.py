"""arena chaos: drain a queue while killing workers and freezing Postgres, then check it.

Like `arena bench` this runs in its own database, `arena_bench`, and drops it afterwards.
Workers die by kill (SIGKILL; TerminateProcess on Windows), with no chance to clean up, and
are replaced straight away. Others get a graceful stop (SIGTERM, or Ctrl-Break on Windows,
where an external SIGTERM is a hard kill) and must exit cleanly. Postgres is frozen with
`docker pause`, usually past the short
lease used here, so a stalled worker comes back to find its match taken and its writes
fenced. A worker that dies by itself is counted and replaced too.

Bot turns are free, which would make the spend check trivial, so workers here seat PricedBot:
the same bot reporting made-up tokens at a made-up price. That money runs through the normal
fenced write path, and nothing calls a provider.

`check_invariants` is the point of the run. It reads only what the database kept:
- every match completed, once: final state, and one rating row per seat, no more, no fewer;
- every turn log continuous from 0, with no duplicates;
- every log replays through the engine to the stored scores;
- no player's games_played disagrees with the matches they sat in;
- tournament spend equals the sum of turn costs, and each spend shard equals what its
  worker slot's turns cost.

If this process dies while Postgres is frozen, run `docker unpause gauntlet-db-1`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg

from arena.bench import (
    DSN_ENV,
    BenchError,
    bench_dsn,
    bench_roster,
    drop_database,
    queue_matches,
    reset_database,
)
from arena.costs import Pricing
from arena.engine.match_runner import CorruptTurnLog, MatchSpec, iter_states
from arena.games.registry import get_game
from arena.players.base import Decision, Player, TurnContext
from arena.roster import PlayerFactory
from arena.store.db import create_pool
from arena.store.spend import EXACT, TOTAL_SPEND_SQL
from arena.store.turns import PostgresTurnStore
from arena.types import Usage
from arena.worker import WorkerConfig, install_stop_signals, run_worker

# Made-up, like the usage below: $0.0015 a turn, so spend is exact in NUMERIC(10, 6).
CHAOS_PRICE = Pricing(1.0, 5.0)
CHAOS_USAGE = Usage(input_tokens=1000, output_tokens=100)

# Short, so a killed worker's match is reclaimed quickly and a pause outlives the lease.
CHAOS_WORKER = WorkerConfig(
    lease_seconds=4.0, heartbeat_seconds=1.0, max_run_attempts=50, idle_sleep_seconds=0.25
)
COMPOSE_SERVICES = ("db", "testdb")
STOP_SECONDS = 15.0  # time for a graceful stop: one turn and a release
REPO_ROOT = Path(__file__).resolve().parent.parent


class PricedBot:
    """A bot that bills, so spend accounting has something to get wrong."""

    def __init__(self, inner: Player):
        self.player_id = inner.player_id
        self._inner = inner

    async def act(self, ctx: TurnContext) -> Decision:
        return replace(await self._inner.act(ctx), usage=CHAOS_USAGE)


class ChaosFactory(PlayerFactory):
    def build(self, player_id: str) -> tuple[Player, Pricing]:
        player, _ = super().build(player_id)
        return PricedBot(player), CHAOS_PRICE


@dataclass(frozen=True)
class ChaosReport:
    matches: int
    turns: int
    kills: int
    pauses: int
    paused_seconds: float
    stops: int  # graceful stops, each of which exited cleanly or is a violation
    worker_deaths: int  # workers that died without being killed
    reclaimed: int  # claims beyond each match's first, refunded by a graceful stop
    handed_over: int  # matches played by more than one worker
    spend_usd: float
    violations: list[str]
    spend_mode: str = EXACT


async def _run(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=REPO_ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode:
        raise BenchError(f"{' '.join(args)}: {err.decode().strip()}")
    return out.decode().strip()


async def find_container(dsn: str) -> str:
    """The compose Postgres this DSN reaches. Nothing else may ever be paused, so a server
    that isn't one of this repo's two services is an error."""
    port = str(urlsplit(dsn).port)
    for service in COMPOSE_SERVICES:
        container = await _run("docker", "compose", "ps", "-q", service)
        if container and (await _run("docker", "port", container, "5432")).endswith(f":{port}"):
            return container
    raise BenchError(f"no running compose service {COMPOSE_SERVICES} publishes port {port}")


async def _pause(container: str, seconds: float) -> None:
    await _run("docker", "pause", container)
    try:
        await asyncio.sleep(seconds)
    finally:
        await _run("docker", "unpause", container)


async def _spawn(dsn: str, players: int) -> asyncio.subprocess.Process:
    """Start a worker, returning once it can take a graceful stop: before its signal
    handlers exist, Ctrl-Break would simply end it."""
    # stderr stays attached: lost leases and fenced writes are worth seeing.
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "arena.chaos",
        "--child",
        str(players),
        stdout=asyncio.subprocess.PIPE,
        env=os.environ | {DSN_ENV: dsn},
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    assert proc.stdout is not None  # started with stdout=PIPE
    if (await proc.stdout.readline()).strip() != b"ready":
        raise BenchError("a worker process died during start-up")
    return proc


async def _stop(proc: asyncio.subprocess.Process) -> bool:
    """Ask a worker to stop. True if it exited cleanly in time."""
    proc.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
    try:
        return await asyncio.wait_for(proc.wait(), STOP_SECONDS) == 0
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return False


async def check_invariants(pool: asyncpg.Pool, expected_matches: int) -> list[str]:
    """Every way the database disagrees with itself. Empty means the run held."""
    bad: list[str] = []
    rows = await pool.fetch(
        "SELECT id, game, seed, options, status, scores, tournament_id, lease_owner FROM matches"
    )
    if len(rows) != expected_matches:
        bad.append(f"{len(rows)} matches, expected {expected_matches}")

    seats: dict[int, list[str]] = {}
    for r in await pool.fetch(
        "SELECT match_id, player_id FROM match_seats ORDER BY match_id, seat"
    ):
        seats.setdefault(r["match_id"], []).append(r["player_id"])

    rated = {
        (r["match_id"], r["player_id"]): r["n"]
        for r in await pool.fetch(
            "SELECT match_id, player_id, count(*) AS n FROM rating_history "
            "GROUP BY match_id, player_id"
        )
    }
    store = PostgresTurnStore(pool, "chaos-check")
    for r in rows:
        match = r["id"]
        if r["status"] != "completed":
            bad.append(f"match {match} is {r['status']}")
            continue
        if r["lease_owner"] is not None:
            bad.append(f"match {match} completed with a lease still held")
        for player in seats[match]:
            if rated.get((match, player), 0) != 1:
                count = rated.get((match, player), 0)
                bad.append(f"match {match} rated {count} times for {player}")
        turns = await store.load_turns(str(match))
        numbers = [t.turn_no for t in turns]
        if numbers != list(range(len(numbers))):
            bad.append(f"match {match} turn log isn't continuous: {numbers}")
            continue
        game = get_game(r["game"])
        spec = MatchSpec(str(match), r["game"], r["seed"], seats[match], r["options"])
        try:
            *_, final = iter_states(game, spec, turns)
        except CorruptTurnLog as e:
            bad.append(f"match {match} doesn't replay: {e}")
            continue
        if not game.is_terminal(final):
            bad.append(f"match {match} completed but its log stops before the end")
        elif game.scores(final) != r["scores"]:
            bad.append(f"match {match} replays to {game.scores(final)}, stored {r['scores']}")

    extra = set(rated) - {(m, p) for m, players in seats.items() for p in players}
    if extra:
        bad.append(f"rating rows for seats that don't exist: {sorted(extra)[:5]}")

    for r in await pool.fetch(
        """
        SELECT s.player_id, count(*) AS seats, r.games_played
        FROM match_seats s JOIN matches m ON m.id = s.match_id AND m.status = 'completed'
        LEFT JOIN ratings r ON r.player_id = s.player_id AND r.game = m.game
        GROUP BY s.player_id, r.games_played
        """
    ):
        if r["seats"] != r["games_played"]:
            bad.append(f"{r['player_id']}: {r['seats']} seats, games_played {r['games_played']}")

    for r in await pool.fetch(
        f"""
        SELECT t.id, t.status, {TOTAL_SPEND_SQL} AS spent_usd,
               COALESCE(sum(u.cost_usd), 0) AS turn_costs
        FROM tournaments t
        LEFT JOIN matches m ON m.tournament_id = t.id
        LEFT JOIN turns u ON u.match_id = m.id
        GROUP BY t.id
        """
    ):
        if r["spent_usd"] != r["turn_costs"]:
            bad.append(f"tournament {r['id']} spent {r['spent_usd']}, turns cost {r['turn_costs']}")
        if r["status"] != "completed":
            bad.append(f"tournament {r['id']} is {r['status']}")

    for r in await pool.fetch(
        """
        SELECT s.worker_id, s.spent_usd, t.spend_mode, COALESCE(sum(u.cost_usd), 0) AS turn_costs
        FROM tournament_spend_shards s
        JOIN tournaments t ON t.id = s.tournament_id
        LEFT JOIN matches m ON m.tournament_id = s.tournament_id
        LEFT JOIN turns u ON u.match_id = m.id AND u.worker_id = s.worker_id
        GROUP BY s.tournament_id, s.worker_id, s.spent_usd, t.spend_mode
        """
    ):
        if r["spend_mode"] != "sharded":
            bad.append(f"{r['spend_mode']} tournament has a spend shard for {r['worker_id']}")
        elif r["spent_usd"] != r["turn_costs"]:
            bad.append(
                f"shard {r['worker_id']} holds {r['spent_usd']}, its turns cost {r['turn_costs']}"
            )
    return bad


async def _unfinished(conn: asyncpg.Connection) -> tuple[int, int]:
    row = await conn.fetchrow(
        "SELECT count(*) FILTER (WHERE status IN ('queued', 'running')) AS open, "
        "count(*) FILTER (WHERE status = 'failed') AS failed FROM matches"
    )
    return row["open"], row["failed"]


async def run_chaos(
    admin_dsn: str,
    matches: int = 2000,
    workers: int = 4,
    players: int = 8,
    seed: int = 0,
    mean_gap: float = 2.0,
    pause_chance: float = 0.2,
    stop_chance: float = 0.2,
    pause_seconds: tuple[float, float] = (2.0, 6.0),
    time_limit: float = 1800.0,
    spend_mode: str = EXACT,
) -> ChaosReport:
    rng = random.Random(seed)
    dsn = bench_dsn(admin_dsn)
    container = await find_container(admin_dsn)
    await reset_database(admin_dsn)
    procs: list[asyncio.subprocess.Process] = []
    conn: asyncpg.Connection | None = None
    kills = pauses = stops = bad_stops = deaths = 0
    paused = 0.0
    try:
        await queue_matches(dsn, matches, players, spend_mode)
        procs = [await _spawn(dsn, players) for _ in range(workers)]
        conn = await asyncpg.connect(dsn)
        deadline = time.monotonic() + time_limit
        while True:
            still_open, failed = await _unfinished(conn)
            if not still_open or failed:
                break
            if time.monotonic() > deadline:
                raise BenchError(f"{still_open} matches unfinished after {time_limit:.0f}s")
            await asyncio.sleep(rng.expovariate(1 / mean_gap))
            for i, proc in enumerate(procs):
                if proc.returncode is not None:
                    deaths += 1
                    procs[i] = await _spawn(dsn, players)
            roll = rng.random()
            if roll < pause_chance:
                seconds = rng.uniform(*pause_seconds)
                await _pause(container, seconds)
                pauses += 1
                paused += seconds
                continue
            i = rng.randrange(len(procs))
            if roll < pause_chance + stop_chance:
                stops += 1
                bad_stops += not await _stop(procs[i])
            else:
                procs[i].kill()
                await procs[i].wait()
                kills += 1
            procs[i] = await _spawn(dsn, players)
    finally:
        for proc in procs:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        if conn is not None:
            await conn.close()

    pool = await create_pool(dsn, max_size=4)
    try:
        violations = await check_invariants(pool, matches)
        totals = await pool.fetchrow(
            "SELECT (SELECT count(*) FROM turns) AS turns, "
            f"(SELECT COALESCE(sum({TOTAL_SPEND_SQL}), 0) FROM tournaments t) AS spend, "
            "(SELECT COALESCE(sum(run_attempts - 1), 0) FROM matches) AS reclaimed, "
            "(SELECT count(*) FROM (SELECT match_id FROM turns GROUP BY match_id "
            "HAVING count(DISTINCT worker_id) > 1) AS x) AS handed_over"
        )
    finally:
        await pool.close()
        await drop_database(admin_dsn)
    if bad_stops:
        violations.append(f"{bad_stops} of {stops} graceful stops didn't exit cleanly")
    if not kills + pauses + stops:
        violations.append("nothing tested: no kills, pauses or stops")
    return ChaosReport(
        matches=matches,
        turns=totals["turns"],
        kills=kills,
        pauses=pauses,
        paused_seconds=paused,
        stops=stops,
        worker_deaths=deaths,
        reclaimed=totals["reclaimed"],
        handed_over=totals["handed_over"],
        spend_usd=float(totals["spend"]),
        violations=violations,
        spend_mode=spend_mode,
    )


def format_report(r: ChaosReport) -> str:
    verdict = "all invariants held" if not r.violations else f"{len(r.violations)} VIOLATIONS"
    lines = [
        f"{r.matches:,} matches, {r.turns:,} turns, ${r.spend_usd:.4f} of made-up spend "
        f"({r.spend_mode})",
        f"{r.kills} workers killed, {r.pauses} Postgres pauses ({r.paused_seconds:.1f}s), "
        f"{r.stops} graceful stops, {r.worker_deaths} other worker deaths",
        f"{r.reclaimed} extra claims, {r.handed_over} matches played by more than one worker",
        verdict,
    ]
    return "\n".join(lines + r.violations[:50])


async def _child(players: int) -> None:
    pool = await create_pool(os.environ[DSN_ENV], max_size=3)
    stop = asyncio.Event()
    install_stop_signals(stop)
    print("ready", flush=True)
    await run_worker(pool, ChaosFactory(bench_roster(players), {}), CHAOS_WORKER, stop=stop)
    await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", type=int, metavar="PLAYERS", required=True)
    asyncio.run(_child(parser.parse_args().child))
