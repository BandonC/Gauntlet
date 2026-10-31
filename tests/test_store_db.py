"""Postgres integration tests: queue, leases, fencing, worker, budget.

Needs a disposable database: DATABASE_URL=... pytest -m db
Each test drops and recreates the public schema, so never point this at real data.
"""

import asyncio
import json
import math
import os
import re
from collections import Counter
from itertools import pairwise

import asyncpg
import pytest

from arena import live, web
from arena.costs import FREE
from arena.engine.match_runner import LeaseLost, TurnRecord
from arena.export import export_site
from arena.games.base import placements_from_scores
from arena.players.base import Decision, TurnContext
from arena.players.bots import RandomPlayer
from arena.ratings.glicko2 import Glicko, update_pair
from arena.ratings.weng_lin import WengLin, rate
from arena.roster import PlayerConfig, PlayerFactory, ProviderLimits, Roster
from arena.store import queue, ratings, read
from arena.store.db import create_pool
from arena.store.metrics import player_metrics, werewolf_metrics
from arena.store.migrate import MIGRATIONS_DIR, migrate
from arena.store.ratelimit import PostgresTokenBucket, seed_rate_limits
from arena.store.tournaments import (
    NEXT_ROUND_SQL,
    advance_tournament,
    create_round_robin,
    create_swiss,
    create_tables,
    leaderboard,
    resume_tournament,
    swiss_entrants,
)
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

    assert await queue.complete(pool, m.id, "w0", [1.0, 0.0], [1, 2])

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
    gw, gl = update_pair(Glicko(), Glicko(), 1.0)
    board = await leaderboard(pool, "connect4")
    assert [
        (r["player_id"], r["rating"], r["rd"], r["elo"], r["games"], r["points"]) for r in board
    ] == [
        (winner, round(gw.rating, 1), round(gw.rd, 1), 1516.0, 1, 1.0),
        (loser, round(gl.rating, 1), round(gl.rd, 1), 1484.0, 1, 0.0),
    ]
    assert board[0]["low"] == round(gw.rating - 2 * gw.rd, 1)
    # Weng-Lin rates heads-up matches too, and ranks the board.
    wl_w, wl_l = rate([WengLin(), WengLin()], [1, 2])
    assert [(r["ordinal"], r["mu"], r["sigma"]) for r in board] == [
        (round(wl_w.ordinal, 2), round(wl_w.mu, 2), round(wl_w.sigma, 2)),
        (round(wl_l.ordinal, 2), round(wl_l.mu, 2), round(wl_l.sigma, 2)),
    ]


async def test_complete_after_lost_lease_writes_no_ratings(pool):
    await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=1, budget_usd=None)
    (m,) = await _claim_all(pool, 1)
    await pool.execute("UPDATE matches SET lease_expires_at = now() - interval '1 second'")
    assert (await queue.claim(pool, "B", 30)).id == m.id

    assert not await queue.complete(pool, m.id, "w0", [1.0, 0.0], [1, 2])  # old owner
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
        await queue.complete(pool, m.id, "w0", [1.0, 0.0], [1, 2])

    row = await pool.fetchrow("SELECT status, lease_owner FROM matches WHERE id = $1", m.id)
    assert row["status"] == "running" and row["lease_owner"] == "w0"
    assert await pool.fetchval("SELECT count(*) FROM ratings WHERE games_played > 0") == 0


async def test_concurrent_completions_lose_no_rating_updates(pool):
    players = PLAYERS + [PlayerConfig("r3", "random", seed=3)]
    _, n = await create_round_robin(pool, "connect4", players, games_per_pair=4, budget_usd=None)
    claims = await _claim_all(pool, n)

    # Every match shares a player with others; complete them all at once.
    oks = await asyncio.gather(
        *(queue.complete(pool, c.id, f"w{i}", [1.0, 0.0], [1, 2]) for i, c in enumerate(claims))
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

    assert await queue.complete(pool, first.id, "w0", [1.0, 0.0], [1, 2])
    assert await tournament_status(pool, tid) == "running"
    assert await queue.complete(pool, second.id, "w1", [0.5, 0.5], [1, 1])
    assert await tournament_status(pool, tid) == "completed"


async def test_failed_match_pauses_tournament_until_resumed(pool):
    tid, _ = await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=3, budget_usd=None)
    first, second, third = await _claim_all(pool, 3)
    assert await queue.complete(pool, first.id, "w0", [1.0, 0.0], [1, 2])

    retry = dict(max_attempts=2, backoff_seconds=0)
    assert await queue.release_for_retry(pool, second.id, "w1", "boom", **retry) == "queued"
    assert await tournament_status(pool, tid) == "running"  # a retry is not a failure

    again = await queue.claim(pool, "w3", 30)
    assert again.id == second.id and again.run_attempts == 2
    assert await queue.release_for_retry(pool, second.id, "w3", "boom", **retry) == "failed"
    row = await pool.fetchrow("SELECT status, last_error FROM tournaments WHERE id = $1", tid)
    assert row["status"] == "paused" and row["last_error"] == f"match {second.id} failed: boom"
    # The third match gets retried too, but nothing is claimable while paused.
    assert await queue.release_for_retry(pool, third.id, "w2", "x", **retry) == "queued"
    assert await queue.claim(pool, "w4", 30) is None

    assert await resume_tournament(pool, tid) == 1
    m = await pool.fetchrow("SELECT status, run_attempts FROM matches WHERE id = $1", second.id)
    assert (m["status"], m["run_attempts"]) == ("queued", 0)
    row = await pool.fetchrow("SELECT status, last_error FROM tournaments WHERE id = $1", tid)
    assert (row["status"], row["last_error"]) == ("running", None)

    await run_worker(pool, FACTORY, FAST, concurrency=2, exit_when_idle=True)
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
        await advance_tournament(c1, tid)  # sees b running: no-op
        second = asyncio.create_task(advance_tournament(c2, tid))
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
    assert await queue.complete(pool, m.id, "w0", [0.0, 1.0], [2, 1])

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


