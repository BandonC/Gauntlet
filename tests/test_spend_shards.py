"""Sharded spend: the total stays right, the cap holds to its bound, slots don't queue."""

import asyncio
import os

import asyncpg
import pytest

from arena.bench import bench_roster
from arena.chaos import CHAOS_PRICE, CHAOS_USAGE, ChaosFactory
from arena.store import read
from arena.store.db import create_pool
from arena.store.migrate import migrate
from arena.store.spend import ADD_EXACT_SQL, ADD_SHARD_SQL, SHARDED, TOTAL_SPEND_SQL
from arena.store.tournaments import create_round_robin
from arena.worker import WorkerConfig, run_worker

DSN = os.environ.get("DATABASE_URL")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not DSN, reason="DATABASE_URL not set")]

TURN_COST = CHAOS_PRICE.cost(CHAOS_USAGE)
ROSTER = bench_roster(3)
PLAYERS = list(ROSTER.players.values())
FAST = WorkerConfig(lease_seconds=30, heartbeat_seconds=5, idle_sleep_seconds=0.05)


@pytest.fixture
async def pool():
    conn = await asyncpg.connect(DSN)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.close()
    await migrate(DSN)
    p = await create_pool(DSN, max_size=12)
    yield p
    await p.close()


async def drain(pool, concurrency: int = 1) -> None:
    await run_worker(
        pool, ChaosFactory(ROSTER, {}), FAST, concurrency=concurrency, exit_when_idle=True
    )


async def total(pool) -> float:
    return float(await pool.fetchval(f"SELECT {TOTAL_SPEND_SQL} FROM tournaments t"))


async def test_turns_land_in_each_slots_shard_and_reads_see_the_sum(pool):
    tid, _ = await create_round_robin(
        pool, "liars_dice", PLAYERS, 2, None, seed=0, spend_mode=SHARDED
    )
    await drain(pool, concurrency=3)

    row = await pool.fetchrow("SELECT spent_usd, status FROM tournaments WHERE id = $1", tid)
    assert row["spent_usd"] == 0
    assert row["status"] == "completed"
    shards = await pool.fetch("SELECT worker_id, spent_usd FROM tournament_spend_shards")
    assert len(shards) >= 2
    logged = float(await pool.fetchval("SELECT sum(cost_usd) FROM turns"))
    assert logged > 0
    assert float(sum(s["spent_usd"] for s in shards)) == pytest.approx(logged)
    shown = (await read.list_tournaments(pool, tid))[0]["spent_usd"]
    assert shown == pytest.approx(logged)


async def test_exact_mode_writes_no_shards(pool):
    await create_round_robin(pool, "liars_dice", PLAYERS, 1, None, seed=0)
    await drain(pool)
    assert await pool.fetchval("SELECT count(*) FROM tournament_spend_shards") == 0
    assert await pool.fetchval("SELECT spent_usd FROM tournaments") > 0


@pytest.mark.parametrize("slots", [1, 4])
async def test_overshoot_stays_within_slots_times_max_turn_cost(pool, slots):
    budget = 25 * TURN_COST
    tid, n = await create_round_robin(
        pool, "liars_dice", PLAYERS, 8, budget, seed=0, spend_mode=SHARDED
    )
    await drain(pool, concurrency=slots)

    spent = await total(pool)
    assert budget <= spent <= budget + slots * TURN_COST + 1e-9
    left = await pool.fetch("SELECT status, lease_owner, run_attempts FROM matches")
    assert sum(r["status"] == "queued" for r in left) >= 1
    assert all(r["lease_owner"] is None for r in left)
    assert all(r["run_attempts"] <= 1 for r in left)  # the budget stop cost no attempt
    assert await pool.fetchval("SELECT status FROM tournaments WHERE id = $1", tid) == "running"


async def test_exact_mode_keeps_finishing_the_match_past_the_cap(pool):
    """Unchanged on purpose. A cap of one turn still plays one whole match."""
    await create_round_robin(pool, "liars_dice", PLAYERS, 8, TURN_COST, seed=0)
    await drain(pool)
    assert await pool.fetchval("SELECT count(*) FROM matches WHERE status = 'completed'") == 1
    assert await total(pool) > 5 * TURN_COST


async def test_raising_the_budget_resumes_a_stopped_match(pool):
    tid, n = await create_round_robin(
        pool, "liars_dice", PLAYERS, 1, 5 * TURN_COST, seed=0, spend_mode=SHARDED
    )
    await drain(pool)
    assert await pool.fetchval("SELECT count(*) FROM matches WHERE status = 'completed'") < n

    await pool.execute("UPDATE tournaments SET budget_usd = 100 WHERE id = $1", tid)
    await drain(pool)

    assert await pool.fetchval("SELECT status FROM tournaments WHERE id = $1", tid) == "completed"
    assert await total(pool) == pytest.approx(
        float(await pool.fetchval("SELECT sum(cost_usd) FROM turns"))
    )


async def uncommitted_write(pool, sql, *args):
    conn = await pool.acquire()
    tx = conn.transaction()
    await tx.start()
    await conn.execute(sql, *args)
    return conn, tx


async def test_two_slots_never_wait_on_each_other_but_exact_writers_do(pool):
    await create_round_robin(pool, "liars_dice", PLAYERS, 1, None, seed=0)
    match = await pool.fetchval("SELECT min(id) FROM matches")

    conn, tx = await uncommitted_write(pool, ADD_SHARD_SQL, match, 0.001, "slot-a")
    await asyncio.wait_for(pool.execute(ADD_SHARD_SQL, match, 0.001, "slot-b"), 2)
    await tx.rollback()
    await pool.release(conn)

    conn, tx = await uncommitted_write(pool, ADD_EXACT_SQL, match, 0.001)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(pool.execute(ADD_EXACT_SQL, match, 0.001), 0.5)
    await tx.rollback()
    await pool.release(conn)
