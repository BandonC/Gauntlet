"""Postgres integration tests: queue, leases, fencing, worker, budget.

Needs a disposable database: DATABASE_URL=... pytest -m db
Each test drops and recreates the public schema, so never point this at real data.
"""

import asyncio
import os

import asyncpg
import pytest

from arena.engine.match_runner import LeaseLost, TurnRecord
from arena.roster import PlayerConfig, PlayerFactory, Roster
from arena.store import queue
from arena.store.db import create_pool
from arena.store.migrate import migrate
from arena.store.tournaments import create_round_robin, leaderboard
from arena.store.turns import PostgresTurnStore
from arena.types import Usage
from arena.worker import WorkerConfig, run_one, run_worker

DSN = os.environ.get("DATABASE_URL")
pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(not DSN, reason="DATABASE_URL not set"),
]

PLAYERS = [PlayerConfig("r1", "random", seed=1), PlayerConfig("r2", "random", seed=2)]
FACTORY = PlayerFactory(Roster({p.id: p for p in PLAYERS}, {}), {})
FAST = WorkerConfig(
    lease_seconds=30, heartbeat_seconds=5, max_run_attempts=2, idle_sleep_seconds=0.05
)


@pytest.fixture
async def pool():
    conn = await asyncpg.connect(DSN)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.close()
    await migrate(DSN)
    p = await create_pool(DSN, max_size=10)
    yield p
    await p.close()


def turn(n: int, seat: int, col: int) -> TurnRecord:
    return TurnRecord(n, seat, {"column": col}, False, 1, None, None, Usage(10, 1), 0.001, 5)


async def test_concurrent_claims_never_collide(pool):
    _, n = await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=6, budget_usd=None)
    claims = await asyncio.gather(*(queue.claim(pool, f"w{i}", 30) for i in range(10)))
    ids = [c.id for c in claims if c]
    assert len(ids) == n == 6 and len(set(ids)) == 6


async def test_expired_lease_is_reclaimed_and_old_writer_is_fenced(pool):
    await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=1, budget_usd=None)
    a = await queue.claim(pool, "A", 30)
    old = PostgresTurnStore(pool, "A")
    await old.append_turn(str(a.id), turn(0, 0, 3))

    # A stalls; its lease expires; B takes over.
    await pool.execute("UPDATE matches SET lease_expires_at = now() - interval '1 second'")
    b = await queue.claim(pool, "B", 30)
    assert b.id == a.id and b.run_attempts == 2

    with pytest.raises(LeaseLost):  # A wakes up and tries to keep writing
        await old.append_turn(str(a.id), turn(1, 1, 4))
    assert not await queue.heartbeat(pool, a.id, "A", 30)

    new = PostgresTurnStore(pool, "B")
    await new.append_turn(str(b.id), turn(1, 1, 4))
    assert [t.turn_no for t in await new.load_turns(str(b.id))] == [0, 1]

    with pytest.raises(LeaseLost):  # duplicate turn number is refused too
        await new.append_turn(str(b.id), turn(1, 1, 5))


async def test_worker_runs_tournament_and_resumes_partial_match(pool):
    tid, n = await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=4, budget_usd=None)

    # Simulate a worker that died after 2 turns of the first match.
    dead = await queue.claim(pool, "dead", 30)
    await PostgresTurnStore(pool, "dead").append_turn(str(dead.id), turn(0, 0, 3))
    await PostgresTurnStore(pool, "dead").append_turn(str(dead.id), turn(1, 1, 3))
    await pool.execute("UPDATE matches SET lease_expires_at = now() - interval '1 second'")

    await run_worker(pool, FACTORY, FAST, concurrency=3, exit_when_idle=True)

    rows = await pool.fetch("SELECT id, status, scores FROM matches ORDER BY id")
    assert [r["status"] for r in rows] == ["completed"] * n
    assert all(sum(r["scores"]) == 1.0 for r in rows)
    resumed = await PostgresTurnStore(pool, "x").load_turns(str(dead.id))
    assert resumed[0].action == {"column": 3} and resumed[1].action == {"column": 3}

    board = await leaderboard(pool, "connect4")
    assert {r["player_id"] for r in board} == {"r1", "r2"}
    assert sum(r["games"] for r in board) == 2 * n


async def test_budget_cap_stops_new_claims(pool):
    tid, _ = await create_round_robin(
        pool, "connect4", PLAYERS, games_per_pair=2, budget_usd=0.0015
    )
    m = await queue.claim(pool, "A", 30)
    store = PostgresTurnStore(pool, "A")
    await store.append_turn(str(m.id), turn(0, 0, 3))
    await store.append_turn(str(m.id), turn(1, 1, 3))  # spent 0.002 > cap
    spent = await pool.fetchval("SELECT spent_usd::float FROM tournaments WHERE id = $1", tid)
    assert spent == pytest.approx(0.002)
    assert await queue.claim(pool, "B", 30) is None


async def test_unknown_player_marks_match_failed(pool):
    await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=1, budget_usd=None)
    empty = PlayerFactory(Roster({}, {}), {})
    assert await run_one(pool, empty, "w", FAST)
    row = await pool.fetchrow("SELECT status, last_error FROM matches")
    assert row["status"] == "failed" and "not in roster" in row["last_error"]