async def test_postgres_bucket_never_grants_more_than_capacity_plus_refill(pool):
    capacity, rate, window = 5.0, 20.0, 1.5
    await pool.execute(
        "INSERT INTO rate_limits (vendor, tokens, capacity, refill_per_sec) "
        "VALUES ('v', $1, $1, $2)",
        capacity,
        rate,
    )
    grants: list[float] = []

    async def server_clock() -> float:  # the bucket's own clock: Docker's can differ from ours
        return await pool.fetchval("SELECT extract(epoch FROM now())::float8")

    t0 = await server_clock()

    async def hammer():
        bucket = PostgresTokenBucket(pool, "v")  # one per task, like separate workers
        while await server_clock() - t0 < window:
            await bucket.take()
            grants.append(await server_clock())

    await asyncio.gather(*(hammer() for _ in range(8)))

    grants.sort()
    for i, t in enumerate(grants, start=1):  # every prefix of the run respects the bucket
        assert i <= capacity + rate * (t - t0), f"grant {i} at +{t - t0:.3f}s"
    # ...and the bucket was actually used, not starved: close to the theoretical max.
    assert len(grants) >= 0.7 * (capacity + rate * window)


async def test_seed_rate_limits_upserts_and_keeps_fill_level(pool):
    limits = {
        "anthropic": ProviderLimits(requests_per_minute=600, max_concurrency=2),
        "openai": ProviderLimits(max_concurrency=4),  # no rpm: no bucket
    }
    assert await seed_rate_limits(pool, limits) == ["anthropic"]
    row = await pool.fetchrow("SELECT tokens, capacity, refill_per_sec FROM rate_limits")
    assert tuple(row) == (60.0, 60.0, 10.0)

    # A worker restart must not refill the bucket; a smaller capacity clamps it.
    await pool.execute("UPDATE rate_limits SET tokens = 3")
    await seed_rate_limits(pool, {"anthropic": ProviderLimits(requests_per_minute=120)})
    row = await pool.fetchrow("SELECT tokens, capacity, refill_per_sec FROM rate_limits")
    assert tuple(row) == (3.0, 12.0, 2.0)
    await seed_rate_limits(pool, {"anthropic": ProviderLimits(requests_per_minute=12)})
    assert await pool.fetchval("SELECT tokens FROM rate_limits") == 1.0


async def test_unseeded_vendor_raises_instead_of_waiting_forever(pool):
    with pytest.raises(RuntimeError, match="no rate_limits row"):
        await PostgresTokenBucket(pool, "nobody").take()


RATING_COLS = (
    "SELECT player_id, game, rating, glicko_rating, glicko_rd, glicko_vol, wl_mu, wl_sigma, "
    "games_played "
    "FROM ratings ORDER BY game, player_id"
)
HISTORY_COLS = (
    "SELECT player_id, match_id, rating_before, rating_after, glicko_rating_before, "
    "glicko_rating_after, glicko_rd_before, glicko_rd_after, wl_mu_before, wl_mu_after, "
    "wl_sigma_before, wl_sigma_after FROM rating_history "
    "ORDER BY player_id, match_id"
)


async def _snapshot(pool):
    return [tuple(r) for r in await pool.fetch(RATING_COLS)], [
        tuple(r) for r in await pool.fetch(HISTORY_COLS)
    ]


async def test_rebuild_reproduces_live_ratings_exactly(pool):
    # Live: 4 players, 24 matches completed in concurrent batches, so completions that
    # share a player race for the rating locks and commit in a nondeterministic order.
    players = PLAYERS + [PlayerConfig("r3", "random", seed=3), PlayerConfig("r4", "random", seed=4)]
    _, n = await create_round_robin(pool, "connect4", players, games_per_pair=4, budget_usd=None)
    claims = await _claim_all(pool, n)
    outcomes = [[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]]
    for start in range(0, n, 8):
        batch = claims[start : start + 8]
        oks = await asyncio.gather(
            *(
                queue.complete(
                    pool,
                    c.id,
                    f"w{start + i}",
                    outcomes[(start + i) % 3],
                    placements_from_scores(outcomes[(start + i) % 3]),
                )
                for i, c in enumerate(batch)
            )
        )
        assert all(oks)
    live = await _snapshot(pool)
    assert len(live[1]) == 2 * n

    # Wreck the tables, then rebuild from the match log: bit-for-bit identical floats.
    await pool.execute(
        "UPDATE ratings SET glicko_rating = 0, rating = 0, glicko_rd = 1, wl_mu = 0, wl_sigma = 1"
    )
    assert await ratings.rebuild(pool) == n
    assert await _snapshot(pool) == live


async def test_rebuild_of_one_game_leaves_other_games_alone(pool):
    await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=1, budget_usd=None)
    (m,) = await _claim_all(pool, 1)
    assert await queue.complete(pool, m.id, "w0", [1.0, 0.0], [1, 2])
    await pool.execute(
        "INSERT INTO ratings (player_id, game, rating, games_played) "
        "VALUES ('r1', 'other', 1234, 9)"
    )
    assert await ratings.rebuild(pool, "connect4") == 1
    assert await pool.fetchval("SELECT rating FROM ratings WHERE game = 'other'") == 1234


FIVE = PLAYERS + [PlayerConfig(f"r{i}", "random", seed=i) for i in (3, 4, 5)]
FIVE_FACTORY = PlayerFactory(Roster({p.id: p for p in FIVE}, {}), {})


async def _round_pairs(pool, tid):
    rows = await pool.fetch(
        "SELECT m.round, array_agg(s.player_id ORDER BY s.player_id) AS seats "
        "FROM matches m JOIN match_seats s ON s.match_id = m.id "
        "WHERE m.tournament_id = $1 GROUP BY m.id ORDER BY m.round, m.id",
        tid,
    )
    by_round: dict[int, list[frozenset]] = {}
    for r in rows:
        by_round.setdefault(r["round"], []).append(frozenset(r["seats"]))
    return by_round


