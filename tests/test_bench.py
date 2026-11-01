"""arena bench: pure helpers, and one real run with real worker processes."""

import os

import asyncpg
import pytest

from arena.bench import (
    BENCH_DB,
    BenchError,
    BenchResult,
    bench_dsn,
    bench_roster,
    format_table,
    percentile,
    percentiles,
    run_bench,
)


def result(workers: int, elapsed: float) -> BenchResult:
    return BenchResult(
        workers=workers,
        matches=5000,
        turns=130000,
        elapsed_s=elapsed,
        queue_wait_s=(1.0, 2.0, 3.0),
        claim_ms=(1.5, 2.5, 3.5),
        max_connections=workers * 2,
        reruns=0,
        deadlocks=0,
    )


def test_bench_dsn_swaps_only_the_database():
    assert (
        bench_dsn("postgresql://arena:arena@localhost:5434/arena?sslmode=disable")
        == "postgresql://arena:arena@localhost:5434/arena_bench?sslmode=disable"
    )


def test_bench_refuses_a_url_that_already_names_its_database():
    with pytest.raises(BenchError):
        bench_dsn(f"postgresql://arena:arena@localhost:5434/{BENCH_DB}")


def test_percentile_is_nearest_rank():
    values = list(range(1, 101))
    assert percentile(values, 0.50) == 50
    assert percentile(values, 0.95) == 95
    assert percentile(values, 0.99) == 99
    assert percentile([7.0], 0.99) == 7.0
    assert percentiles([3.0, 1.0, 2.0]) == (2.0, 3.0, 3.0)


def test_percentile_of_nothing_is_an_error():
    with pytest.raises(ValueError):
        percentile([], 0.5)


def test_bench_players_are_free_bots_with_distinct_identities():
    roster = bench_roster(8)
    assert {p.provider for p in roster.players.values()} == {"bot"}
    assert len({p.seed for p in roster.players.values()}) == 8


def test_table_shows_speed_up_against_the_first_row():
    table = format_table([result(1, 500.0), result(2, 250.0)])
    assert "| 1 | 5,000 | 500.0 | 10.0 | 260 | 1.00x |" in table
    assert "| 2 | 5,000 | 250.0 | 20.0 | 520 | 2.00x |" in table
    assert "1.0/2.0/3.0" in table
    assert "1.5/2.5/3.5" in table


@pytest.mark.db
async def test_real_workers_finish_every_match_in_their_own_database():
    dsn = os.environ["DATABASE_URL"]
    results = await run_bench(dsn, matches=12, workers=[2], players=4)

    r = results[0]
    assert r.matches == 12
    assert r.turns > 12
    assert r.reruns == 0
    assert r.deadlocks == 0
    assert r.elapsed_s > 0
    assert 2 <= r.max_connections <= 6
    assert all(v >= 0 for v in (*r.queue_wait_s, *r.claim_ms))

    conn = await asyncpg.connect(dsn)
    try:
        assert (
            await conn.fetchval("SELECT count(*) FROM pg_database WHERE datname=$1", BENCH_DB) == 0
        )
    finally:
        await conn.close()


@pytest.mark.db
async def test_exact_match_count_when_pairs_do_not_divide_it():
    results = await run_bench(os.environ["DATABASE_URL"], matches=7, workers=[1], players=3)
    assert results[0].matches == 7
