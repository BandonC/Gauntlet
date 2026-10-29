"""Postgres integration tests: queue, leases, fencing, worker, budget.

Needs a disposable database: DATABASE_URL=... pytest -m db
Each test drops and recreates the public schema, so never point this at real data.
"""

import asyncio
import os
from itertools import pairwise

import asyncpg
import pytest

from arena.costs import FREE
from arena.engine.match_runner import LeaseLost, TurnRecord
from arena.players.base import Decision, TurnContext
from arena.players.bots import RandomPlayer
from arena.roster import PlayerConfig, PlayerFactory, Roster
from arena.store import queue
from arena.store.db import create_pool
from arena.store.metrics import player_metrics
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


async def tournament_status(pool, tid: int) -> str:
    return await pool.fetchval("SELECT status FROM tournaments WHERE id = $1", tid)


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
    assert await tournament_status(pool, tid) == "completed"


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
    assert await tournament_status(pool, tid) == "running"  # capped, not finished


async def test_unknown_player_marks_match_failed(pool):
    await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=1, budget_usd=None)
    empty = PlayerFactory(Roster({}, {}), {})
    assert await run_one(pool, empty, "w", FAST)
    row = await pool.fetchrow("SELECT status, last_error FROM matches")
    assert row["status"] == "failed" and "not in roster" in row["last_error"]


async def _claim_all(pool, n: int) -> list[queue.ClaimedMatch]:
    claims = [await queue.claim(pool, f"w{i}", 30) for i in range(n)]
    assert all(claims)
    return claims


async def test_complete_writes_ratings_and_history_in_one_step(pool):
    await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=1, budget_usd=None)
    (m,) = await _claim_all(pool, 1)
    winner, loser = m.seats

    assert await queue.complete(pool, m.id, "w0", [1.0, 0.0])

    rows = await pool.fetch("SELECT player_id, rating, games_played FROM ratings ORDER BY rating")
    assert [(r["player_id"], r["rating"], r["games_played"]) for r in rows] == [
        (loser, 1484.0, 1),
        (winner, 1516.0, 1),
    ]
    hist = await pool.fetch(
        "SELECT player_id, rating_before, rating_after FROM rating_history WHERE match_id = $1",
        m.id,
    )
    assert {(h["player_id"], h["rating_before"], h["rating_after"]) for h in hist} == {
        (winner, 1500.0, 1516.0),
        (loser, 1500.0, 1484.0),
    }
    board = await leaderboard(pool, "connect4")
    assert [(r["player_id"], r["rating"], r["games"], r["points"]) for r in board] == [
        (winner, 1516.0, 1, 1.0),
        (loser, 1484.0, 1, 0.0),
    ]


async def test_complete_after_lost_lease_writes_no_ratings(pool):
    await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=1, budget_usd=None)
    (m,) = await _claim_all(pool, 1)
    await pool.execute("UPDATE matches SET lease_expires_at = now() - interval '1 second'")
    assert (await queue.claim(pool, "B", 30)).id == m.id

    assert not await queue.complete(pool, m.id, "w0", [1.0, 0.0])  # old owner
    assert await pool.fetchval("SELECT count(*) FROM ratings") == 0
    assert await pool.fetchval("SELECT count(*) FROM rating_history") == 0


