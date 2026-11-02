"""arena chaos. The checker must catch each kind of damage, or a pass proves nothing."""

import asyncio
import os
import shutil

import asyncpg
import pytest

from arena.bench import (
    BenchError,
    bench_dsn,
    bench_roster,
    drop_database,
    queue_matches,
    reset_database,
)
from arena.chaos import (
    CHAOS_PRICE,
    CHAOS_USAGE,
    ChaosFactory,
    ChaosReport,
    PricedBot,
    check_invariants,
    find_container,
    format_report,
    run_chaos,
)
from arena.store.db import create_pool
from arena.store.spend import TOTAL_SPEND_SQL
from arena.worker import WorkerConfig, run_worker

FIRST_MATCH = "(SELECT min(id) FROM matches)"


def report(violations):
    return ChaosReport(10, 200, 3, 1, 2.5, 2, 0, 3, 4, 0.3, violations)


def test_report_says_when_invariants_held():
    assert "all invariants held" in format_report(report([]))


def test_report_lists_violations():
    text = format_report(report(["match 1 is failed"]))
    assert "1 VIOLATIONS" in text
    assert "match 1 is failed" in text


def test_chaos_players_bill_something():
    assert CHAOS_PRICE.cost(CHAOS_USAGE) == pytest.approx(0.0015)
    player, price = ChaosFactory(bench_roster(2), {}).build("bench-bot-0")
    assert isinstance(player, PricedBot)
    assert price == CHAOS_PRICE


@pytest.fixture(params=["exact", "sharded"])
async def clean_run(request):
    """A small undamaged run in arena_bench, finished by one in-process worker."""
    admin = os.environ["DATABASE_URL"]
    dsn = bench_dsn(admin)
    await reset_database(admin)
    await queue_matches(dsn, 6, 3, request.param)
    pool = await create_pool(dsn, max_size=4)
    await run_worker(pool, ChaosFactory(bench_roster(3), {}), WorkerConfig(), exit_when_idle=True)
    yield pool
    await pool.close()
    await drop_database(admin)


@pytest.mark.db
async def test_clean_run_has_no_violations_and_real_spend(clean_run):
    assert await check_invariants(clean_run, 6) == []
    assert await clean_run.fetchval(f"SELECT {TOTAL_SPEND_SQL} FROM tournaments t") > 0


TAMPERED = [
    (
        "unfinished match",
        f"UPDATE matches SET status = 'failed' WHERE id = {FIRST_MATCH}",
        "failed",
    ),
    (
        "gap in a turn log",
        f"DELETE FROM turns WHERE turn_no = 2 AND match_id = {FIRST_MATCH}",
        "isn't continuous",
    ),
    (
        "log cut short",
        f"DELETE FROM turns WHERE match_id = {FIRST_MATCH} AND turn_no = "
        f"(SELECT max(turn_no) FROM turns WHERE match_id = {FIRST_MATCH})",
        "stops before the end",
    ),
    (
        "stored scores differ from the replay",
        f"UPDATE matches SET scores = '[0.5, 0.5]' WHERE id = {FIRST_MATCH}",
        "replays to",
    ),
    (
        "move changed after the fact",
        f"UPDATE turns SET seat = 1 - seat WHERE turn_no = 0 AND match_id = {FIRST_MATCH}",
        "doesn't replay",
    ),
    (
        "lost rating",
        "DELETE FROM rating_history WHERE id = (SELECT min(id) FROM rating_history)",
        "rated 0 times",
    ),
    (
        "counted twice in games_played",
        "UPDATE ratings SET games_played = games_played + 1 "
        "WHERE player_id = (SELECT min(player_id) FROM ratings)",
        "games_played",
    ),
    ("spend drift", "UPDATE tournaments SET spent_usd = spent_usd + 0.000001", "turns cost"),
    (
        "stray shard",
        "INSERT INTO tournament_spend_shards (tournament_id, worker_id, spent_usd) "
        "SELECT id, 'ghost', 0.000001 FROM tournaments",
        "ghost",
    ),
    (
        "unpaid turn",
        f"UPDATE turns SET cost_usd = 0 WHERE turn_no = 0 AND match_id = {FIRST_MATCH}",
        "turns cost",
    ),
    ("tournament left open", "UPDATE tournaments SET status = 'running'", "is running"),
    (
        "held lease",
        f"UPDATE matches SET lease_owner = 'ghost' WHERE id = {FIRST_MATCH}",
        "lease still held",
    ),
]


@pytest.mark.db
@pytest.mark.parametrize(
    ("sql", "expected"), [t[1:] for t in TAMPERED], ids=[t[0] for t in TAMPERED]
)
async def test_checker_catches_damage(clean_run, sql, expected):
    await clean_run.execute(sql)
    violations = await check_invariants(clean_run, 6)
    assert any(expected in v for v in violations), violations


@pytest.mark.db
async def test_checker_catches_a_missing_match(clean_run):
    violations = await check_invariants(clean_run, 7)
    assert any("expected 7" in v for v in violations), violations


@pytest.mark.db
async def test_second_rating_row_is_refused_by_the_schema(clean_run):
    """The checker can't be shown two rows for a seat: the database won't hold them."""
    with pytest.raises(asyncpg.UniqueViolationError):
        await clean_run.execute(
            "INSERT INTO rating_history (player_id, game, match_id, rating_before, rating_after) "
            "SELECT player_id, game, match_id, rating_before, rating_after "
            "FROM rating_history LIMIT 1"
        )


@pytest.mark.db
async def test_only_this_repos_compose_postgres_can_be_paused():
    with pytest.raises(BenchError):
        await find_container("postgresql://arena:arena@localhost:54322/postgres")


@pytest.mark.slow
@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs DATABASE_URL")
@pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker")
@pytest.mark.parametrize("spend_mode", ["exact", "sharded"])
async def test_short_chaos_run_holds_every_invariant(spend_mode):
    result = await asyncio.wait_for(
        run_chaos(
            os.environ["DATABASE_URL"],
            matches=500,
            workers=3,
            players=4,
            mean_gap=0.4,
            pause_chance=0.25,
            stop_chance=0.25,
            pause_seconds=(1.0, 5.0),
            spend_mode=spend_mode,
            time_limit=300.0,
        ),
        timeout=360.0,
    )
    print(format_report(result))
    assert result.violations == [], format_report(result)
    assert result.kills >= 1
    assert result.pauses >= 1
    assert result.stops >= 1
    assert result.reclaimed >= 1


@pytest.mark.slow
@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs DATABASE_URL")
@pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker")
async def test_graceful_stops_hand_matches_over_without_waiting_for_a_lease():
    """Only graceful stops. Every stop must exit cleanly, a match stopped mid-game must
    carry on under another worker, and no stop may cost a run attempt: an unreleased
    lease would expire and be re-claimed, which `reclaimed` would show."""
    result = await asyncio.wait_for(
        run_chaos(
            os.environ["DATABASE_URL"],
            matches=60,
            workers=2,
            players=4,
            mean_gap=0.4,
            pause_chance=0.0,
            stop_chance=1.0,
            time_limit=120.0,
        ),
        timeout=150.0,
    )
    assert result.violations == [], format_report(result)
    assert result.stops >= 1
    assert result.kills == 0
    assert result.handed_over >= 1
    assert result.reclaimed == 0
