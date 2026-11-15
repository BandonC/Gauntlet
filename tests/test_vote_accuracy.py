"""Per-night village vote accuracy, counted from real engine games played by bots."""

import os

import asyncpg
import pytest

from arena.analysis.vote_accuracy import NIGHTS_BEFORE_DAY_ONE, Tally, load, tally
from arena.engine import InMemoryTurnStore, MatchRunner, MatchSpec
from arena.engine.match_runner import iter_states
from arena.games.registry import get_game
from arena.games.werewolf import SEATS, WOLF
from arena.players.bots import AccuracyVoteBot
from arena.roster import PlayerConfig, PlayerFactory, Roster
from arena.store.db import create_pool
from arena.store.migrate import migrate
from arena.store.tournaments import create_tables
from arena.worker import WorkerConfig, run_worker


async def final_state(name: str, seed: int, p: float, focus: float):
    game = get_game(name)
    roles = list(game.initial_state(SEATS, seed).roles)
    players = [AccuracyVoteBot(f"b{s}", seed, roles, p, focus) for s in range(SEATS)]
    spec = MatchSpec(f"va{seed}", name, seed, [q.player_id for q in players])
    out = await MatchRunner(game, players, InMemoryTurnStore()).run(spec)
    *_, final = iter_states(game, spec, out.turns)
    return final


@pytest.mark.parametrize("name", sorted(NIGHTS_BEFORE_DAY_ONE))
async def test_perfect_voters_always_hit_and_the_first_vote_removes_a_wolf(name):
    total = Tally()
    for seed in range(5):
        final = await final_state(name, seed, 1.0, 1.0)
        total.add(tally(name, final, final.winner == "village"))
    assert total.games == 5 and sum(total.votes.values()) > 0
    assert total.hits == total.votes
    assert total.first_vote == {"wolf": 5}
    assert min(total.votes) == NIGHTS_BEFORE_DAY_ONE[name]  # day 1 sits after that many nights


async def test_counts_only_living_non_wolves_and_every_village_vote_once():
    final = await final_state("werewolf_dayfirst", 3, 0.5, 0.0)
    t = tally("werewolf_dayfirst", final, final.winner == "village")
    wolves = {s for s, r in enumerate(final.roles) if r == WOLF}
    expected = 0
    for v in final.day_votes:
        dead = {
            d.seat for d in final.deaths if d.day < v.day or (d.day == v.day and d.cause == "night")
        }
        expected += v.seat not in wolves and v.seat not in dead
    assert sum(t.votes.values()) == expected
    assert sum(t.hits.values()) <= expected


DSN = os.environ.get("DATABASE_URL")
WW = [PlayerConfig(f"ww{i}", "bot", model="werewolf", seed=i) for i in range(1, 8)]
FAST = WorkerConfig(lease_seconds=30, heartbeat_seconds=5, idle_sleep_seconds=0.05)


@pytest.mark.db
@pytest.mark.skipif(not DSN, reason="DATABASE_URL not set")
async def test_load_replays_stored_games_and_leaves_cancelled_tournaments_out():
    assert DSN is not None
    conn = await asyncpg.connect(DSN)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.close()
    await migrate(DSN)
    pool = await create_pool(DSN, max_size=6)
    try:
        kept, n = await create_tables(pool, "werewolf_dayfirst", WW, 7, 4, budget_usd=None, seed=1)
        dropped, _ = await create_tables(pool, "werewolf", WW, 7, 3, budget_usd=None, seed=2)
        factory = PlayerFactory(Roster({p.id: p for p in WW}, {}), {})
        await run_worker(pool, factory, FAST, concurrency=2, exit_when_idle=True)
        # cancel_tournament refuses a finished tournament, and cancelling first would leave no
        # completed games to filter, so mark it the way the filter reads it.
        await pool.execute("UPDATE tournaments SET status = 'cancelled' WHERE id = $1", dropped)
        assert await pool.fetchval(
            "SELECT count(*) FROM matches WHERE tournament_id = $1 AND status = 'completed'",
            dropped,
        )
        village = await pool.fetchval(
            "SELECT count(*) FROM matches WHERE tournament_id = $1 "
            "AND (SELECT sum(x::float) FROM jsonb_array_elements_text(scores) x) = 5",
            kept,
        )
    finally:
        await pool.close()

    by_t = await load(DSN, [kept, dropped])
    assert by_t[kept].games == n == 4
    assert by_t[kept].village_wins == village
    assert sum(by_t[kept].first_vote.values()) == 4
    assert min(by_t[kept].votes) == NIGHTS_BEFORE_DAY_ONE["werewolf_dayfirst"]
    assert by_t[dropped].games == 0 and not by_t[dropped].votes
