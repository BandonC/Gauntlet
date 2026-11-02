"""arena bench: queue N bot matches, run M worker processes, report what they managed.

Every run gets a fresh database, `arena_bench`, on the server DATABASE_URL points at, and
drops it afterwards, so real tournaments and ratings are never touched. Bots only: no run
here can spend money.

The workers are real `run_worker` processes doing real leases, fenced turn writes and
rating transactions. They start, connect and wait, so interpreter start-up isn't timed.
Timing is on Postgres's clock, from the first claim to the last completion.

Queue wait is time from "go" to a match's claim. Everything is queued up front, so it
says how long the backlog took to drain, not how a steady arrival rate would feel.
Claim latency is `queue.claim` as seen by a worker, successful claims only.
Connections is the most the workers held at once, sampled four times a second.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from arena.roster import PlayerConfig, PlayerFactory, Roster
from arena.store.db import create_pool
from arena.store.migrate import migrate
from arena.store.spend import EXACT
from arena.store.tournaments import create_round_robin
from arena.worker import WorkerConfig, run_worker

BENCH_DB = "arena_bench"
GAME = "liars_dice"
DSN_ENV = "ARENA_BENCH_DSN"
SAMPLE_SECONDS = 0.25
# Never reached, so runs drain, but set so sharded mode pays for its budget checks.
BENCH_BUDGET_USD = 100_000.0


class BenchError(RuntimeError):
    """The run didn't produce a result worth reporting."""


@dataclass(frozen=True)
class BenchResult:
    workers: int
    matches: int
    turns: int
    elapsed_s: float
    queue_wait_s: tuple[float, float, float]  # p50, p95, p99
    claim_ms: tuple[float, float, float]  # p50, p95, p99
    max_connections: int
    reruns: int  # claims beyond the first, 0 in a clean run
    deadlocks: int
    spend_mode: str = EXACT

    @property
    def matches_per_s(self) -> float:
        return self.matches / self.elapsed_s

    @property
    def turns_per_s(self) -> float:
        return self.turns / self.elapsed_s


def bench_dsn(dsn: str) -> str:
    """The same server as `dsn`, database arena_bench."""
    parts = urlsplit(dsn)
    if parts.path.lstrip("/") == BENCH_DB:
        raise BenchError(f"DATABASE_URL already names {BENCH_DB}, which bench drops")
    return urlunsplit(parts._replace(path=f"/{BENCH_DB}"))


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest rank."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def percentiles(values: Sequence[float]) -> tuple[float, float, float]:
    return percentile(values, 0.50), percentile(values, 0.95), percentile(values, 0.99)


def bench_roster(players: int) -> Roster:
    configs = [
        PlayerConfig(id=f"bench-bot-{i}", provider="bot", model=GAME, seed=i)
        for i in range(players)
    ]
    return Roster(players={c.id: c for c in configs}, limits={})


async def drop_database(admin_dsn: str) -> None:
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f"DROP DATABASE IF EXISTS {BENCH_DB} WITH (FORCE)")
    finally:
        await conn.close()


async def reset_database(admin_dsn: str) -> None:
    await drop_database(admin_dsn)
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f"CREATE DATABASE {BENCH_DB}")
    finally:
        await conn.close()
    await migrate(bench_dsn(admin_dsn))


async def queue_matches(dsn: str, matches: int, players: int, spend_mode: str = EXACT) -> None:
    """Exactly `matches` matches across a round-robin of `players` bots. Same seed every
    run, so each worker count plays the same games. The player pool matters: every
    completion locks both players' rating rows, so a smaller pool means more contention."""
    if players < 2:
        raise BenchError("bench needs at least 2 players")
    roster = bench_roster(players)
    pairs = players * (players - 1) // 2
    pool = await create_pool(dsn, max_size=2)
    try:
        tid, _ = await create_round_robin(
            pool,
            GAME,
            list(roster.players.values()),
            math.ceil(matches / pairs),
            BENCH_BUDGET_USD,
            seed=0,
            spend_mode=spend_mode,
        )
        await pool.execute(
            "DELETE FROM matches WHERE tournament_id = $1 AND id NOT IN "
            "(SELECT id FROM matches WHERE tournament_id = $1 ORDER BY id LIMIT $2)",
            tid,
            matches,
        )
    finally:
        await pool.close()


async def spawn_workers(
    count: int, players: int, dsn: str, out_dir: Path
) -> list[asyncio.subprocess.Process]:
    """Start `count` worker processes and return once each has connected. They run
    nothing until told to go (a line on stdin)."""
    procs = []
    for i in range(count):
        procs.append(
            await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "arena.bench",
                "--child",
                str(players),
                str(out_dir / f"claims-{i}.json"),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                env=os.environ | {DSN_ENV: dsn},
            )
        )
    for proc in procs:
        if (await proc.stdout.readline()).strip() != b"ready":
            raise BenchError("a worker process died during start-up")
    return procs