async def test_swiss_runs_every_round_without_rematches(pool):
    tid, n = await create_swiss(pool, "connect4", FIVE, budget_usd=None)
    t = await pool.fetchrow("SELECT rounds, current_round FROM tournaments WHERE id = $1", tid)
    assert (t["rounds"], t["current_round"], n) == (3, 1, 4)  # ceil(log2 5); 2 pairings x 2

    await run_worker(pool, FIVE_FACTORY, FAST, concurrency=3, exit_when_idle=True)

    t = await pool.fetchrow("SELECT status, current_round FROM tournaments WHERE id = $1", tid)
    assert (t["status"], t["current_round"]) == ("completed", 3)
    by_round = await _round_pairs(pool, tid)
    assert sorted(by_round) == [1, 2, 3]
    pairings = []
    for games in by_round.values():
        assert len(games) == 4 and all(games.count(g) == 2 for g in games)  # seats swapped
        pairings += set(games)
    assert len(pairings) == len(set(pairings)) == 6  # nobody met twice
    byes = await pool.fetch("SELECT player_id FROM tournament_byes WHERE tournament_id = $1", tid)
    assert len({b["player_id"] for b in byes}) == 3  # a different player sat out each round
    # Byes are not matches: only the 12 played games touched the ratings.
    assert await pool.fetchval("SELECT count(*) FROM rating_history") == 2 * 12


async def test_bye_counts_two_points_and_is_unrated(pool):
    three = FIVE[:3]
    tid, _ = await create_swiss(pool, "connect4", three, budget_usd=None, rounds=2)
    assert await pool.fetchval("SELECT player_id FROM tournament_byes") == "r3"
    m1, m2 = await _claim_all(pool, 2)  # r1 v r2, seats swapped
    winner = "r1"
    for i, m in enumerate((m1, m2)):
        scores = [1.0, 0.0] if m.seats[0] == winner else [0.0, 1.0]
        assert await queue.complete(pool, m.id, f"w{i}", scores, placements_from_scores(scores))

    async with pool.acquire() as conn:
        entrants = {e.player_id: e for e in await swiss_entrants(conn, tid, "connect4")}
    assert (entrants["r3"].points, entrants["r3"].had_bye) == (2.0, True)
    assert entrants["r1"].points == 2.0  # won both games
    # r2 lost both, but round 2 is already queued with r2 on the bye: 0 + 2.
    assert (entrants["r2"].points, entrants["r2"].had_bye) == (2.0, True)
    assert await pool.fetchval("SELECT count(*) FROM ratings WHERE player_id = 'r3'") == 0
    # Seeds and tie-breaks use the same number the leaderboard ranks by.
    assert entrants["r3"].rating == WengLin().ordinal
    assert entrants["r1"].rating > entrants["r2"].rating
    # Round 2 was queued by the second completion: r1 v r3, and r2 sits out.
    assert (await _round_pairs(pool, tid))[2] == [frozenset({"r1", "r3"})] * 2
    assert await pool.fetchval("SELECT player_id FROM tournament_byes WHERE round = 2") == "r2"


async def test_round_finishing_together_queues_next_round_exactly_once(pool):
    four = FIVE[:4]
    tid, n = await create_swiss(pool, "connect4", four, budget_usd=None)
    assert n == 4
    claims = await _claim_all(pool, 4)
    for i, c in enumerate(claims[:2]):
        assert await queue.complete(pool, c.id, f"w{i}", [1.0, 0.0], [1, 2])
    a, b = claims[2:]
    done = "UPDATE matches SET status = 'completed', scores = '[1, 0]' WHERE id = $1"
    async with pool.acquire() as c1, pool.acquire() as c2:
        t1, t2 = c1.transaction(), c2.transaction()
        await t1.start()
        await t2.start()
        await c1.execute(done, a.id)
        await c2.execute(done, b.id)
        await advance_tournament(c1, tid)  # sees b running: no-op
        second = asyncio.create_task(advance_tournament(c2, tid))
        await asyncio.sleep(0.2)  # blocks on the tournament row lock
        await t1.commit()
        await second
        await t2.commit()
    assert await pool.fetchval("SELECT current_round FROM tournaments WHERE id = $1", tid) == 2
    assert len((await _round_pairs(pool, tid))[2]) == 4  # one round 2, not zero or two

    async with pool.acquire() as conn, conn.transaction():
        await advance_tournament(conn, tid)  # round 2 is pending: nothing happens
    assert await pool.fetchval("SELECT max(round) FROM matches WHERE tournament_id = $1", tid) == 2


async def test_round_counter_only_advances_once_per_round(pool):
    tid, _ = await create_swiss(pool, "connect4", FIVE[:4], budget_usd=None)
    # The conditional UPDATE is the second guard: a stale "current_round = 1" loses.
    assert await pool.fetchval(NEXT_ROUND_SQL, tid, 1) == 2
    assert await pool.fetchval(NEXT_ROUND_SQL, tid, 1) is None


async def test_swiss_paused_on_failure_and_resumed_round_carries_on(pool):
    tid, _ = await create_swiss(pool, "connect4", FIVE[:4], budget_usd=None)
    claims = await _claim_all(pool, 4)
    for i, c in enumerate(claims[:3]):
        assert await queue.complete(pool, c.id, f"w{i}", [1.0, 0.0], [1, 2])
    last = claims[3]
    fail = dict(max_attempts=1, backoff_seconds=0)
    assert await queue.release_for_retry(pool, last.id, "w3", "boom", **fail) == "failed"
    assert await tournament_status(pool, tid) == "paused"
    assert 2 not in await _round_pairs(pool, tid)  # no pairing while paused

    assert await resume_tournament(pool, tid) == 1
    await run_worker(pool, FIVE_FACTORY, FAST, concurrency=2, exit_when_idle=True)
    t = await pool.fetchrow("SELECT status, current_round FROM tournaments WHERE id = $1", tid)
    assert (t["status"], t["current_round"]) == ("completed", 2)


async def test_every_page_renders_from_a_real_tournament(pool):
    tid, n = await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=2, budget_usd=1)
    await run_worker(pool, FACTORY, FAST, concurrency=2, exit_when_idle=True)
    urls = web.Urls()

    index = await web.index_page(pool, urls)
    assert '<h2 id="connect4">' in index and "r1" in index and "r2" in index
    assert '<h2 id="liars_dice">' in index and "Liar&#39;s Dice" in index
    assert "No rated matches yet." in index

    ids = [m["id"] for m in await read.list_matches(pool, tid)]
    assert len(ids) == n
    t_html = await web.tournament_page(pool, urls, tid)
    assert all(f'href="/matches/{i}"' in t_html for i in ids) and "completed" in t_html
    assert f'href="/tournaments/{tid}"' in await web.tournaments_page(pool, urls)
    recent = await web.matches_page(pool, urls)
    assert all(f'href="/matches/{i}"' in recent for i in ids)

    detail = await read.match_detail(pool, ids[0])
    m_html = await web.match_page(pool, urls, ids[0])
    assert m_html.count("<tr data-frame=") == len(detail["turns"])  # one row per turn
    assert 'id="replay-data"' in m_html and "initReplay" in m_html
    assert await web.match_page(pool, urls, 10**9) is None
    assert await web.tournament_page(pool, urls, 10**9) is None