async def test_double_rating_rolls_back_completion(pool):
    await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=1, budget_usd=None)
    (m,) = await _claim_all(pool, 1)
    # A stray history row for this match (e.g. a bug elsewhere) must block re-rating.
    await pool.execute(
        "INSERT INTO rating_history (player_id, game, match_id, rating_before, rating_after) "
        "VALUES ($1, 'connect4', $2, 1500, 1500)",
        m.seats[0],
        m.id,
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await queue.complete(pool, m.id, "w0", [1.0, 0.0])

    row = await pool.fetchrow("SELECT status, lease_owner FROM matches WHERE id = $1", m.id)
    assert row["status"] == "running" and row["lease_owner"] == "w0"
    assert await pool.fetchval("SELECT count(*) FROM ratings WHERE games_played > 0") == 0


async def test_concurrent_completions_lose_no_rating_updates(pool):
    players = PLAYERS + [PlayerConfig("r3", "random", seed=3)]
    _, n = await create_round_robin(pool, "connect4", players, games_per_pair=4, budget_usd=None)
    claims = await _claim_all(pool, n)

    # Every match shares a player with others; complete them all at once.
    oks = await asyncio.gather(
        *(queue.complete(pool, c.id, f"w{i}", [1.0, 0.0]) for i, c in enumerate(claims))
    )
    assert all(oks)

    rows = await pool.fetch("SELECT player_id, rating, games_played FROM ratings")
    assert sum(r["games_played"] for r in rows) == 2 * n  # no lost updates
    assert sum(r["rating"] for r in rows) == pytest.approx(1500.0 * len(rows))  # zero-sum
    # Each player's history is one unbroken chain: every update saw the previous one.
    for r in rows:
        chain = await pool.fetch(
            "SELECT rating_before, rating_after FROM rating_history "
            "WHERE player_id = $1 ORDER BY id",
            r["player_id"],
        )
        assert len(chain) == r["games_played"]
        assert chain[0]["rating_before"] == 1500.0
        assert all(x["rating_after"] == y["rating_before"] for x, y in pairwise(chain))
        assert chain[-1]["rating_after"] == r["rating"]


async def test_tournament_completes_only_after_last_match(pool):
    tid, _ = await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=2, budget_usd=None)
    first, second = await _claim_all(pool, 2)

    assert await queue.complete(pool, first.id, "w0", [1.0, 0.0])
    assert await tournament_status(pool, tid) == "running"
    assert await queue.complete(pool, second.id, "w1", [0.5, 0.5])
    assert await tournament_status(pool, tid) == "completed"


async def test_failed_match_counts_as_done_but_retry_does_not(pool):
    tid, _ = await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=2, budget_usd=None)
    first, second = await _claim_all(pool, 2)
    assert await queue.complete(pool, first.id, "w0", [1.0, 0.0])

    retry = dict(max_attempts=2, backoff_seconds=0)
    assert await queue.release_for_retry(pool, second.id, "w1", "boom", **retry) == "queued"
    assert await tournament_status(pool, tid) == "running"

    again = await queue.claim(pool, "w2", 30)
    assert again.id == second.id and again.run_attempts == 2
    assert await queue.release_for_retry(pool, second.id, "w2", "boom", **retry) == "failed"
    assert await tournament_status(pool, tid) == "completed"


async def test_last_two_matches_finishing_together_close_the_tournament(pool):
    # Two transactions each finish one of the last two matches and check the tournament
    # before either commits. Without the tournament row lock, each sees the other's
    # match as still running, and the tournament is never closed.
    tid, _ = await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=2, budget_usd=None)
    a, b = await _claim_all(pool, 2)
    done = "UPDATE matches SET status = 'completed', scores = '[1, 0]' WHERE id = $1"
    async with pool.acquire() as c1, pool.acquire() as c2:
        t1, t2 = c1.transaction(), c2.transaction()
        await t1.start()
        await t2.start()
        await c1.execute(done, a.id)
        await c2.execute(done, b.id)
        await queue.finish_tournament_if_done(c1, tid)  # sees b running: no-op
        second = asyncio.create_task(queue.finish_tournament_if_done(c2, tid))
        await asyncio.sleep(0.2)  # let it block on the tournament row lock
        await t1.commit()
        await second
        await t2.commit()
    assert await tournament_status(pool, tid) == "completed"


class HeldPlayer(RandomPlayer):
    """RandomPlayer whose first move waits until the test releases it."""

    def __init__(self, player_id: str, seed: int, started: asyncio.Event, gate: asyncio.Event):
        super().__init__(player_id, seed)
        self.started, self.gate = started, gate

    async def act(self, ctx: TurnContext) -> Decision:
        self.started.set()
        await self.gate.wait()
        return await super().act(ctx)


class HeldFactory:
    def __init__(self) -> None:
        self.started, self.gate = asyncio.Event(), asyncio.Event()

    def build(self, player_id: str):
        seed = {p.id: p.seed for p in PLAYERS}[player_id]
        return HeldPlayer(player_id, seed, self.started, self.gate), FREE