def _read_claims(out_dir: Path) -> list[float]:
    return [
        seconds
        for path in sorted(out_dir.glob("claims-*.json"))
        for seconds in json.loads(path.read_text())
    ]


async def _clock(dsn: str) -> datetime:
    """Postgres's time, so queue waits compare with started_at. The connection is gone
    before the workers start, so the sampler counts only theirs."""
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchval("SELECT clock_timestamp()")
    finally:
        await conn.close()


async def _sample_connections(dsn: str, peak: list[int]) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        while True:
            count = await conn.fetchval(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
            peak[0] = max(peak[0], count)
            await asyncio.sleep(SAMPLE_SECONDS)
    finally:
        await conn.close()


async def run_once(
    admin_dsn: str, matches: int, workers: int, players: int, spend_mode: str = EXACT
) -> BenchResult:
    dsn = bench_dsn(admin_dsn)
    await reset_database(admin_dsn)
    await queue_matches(dsn, matches, players, spend_mode)

    with tempfile.TemporaryDirectory(prefix="arena-bench-") as tmp:
        out_dir = Path(tmp)
        procs = await spawn_workers(workers, players, dsn, out_dir)
        peak = [0]
        sampler = asyncio.create_task(_sample_connections(dsn, peak))
        try:
            go = await _clock(dsn)
            for proc in procs:
                proc.stdin.write(b"go\n")
                await proc.stdin.drain()
            codes = await asyncio.gather(*(proc.wait() for proc in procs))
        finally:
            sampler.cancel()
            await asyncio.gather(sampler, return_exceptions=True)
            for proc in procs:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
        if any(codes):
            raise BenchError(f"worker exit codes: {codes}")
        claims = await asyncio.to_thread(_read_claims, out_dir)

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT status, run_attempts, started_at, completed_at FROM matches"
        )
        turns = await conn.fetchval("SELECT count(*) FROM turns")
        deadlocks = await conn.fetchval(
            "SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()"
        )
    finally:
        await conn.close()

    unfinished = sum(r["status"] != "completed" for r in rows)
    if unfinished or len(rows) != matches:
        raise BenchError(f"{unfinished} of {len(rows)} matches unfinished, wanted {matches}")
    first = min(r["started_at"] for r in rows)
    last = max(r["completed_at"] for r in rows)
    return BenchResult(
        workers=workers,
        matches=matches,
        turns=turns,
        elapsed_s=(last - first).total_seconds(),
        queue_wait_s=percentiles([(r["started_at"] - go).total_seconds() for r in rows]),
        claim_ms=percentiles([seconds * 1000 for seconds in claims]),
        max_connections=peak[0],
        reruns=sum(r["run_attempts"] - 1 for r in rows),
        deadlocks=deadlocks,
        spend_mode=spend_mode,
    )


async def run_bench(
    admin_dsn: str,
    matches: int,
    workers: Sequence[int],
    players: int = 8,
    spend_mode: str = EXACT,
) -> list[BenchResult]:
    bench_dsn(admin_dsn)  # refuse before anything runs
    results = []
    try:
        for count in workers:
            results.append(await run_once(admin_dsn, matches, count, players, spend_mode))
            print(format_table(results[-1:], results[0]), flush=True)
    finally:
        await drop_database(admin_dsn)
    return results


def format_table(results: Sequence[BenchResult], base: BenchResult | None = None) -> str:
    base = base or results[0]
    lines = [
        "| Workers | Matches | Seconds | Matches/s | Turns/s | Speed-up "
        "| Queue wait p50/p95/p99 (s) | Claim p50/p95/p99 (ms) | DB connections | Spend |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in results:
        wait = "/".join(f"{v:.1f}" for v in r.queue_wait_s)
        claim = "/".join(f"{v:.1f}" for v in r.claim_ms)
        lines.append(
            f"| {r.workers} | {r.matches:,} | {r.elapsed_s:.1f} | {r.matches_per_s:.1f} "
            f"| {r.turns_per_s:,.0f} | {r.matches_per_s / base.matches_per_s:.2f}x "
            f"| {wait} | {claim} | {r.max_connections} | {r.spend_mode} |"
        )
    return "\n".join(lines)


def result_json(results: Sequence[BenchResult]) -> str:
    return json.dumps([asdict(r) for r in results], indent=2)


async def _child(players: int, out: Path) -> None:
    """One worker process: connect, say so, wait for go, drain the queue."""
    pool = await create_pool(os.environ[DSN_ENV], max_size=3)
    print("ready", flush=True)
    await asyncio.to_thread(sys.stdin.readline)
    claims: list[float] = []
    try:
        await run_worker(
            pool,
            PlayerFactory(bench_roster(players), {}),
            WorkerConfig(),
            exit_when_idle=True,
            on_claim=claims.append,
        )
    finally:
        await pool.close()
    await asyncio.to_thread(out.write_text, json.dumps(claims))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", nargs=2, metavar=("PLAYERS", "OUT"), required=True)
    child_args = parser.parse_args()
    asyncio.run(_child(int(child_args.child[0]), Path(child_args.child[1])))