async def test_reply_blocks_survive_the_turn_log_but_stay_out_of_match_data(pool):
    await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=1, budget_usd=None)
    (m,) = await _claim_all(pool, 1)
    store = PostgresTurnStore(pool, "w0")
    blocks = [[{"type": "text", "chars": 7, "text": "private"}], [{"type": "tool_use", "chars": 9}]]
    turn = TurnRecord(
        0, 0, {"column": 3}, False, 2, None, None, Usage(), 0.0, 5, reply_blocks=blocks
    )
    await store.append_turn(str(m.id), turn)
    assert (await store.load_turns(str(m.id)))[0].reply_blocks == blocks
    detail = await read.match_detail(pool, m.id)
    assert "reply_blocks" not in detail["turns"][0] and "private" not in json.dumps(detail)


async def test_stop_reasons_survive_the_turn_log_one_per_attempt(pool):
    await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=1, budget_usd=None)
    (m,) = await _claim_all(pool, 1)
    store = PostgresTurnStore(pool, "w0")
    reasons = ["max_tokens", None, "tool_use"]
    turn = TurnRecord(
        0, 0, {"column": 3}, False, 3, None, None, Usage(), 0.0, 5, stop_reasons=reasons
    )
    await store.append_turn(str(m.id), turn)
    assert (await store.load_turns(str(m.id)))[0].stop_reasons == reasons
    assert (await read.match_detail(pool, m.id))["turns"][0]["stop_reasons"] == reasons


async def test_thinking_tokens_survive_the_turn_log_and_cost_is_stored_unchanged(pool):
    await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=1, budget_usd=None)
    (m,) = await _claim_all(pool, 1)
    store = PostgresTurnStore(pool, "w0")
    usage = Usage(100, 300, 0, 0, 280)
    turn = TurnRecord(0, 0, {"column": 3}, False, 1, None, None, usage, 0.0031, 5)
    await store.append_turn(str(m.id), turn)
    saved = (await store.load_turns(str(m.id)))[0]
    assert saved.usage == usage and saved.cost_usd == pytest.approx(0.0031)
    detail = await read.match_detail(pool, m.id)
    assert detail["turns"][0]["tokens"]["thinking_tokens"] == 280


async def test_a_player_page_totals_real_games_and_unknown_players_have_none(pool):
    await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=2, budget_usd=1)
    await run_worker(pool, FACTORY, FAST, concurrency=2, exit_when_idle=True)

    r1, r2 = [await web.player_data(pool, p) for p in ("r1", "r2")]
    assert r1["overall"]["games"] == r2["overall"]["games"] == 2
    assert r1["overall"]["won"] + r2["overall"]["won"] + r1["overall"]["drew"] == 2
    assert [g["game"] for g in r1["games"]] == ["connect4"] and r1["roles"] is None
    assert len(r1["recent"]) == 2 and r1["recent"][0]["id"] > r1["recent"][1]["id"]
    assert {g["place"] for g in r1["games"] + r2["games"]} == {1, 2}

    html = await web.player_page(pool, web.Urls(), web.player_slug("r1"))
    assert "<h2>By game</h2>" in html
    assert await web.player_page(pool, web.Urls(), "nobody") is None
    assert await web.player_json(pool, "nobody") is None


async def test_the_comparison_leaves_out_bots_and_random_players(pool):
    await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=2, budget_usd=1)
    await run_worker(pool, FACTORY, FAST, concurrency=2, exit_when_idle=True)
    assert await web.compare_data(pool) == {"models": [], "games": []}
    rows = await read.seat_results(pool)
    await pool.execute("UPDATE players SET provider = 'anthropic' WHERE id = 'r1'")
    found = await web.compare_data(pool)
    assert [m["model"] for m in found["models"]] == ["r1"]
    assert found["models"][0]["games"] == sum(r["player_id"] == "r1" for r in rows)
    assert [g["game"] for g in found["games"]] == ["connect4"]
    assert "<h2>r1</h2>" in await web.compare_page(pool, web.Urls())


async def test_export_writes_a_browsable_site_whose_links_all_resolve(pool, tmp_path):
    tid, n = await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=2, budget_usd=1)
    await run_worker(pool, FACTORY, FAST, concurrency=2, exit_when_idle=True)
    ids = await read.match_ids(pool)
    assert len(ids) == n

    summary = await export_site(pool, tmp_path)
    # index, tournaments, matches, compare, one tournament, n matches, two players
    assert summary.pages == 4 + 1 + n + 2
    assert (tmp_path / ".nojekyll").exists()

    pages = sorted(tmp_path.glob("*.html"))
    assert len(pages) == summary.pages
    for html_file in pages:
        html = html_file.read_text(encoding="utf-8")
        hrefs = re.findall(r'href="([^"]+)"', html)
        assert hrefs, html_file.name
        for href in hrefs:
            assert not href.startswith("/"), (html_file.name, href)  # would break on disk
            target = href.partition("#")[0]  # "#roles" is this page
            assert not target or (tmp_path / target).is_file(), (html_file.name, href)
    t_html = (tmp_path / f"tournament-{tid}.html").read_text(encoding="utf-8")
    assert all(f'href="match-{i}.html"' in t_html for i in ids)
    assert 'id="replay-data"' in (tmp_path / f"match-{ids[0]}.html").read_text(encoding="utf-8")

    api = tmp_path / "api"
    assert len(list(api.rglob("*.json"))) == summary.json_files
    assert json.loads((api / "matches" / f"{ids[0]}.json").read_text(encoding="utf-8")) == (
        json.loads(json.dumps(await read.match_detail(pool, ids[0])))
    )
    board = json.loads((api / "leaderboard" / "connect4.json").read_text(encoding="utf-8"))
    assert {r["player_id"] for r in board} == {"r1", "r2"}
    metrics = json.loads((api / "metrics" / "connect4.json").read_text(encoding="utf-8"))
    assert all(isinstance(r["attempts"], int) for r in metrics)  # Decimal -> number
    assert json.loads((api / "tournaments.json").read_text(encoding="utf-8"))[0]["id"] == tid

    # Re-exporting into the same directory overwrites cleanly.
    assert (await export_site(pool, tmp_path)).pages == summary.pages