async def test_graceful_stop_persists_in_flight_turn_and_hands_match_back(pool):
    tid, _ = await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=1, budget_usd=None)
    held, stop = HeldFactory(), asyncio.Event()
    worker = asyncio.create_task(run_one(pool, held, "A", FAST, stop))

    await held.started.wait()  # turn 0 is mid-decision (an LLM call, in real life)
    stop.set()  # SIGTERM arrives
    held.gate.set()  # the in-flight decision returns
    assert await asyncio.wait_for(worker, 5)

    mid = await pool.fetchval("SELECT id FROM matches")
    assert [t.turn_no for t in await PostgresTurnStore(pool, "x").load_turns(str(mid))] == [0]
    row = await pool.fetchrow(
        "SELECT status, lease_owner, run_attempts, available_at <= now() AS ready "
        "FROM matches WHERE id = $1",
        mid,
    )
    assert dict(row) == {"status": "queued", "lease_owner": None, "run_attempts": 0, "ready": True}

    # Another worker picks it up at once (no lease expiry wait) and finishes from the log.
    assert await run_one(pool, FACTORY, "B", FAST)
    assert await pool.fetchval("SELECT status FROM matches WHERE id = $1", mid) == "completed"
    assert await tournament_status(pool, tid) == "completed"


async def test_run_worker_with_stop_set_claims_nothing(pool):
    await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=1, budget_usd=None)
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(run_worker(pool, FACTORY, FAST, concurrency=2, stop=stop), 5)
    assert await pool.fetchval("SELECT status FROM matches") == "queued"


def rec(n, seat, *, attempts=1, forfeited=False, latency=0, usage=None, cost=0.0):
    action = None if forfeited else {"column": n % 7}
    usage = usage or Usage()
    return TurnRecord(n, seat, action, forfeited, attempts, None, None, usage, cost, latency)


async def test_player_metrics_from_turn_log(pool):
    tid, _ = await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=1, budget_usd=None)
    (m,) = await _claim_all(pool, 1)
    p0, p1 = m.seats
    store = PostgresTurnStore(pool, "w0")
    # p0: second attempt legal; p1: first attempt legal; p0: three bad attempts, forfeit.
    await store.append_turn(
        str(m.id), rec(0, 0, attempts=2, latency=100, usage=Usage(100, 10, 300, 0), cost=0.01)
    )
    await store.append_turn(str(m.id), rec(1, 1, latency=200, usage=Usage(50, 5), cost=0.002))
    await store.append_turn(
        str(m.id),
        rec(2, 0, attempts=3, forfeited=True, latency=300, usage=Usage(100, 10, 0, 100), cost=0.02),
    )
    assert await queue.complete(pool, m.id, "w0", [0.0, 1.0])

    # A second tournament with a match still running: counts for turns, not cost per game.
    tid2, _ = await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=1, budget_usd=None)
    (m2,) = await _claim_all(pool, 1)
    first = m2.seats[0]
    await PostgresTurnStore(pool, "w0").append_turn(str(m2.id), rec(0, 0, latency=1000, cost=0.5))

    by_player = {r["player_id"]: r for r in await player_metrics(pool, "connect4", tid)}
    assert by_player[p0] == {
        "player_id": p0,
        "turns": 2,
        "attempts": 5,
        "illegal_attempts": 4,
        "illegal_move_rate": 0.8,
        "forfeits": 1,
        "latency_p50_ms": 200.0,
        "latency_p95_ms": 290.0,
        "completed_games": 1,
        "cost_per_game_usd": 0.03,
        "cache_hit_rate": 0.5,  # 300 read / (200 uncached + 300 read + 100 written)
    }
    assert by_player[p1]["illegal_move_rate"] == 0.0
    assert by_player[p1]["latency_p95_ms"] == 200.0
    assert by_player[p1]["cache_hit_rate"] == 0.0
    assert by_player[p1]["cost_per_game_usd"] == 0.002

    everything = {r["player_id"]: r for r in await player_metrics(pool, "connect4")}
    assert everything[first]["turns"] == by_player[first]["turns"] + 1
    assert everything[first]["completed_games"] == 1  # the running match isn't a game yet
    assert everything[first]["cost_per_game_usd"] == by_player[first]["cost_per_game_usd"]
    assert await player_metrics(pool, "connect4", tid2 + 1) == []
    assert await player_metrics(pool, "liars_dice") == []