async def test_a_partial_export_writes_only_what_was_chosen_and_has_no_dead_links(pool, tmp_path):
    kept, n = await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=2, budget_usd=1)
    gone, _ = await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=1, budget_usd=1)
    await run_worker(pool, FACTORY, FAST, concurrency=2, exit_when_idle=True)
    ids = await read.match_ids(pool)

    summary = await export_site(pool, tmp_path, tournaments=[kept], matches=ids[:1])
    assert summary.pages == 4 + 1 + 1 + 2
    names = {p.name for p in tmp_path.glob("*.html")}
    assert names == {
        "index.html",
        "tournaments.html",
        "matches.html",
        "compare.html",
        "player-r1.html",
        "player-r2.html",
        f"tournament-{kept}.html",
        f"match-{ids[0]}.html",
    }
    for html_file in tmp_path.glob("*.html"):
        for href in re.findall(r'href="([^"]*)"', html_file.read_text(encoding="utf-8")):
            target = href.partition("#")[0]  # "#roles" is this page
            assert href and (not target or (tmp_path / target).is_file()), (html_file.name, href)

    t_html = (tmp_path / f"tournament-{kept}.html").read_text(encoding="utf-8")
    assert f'href="match-{ids[0]}.html"' in t_html
    assert f"<td>#{ids[1]}</td>" in t_html  # still listed, just not a link
    assert f"tournament-{gone}" not in (tmp_path / "tournaments.html").read_text(encoding="utf-8")

    api = tmp_path / "api"
    assert [t["id"] for t in json.loads((api / "tournaments.json").read_text("utf-8"))] == [kept]
    assert [m["id"] for m in json.loads((api / "matches.json").read_text("utf-8"))] == ids[:1]
    assert [p.stem for p in (api / "matches").glob("*.json")] == [str(ids[0])]
    board = json.loads((api / "leaderboard" / "connect4.json").read_text(encoding="utf-8"))
    assert {r["player_id"] for r in board} == {"r1", "r2"}  # leaderboards stay whole


async def test_a_partial_export_refuses_ids_that_do_not_exist(pool, tmp_path):
    with pytest.raises(ValueError, match="999"):
        await export_site(pool, tmp_path, matches=[999])
    assert not list(tmp_path.iterdir())


async def test_tables_tournament_runs_rates_every_seat_and_rebuilds(pool):
    tid, n = await create_tables(pool, "liars_dice", FIVE, 3, 10, budget_usd=None, seed=4)
    assert n == 10
    await run_worker(pool, FIVE_FACTORY, FAST, concurrency=3, exit_when_idle=True)
    assert await tournament_status(pool, tid) == "completed"

    rows = await pool.fetch("SELECT scores, placements FROM matches WHERE tournament_id = $1", tid)
    assert len(rows) == 10
    for r in rows:
        assert sorted(r["placements"]) == [1, 2, 3]
        assert r["scores"][r["placements"].index(1)] == 1.0

    # Every seat of every match rated; Elo and Glicko-2 never moved at three seats.
    assert await pool.fetchval("SELECT sum(games_played) FROM ratings") == 30
    assert await pool.fetchval("SELECT count(*) FROM rating_history") == 30
    assert await pool.fetchval(
        "SELECT count(*) FROM rating_history WHERE rating_before <> rating_after "
        "OR glicko_rating_before <> glicko_rating_after"
    ) == 0  # fmt: skip
    assert await pool.fetchval(
        "SELECT count(*) FROM rating_history WHERE wl_sigma_after >= wl_sigma_before"
    ) == 0  # fmt: skip
    board = await leaderboard(pool, "liars_dice")
    assert [r["ordinal"] for r in board] == sorted((r["ordinal"] for r in board), reverse=True)
    assert all((r["elo"], r["rating"]) == (1500.0, 1500.0) for r in board)

    live = await _snapshot(pool)
    await pool.execute("UPDATE ratings SET wl_mu = 0, wl_sigma = 1")
    assert await ratings.rebuild(pool, "liars_dice") == 10
    assert await _snapshot(pool) == live


async def test_tied_placements_reach_the_rating(pool):
    await create_tables(pool, "liars_dice", FIVE[:3], 3, 1, budget_usd=None, seed=1)
    (m,) = await _claim_all(pool, 1)
    assert await queue.complete(pool, m.id, "w0", [1.0, 1.0, 0.0], [1, 1, 3])
    expected = rate([WengLin()] * 3, [1, 1, 3])
    rows = await pool.fetch(
        "SELECT s.seat, r.wl_mu, r.wl_sigma FROM ratings r "
        "JOIN match_seats s ON s.player_id = r.player_id WHERE s.match_id = $1 ORDER BY s.seat",
        m.id,
    )
    assert [(r["wl_mu"], r["wl_sigma"]) for r in rows] == [(e.mu, e.sigma) for e in expected]
    assert await pool.fetchval("SELECT placements FROM matches WHERE id = $1", m.id) == [1, 1, 3]


async def test_overlapping_tables_completing_at_once_serialise_and_rebuild_exactly(pool):
    # Tables of 4 from 5 players share at least 3 players, seated in different orders, so
    # concurrent completions all want the same rating rows. Unordered locks would deadlock.
    _, n = await create_tables(pool, "liars_dice", FIVE, 4, 24, budget_usd=None, seed=9)
    claims = await _claim_all(pool, n)
    finishes = [[1, 2, 3, 4], [4, 3, 2, 1], [2, 1, 4, 3], [1, 1, 3, 3]]
    for start in range(0, n, 8):
        oks = await asyncio.gather(
            *(
                queue.complete(
                    pool,
                    c.id,
                    f"w{start + i}",
                    [float(p == 1) for p in finishes[(start + i) % 4]],
                    finishes[(start + i) % 4],
                )
                for i, c in enumerate(claims[start : start + 8])
            )
        )
        assert all(oks)
    live = await _snapshot(pool)
    assert len(live[1]) == 4 * n

    await pool.execute("UPDATE ratings SET wl_mu = 0, wl_sigma = 1")
    assert await ratings.rebuild(pool) == n
    assert await _snapshot(pool) == live


async def test_tables_refuse_a_size_the_game_cannot_seat(pool):
    with pytest.raises(ValueError, match="connect4 takes 2-2"):
        await create_tables(pool, "connect4", FIVE, 3, 4, budget_usd=None)
    assert await pool.fetchval("SELECT count(*) FROM tournaments") == 0


async def test_migration_backfills_placements_and_rebuild_rates_old_matches(pool):
    # Rebuild the database as it stood at 0004, with matches already completed.
    conn = await asyncpg.connect(DSN)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.execute(
        "CREATE TABLE schema_migrations ("
        " name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    for path in sorted(MIGRATIONS_DIR.glob("*.sql"))[:4]:
        await conn.execute(path.read_text())
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", path.name)
    await conn.execute(
        """
        INSERT INTO players (id, provider, model, prompt_key, config_hash)
        VALUES ('a', 'random', 'm', 'p', 'ha'), ('b', 'random', 'm', 'p', 'hb');
        INSERT INTO matches (id, game, seed, status, scores, completed_at) VALUES
            (1, 'connect4', 1, 'completed', '[0.0, 1.0]', now()),
            (2, 'connect4', 2, 'completed', '[0.5, 0.5]', now()),
            (3, 'connect4', 3, 'queued', NULL, NULL);
        INSERT INTO match_seats (match_id, seat, player_id) VALUES
            (1, 0, 'a'), (1, 1, 'b'), (2, 0, 'a'), (2, 1, 'b'), (3, 0, 'a'), (3, 1, 'b');
        """
    )
    await conn.close()

    assert (await migrate(DSN))[0] == "0005_weng_lin.sql"
    rows = await pool.fetch("SELECT placements FROM matches ORDER BY id")
    assert [r["placements"] for r in rows] == [[2, 1], [1, 1], None]

    assert await ratings.rebuild(pool, "connect4") == 2
    win_b = rate([WengLin(), WengLin()], [2, 1])
    a, b = rate(win_b, [1, 1])
    board = {r["player_id"]: r for r in await leaderboard(pool, "connect4")}
    assert (board["a"]["mu"], board["b"]["mu"]) == (round(a.mu, 2), round(b.mu, 2))


WW = [PlayerConfig(f"ww{i}", "bot", model="werewolf", seed=i) for i in range(1, 10)]
WW_FACTORY = PlayerFactory(Roster({p.id: p for p in WW}, {}), {})


async def test_werewolf_tournament_runs_on_bots_with_balanced_scheduled_roles(pool):
    tid, n = await create_tables(pool, "werewolf", WW[:8], 7, 16, budget_usd=None, seed=2)
    assert n == 16
    await run_worker(pool, WW_FACTORY, FAST, concurrency=4, exit_when_idle=True)
    assert await tournament_status(pool, tid) == "completed"

    rows = await pool.fetch(
        "SELECT m.options, m.scores, m.placements, array_agg(s.player_id ORDER BY s.seat) AS seats "
        "FROM matches m JOIN match_seats s ON s.match_id = m.id "
        "WHERE m.tournament_id = $1 GROUP BY m.id",
        tid,
    )
    counts = {}
    for r in rows:
        assert sum(r["scores"]) in (2.0, 5.0) and r["placements"].count(1) == sum(r["scores"])
        for seat, role in enumerate(r["options"]["roles"]):
            per_role = counts.setdefault(role, dict.fromkeys((p.id for p in WW[:8]), 0))
            per_role[r["seats"][seat]] += 1
    assert set(counts) == {"wolf", "seer", "villager"}
    assert all(max(c.values()) - min(c.values()) <= 1 for c in counts.values())

    # Weng-Lin rated all 7 seats; team ties reached it; the replay and rebuild agree.
    assert await pool.fetchval("SELECT count(*) FROM rating_history") == 16 * 7
    assert len(await leaderboard(pool, "werewolf")) == 8
    live = await _snapshot(pool)
    await pool.execute("UPDATE ratings SET wl_mu = 0, wl_sigma = 1")
    assert await ratings.rebuild(pool, "werewolf") == 16
    assert await _snapshot(pool) == live

    stats = await werewolf_metrics(pool, tid)
    assert [s["player_id"] for s in stats] == [p.id for p in WW[:8]]
    for role, by_player in counts.items():
        assert {s["player_id"]: s[f"{role}_games"] for s in stats} == by_player
    assert sum(s["true_claims"] + s["false_claims"] for s in stats) > 0
    assert all(s["leaks"] == 0 for s in stats)  # bots never name themselves as wolves

    mid = await pool.fetchval("SELECT min(id) FROM matches WHERE tournament_id = $1", tid)
    detail = await read.match_detail(pool, mid)
    assert detail["frames"][0]["roles"] == detail_roles(rows, detail)
    assert "werewolf" in await web.match_page(pool, web.Urls(), mid)
    assert "Werewolf roles" in await web.index_page(pool, web.Urls())


def detail_roles(rows, detail):
    return next(r["options"]["roles"] for r in rows if list(r["seats"]) == detail["seats"])


async def test_a_day_first_tournament_runs_rates_apart_and_keeps_its_role_metrics(pool):
    tid, n = await create_tables(pool, "werewolf_dayfirst", WW[:8], 7, 8, budget_usd=None, seed=2)
    assert n == 8
    await run_worker(pool, WW_FACTORY, FAST, concurrency=2, exit_when_idle=True)
    assert await tournament_status(pool, tid) == "completed"

    games = await pool.fetch("SELECT DISTINCT game FROM ratings")
    assert [r["game"] for r in games] == ["werewolf_dayfirst"]
    assert len(await leaderboard(pool, "werewolf_dayfirst")) == 8
    assert await leaderboard(pool, "werewolf") == []

    stats = await werewolf_metrics(pool, tid)  # silently empty if the game id isn't included
    assert sum(s["wolf_games"] + s["seer_games"] + s["villager_games"] for s in stats) == 8 * 7
    assert sum(s["true_claims"] + s["false_claims"] for s in stats) > 0

    mid = await pool.fetchval("SELECT min(id) FROM matches WHERE tournament_id = $1", tid)
    detail = await read.match_detail(pool, mid)
    assert detail["frames"][0]["phase"] == "discussion" and len(detail["frames"][0]["alive"]) == 7
    assert "says:" in await web.match_page(pool, web.Urls(), mid)
    said = [t for t in detail["turns"] if t["claims"]]
    assert said and all(c["kind"] in ("true", "false", "leak") for t in said for c in t["claims"])
    home = await web.index_page(pool, web.Urls())
    assert f'href="/matches/{mid + 7}#blind"' in home  # newest
    assert "One rule change" not in home  # only one ruleset has been played
    assert (await read.totals(pool))["games"] == 0  # bots cost nothing, so no real games yet


async def test_a_werewolf_player_page_gives_each_games_role_and_the_stats_by_role(pool):
    await create_tables(pool, "werewolf_dayfirst", WW[:7], 7, 2, budget_usd=None, seed=3)
    await run_worker(pool, WW_FACTORY, FAST, concurrency=1, exit_when_idle=True)
    records = [await web.player_data(pool, p.id) for p in WW[:7]]
    # Played roles come from the same deal as the stats by role, and both from the match.
    for record in records:
        seen = Counter(r["role"] for r in record["recent"])
        assert {role: record["roles"][f"{role}_games"] for role in seen} == dict(seen)
        assert record["overall"]["games"] == len(record["recent"])
    wolves = sum(r["role"] == "wolf" for record in records for r in record["recent"])
    assert wolves == 2 * records[0]["games"][0]["games"]
    assert 'data-game="Werewolf (day-first)"' in await web.player_page(
        pool, web.Urls(), web.player_slug(WW[0].id)
    )


async def test_a_single_werewolf_table_seats_all_seven_and_finishes_the_tournament(pool):
    tid, n = await create_tables(pool, "werewolf", WW[:7], 7, 1, budget_usd=None, seed=3)
    assert n == 1
    await run_worker(pool, WW_FACTORY, FAST, concurrency=1, exit_when_idle=True)
    assert await tournament_status(pool, tid) == "completed"
    seated = await pool.fetch("SELECT player_id FROM match_seats")
    assert sorted(r["player_id"] for r in seated) == sorted(p.id for p in WW[:7])
    assert await ratings.final_ratings(pool, tid) is None  # not heads-up


async def test_truncated_flag_survives_the_turn_log(pool):
    tid, _ = await create_tables(pool, "werewolf", WW[:7], 7, 1, budget_usd=None, seed=2)
    m = await queue.claim(pool, "w0", 30)
    assert m.options is not None and len(m.options["roles"]) == 7
    store = PostgresTurnStore(pool, "w0")
    t = TurnRecord(
        0, 0, {"action": "say", "message": "x"}, False, 1, None, None, Usage(), 0, 0, True
    )
    await store.append_turn(str(m.id), t)
    assert (await store.load_turns(str(m.id)))[0].truncated is True


async def test_werewolf_metrics_count_every_forfeit(pool):
    tid, _ = await create_tables(pool, "werewolf", WW[:7], 7, 1, budget_usd=None, seed=2)
    m = await queue.claim(pool, "w0", 30)
    store = PostgresTurnStore(pool, "w0")
    for n in range(2):
        t = TurnRecord(n, 0, None, True, 3, None, "bad target", Usage(), 0, 0)
        await store.append_turn(str(m.id), t)
    seat0 = await pool.fetchval(
        "SELECT player_id FROM match_seats WHERE match_id = $1 AND seat = 0", m.id
    )
    stats = {s["player_id"]: s["forfeits"] for s in await werewolf_metrics(pool, tid)}
    assert stats.pop(seat0) == 2 and not any(stats.values())


async def test_scheduled_roles_are_what_the_match_plays(pool):
    await create_tables(pool, "werewolf", WW[:7], 7, 2, budget_usd=None, seed=5)
    await run_worker(pool, WW_FACTORY, FAST, concurrency=1, exit_when_idle=True)
    for r in await pool.fetch("SELECT id, options FROM matches"):
        detail = await read.match_detail(pool, r["id"])
        assert detail["frames"][-1]["roles"] == r["options"]["roles"]


def sse(message: str) -> tuple[str, int, dict]:
    id_line, event_line, data_line, *_ = message.split("\n")
    return event_line[7:], int(id_line[4:]), json.loads(data_line[6:])


async def _one_match(pool):
    await create_round_robin(pool, "connect4", PLAYERS, games_per_pair=1, budget_usd=None)
    return await queue.claim(pool, "w0", 30)


async def test_live_viewers_get_every_turn_once_while_it_is_being_played(pool):
    m = await _one_match(pool)
    store = PostgresTurnStore(pool, "w0")
    moves = [0, 1, 0, 1, 0, 1, 0]  # seat 0 stacks four
    await store.append_turn(str(m.id), turn(0, 0, moves[0]))

    async def watch(after):
        stream = live.match_events(pool, m.id, after, poll_seconds=0.005)
        return [sse(msg) async for msg in stream]

    # One viewer from the start, one that already has two turns, racing the writer.
    from_start = asyncio.create_task(watch(0))
    resumed = asyncio.create_task(watch(2))
    for n, col in enumerate(moves[1:], 1):
        await asyncio.sleep(0.02)
        await store.append_turn(str(m.id), turn(n, n % 2, col))
    await asyncio.sleep(0.02)
    assert not from_start.done()  # all turns are in, but the match isn't complete yet
    assert await queue.complete(pool, m.id, "w0", [1.0, 0.0], [1, 2])

    events = await from_start
    assert [e[0] for e in events] == ["turn"] * 7 + ["done"]
    assert [e[1] for e in events] == [1, 2, 3, 4, 5, 6, 7, 7]
    assert events[-1][2] == {"status": "completed"}

    detail = await read.match_detail(pool, m.id)
    assert [e[2]["frame"] for e in events[:-1]] == detail["frames"][1:]
    assert [e[2]["turn"]["turn_no"] for e in events[:-1]] == list(range(7))
    assert events[0][2]["turn"]["label"] == "column 0"

    late = await resumed
    assert [e[1] for e in late] == [3, 4, 5, 6, 7, 7]


async def test_a_finished_match_streams_what_is_left_and_ends(pool):
    m = await _one_match(pool)
    await PostgresTurnStore(pool, "w0").append_turn(str(m.id), turn(0, 0, 3))
    assert await queue.complete(pool, m.id, "w0", [1.0, 0.0], [1, 2])
    assert [sse(e)[0] async for e in live.match_events(pool, m.id, 0)] == ["turn", "done"]
    assert [sse(e)[0] async for e in live.match_events(pool, m.id, 1)] == ["done"]


async def test_the_stream_ends_when_a_match_fails_for_good_but_not_on_a_retry(pool):
    m = await _one_match(pool)
    stream = live.match_events(pool, m.id, poll_seconds=0.005, keepalive_seconds=0.01)
    assert await queue.release_for_retry(
        pool, m.id, "w0", "boom", max_attempts=5, backoff_seconds=0
    ) == "queued"  # fmt: skip
    assert await anext(stream) == live.KEEPALIVE  # still open, waiting for the retry

    again = await queue.claim(pool, "w1", 30)
    assert await queue.release_for_retry(
        pool, again.id, "w1", "boom", max_attempts=2, backoff_seconds=0
    ) == "failed"  # fmt: skip
    while (msg := await anext(stream)) == live.KEEPALIVE:
        pass
    assert sse(msg)[0::2] == ("done", {"status": "failed"})


async def test_progress_of_a_missing_match_is_none_and_its_stream_is_empty(pool):
    assert await read.match_progress(pool, 999) is None
    assert [e async for e in live.match_events(pool, 999)] == []


async def test_a_running_match_page_links_its_stream(pool):
    m = await _one_match(pool)
    html = await web.match_page(pool, web.Urls(), m.id)
    assert f'"events": "/api/matches/{m.id}/events"' in html


async def test_rejections_survive_the_turn_log_and_reach_the_api(pool):
    m = await _one_match(pool)
    store = PostgresTurnStore(pool, "w0")
    rejected = [{"attempt": 1, "action": {"column": 9}, "reason": "column 9 is out of range 0-6"}]
    t = TurnRecord(
        0, 0, {"column": 3}, False, 2, None, rejected[0]["reason"], Usage(), 0, 0, False, rejected
    )
    await store.append_turn(str(m.id), t)
    await store.append_turn(str(m.id), turn(1, 1, 3))
    first, second = await store.load_turns(str(m.id))
    assert first.rejections == rejected and second.rejections == []
    assert (await read.match_detail(pool, m.id))["turns"][0]["rejections"] == rejected


async def _finish(pool, tid):
    """Complete every queued match of a tournament: the first-listed player wins."""
    i = 0
    while (m := await queue.claim(pool, f"f{i}", 30)) is not None:
        winner = min(m.seats)
        scores = [1.0, 0.0] if m.seats[0] == winner else [0.0, 1.0]
        assert await queue.complete(pool, m.id, f"f{i}", scores, placements_from_scores(scores))
        i += 1


async def test_round_robin_matches_are_stored_in_shuffled_order(pool):
    tid, n = await create_round_robin(pool, "connect4", FIVE[:3], 10, budget_usd=None, seed=4)
    rows = await pool.fetch(
        "SELECT array_agg(s.player_id ORDER BY s.seat) AS seats FROM matches m "
        "JOIN match_seats s ON s.match_id = m.id WHERE m.tournament_id = $1 "
        "GROUP BY m.id ORDER BY m.id",
        tid,
    )
    pairs = [frozenset(r["seats"]) for r in rows]
    assert n == 30 and sum(a != b for a, b in pairwise(pairs)) > 8


async def test_final_ratings_ignore_play_order_and_reach_the_leaderboard(pool):
    tid, _ = await create_round_robin(pool, "connect4", PLAYERS, 6, budget_usd=None, seed=1)
    assert await ratings.final_ratings(pool, tid) is None  # not finished

    ids = [r["id"] for r in await pool.fetch("SELECT id FROM matches ORDER BY id")]
    for i, match_id in enumerate(ids):  # r1 wins 4, r2 wins 2, in claim order
        m = await queue.claim(pool, f"w{i}", 30)
        assert m.id == match_id
        r1_wins = i < 4
        scores = [1.0, 0.0] if (m.seats[0] == "r1") == r1_wins else [0.0, 1.0]
        assert await queue.complete(pool, m.id, f"w{i}", scores, placements_from_scores(scores))
    assert await tournament_status(pool, tid) == "completed"

    final = await ratings.final_ratings(pool, tid)
    assert list(final) == ["r1", "r2"]
    gap = final["r1"]["rating"] - final["r2"]["rating"]
    assert gap == pytest.approx(400 * math.log10(2), abs=0.2)  # 4:2 odds
    assert final["r1"]["low"] < final["r1"]["rating"] < final["r1"]["high"]

    board = {r["player_id"]: r for r in await leaderboard(pool, "connect4")}
    assert board["r1"]["final"] == final["r1"] and board["r1"]["final_tournament_id"] == tid
    assert "Final ratings" in await web.tournament_page(pool, web.Urls(), tid)
    assert "Final rating (95%)" in await web.index_page(pool, web.Urls())


async def test_no_finite_estimate_or_multi_seat_means_no_final_rating(pool):
    tid, _ = await create_round_robin(pool, "connect4", PLAYERS, 2, budget_usd=None, seed=1)
    await _finish(pool, tid)  # r1 wins everything
    assert await tournament_status(pool, tid) == "completed"
    assert await ratings.final_ratings(pool, tid) is None
    assert all(r["final"] is None for r in await leaderboard(pool, "connect4"))

    wid, _ = await create_tables(pool, "werewolf", WW[:7], 7, 1, budget_usd=None, seed=2)
    await run_worker(pool, WW_FACTORY, FAST, concurrency=1, exit_when_idle=True)
    assert await ratings.final_ratings(pool, wid) is None
    assert all(r["final_tournament_id"] is None for r in await leaderboard(pool, "werewolf"))
