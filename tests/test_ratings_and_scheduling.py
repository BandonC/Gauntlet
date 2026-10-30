from collections import Counter
from itertools import combinations

import pytest

from arena.games.base import placements_from_scores, rank_forfeits_last
from arena.ratings.elo import EloTable, expected_score, update
from arena.tournaments import round_robin, table_schedule


def test_elo_is_zero_sum_and_symmetric():
    a, b = update(1500, 1500, 1.0)
    assert a - 1500 == pytest.approx(1500 - b) == pytest.approx(16.0)
    assert expected_score(1600, 1400) + expected_score(1400, 1600) == pytest.approx(1.0)


def test_elo_table_leaderboard():
    t = EloTable()
    t.record_all([("a", "b", 1.0), ("a", "c", 1.0), ("b", "c", 0.5)])
    board = t.leaderboard()
    assert board[0][0] == "a" and board[0][2] == 2


def test_round_robin_alternates_first_seat():
    s = round_robin(["a", "b", "c"], games_per_pair=4)
    assert len(s) == 3 * 4
    ab = [x for x in s if set(x) == {"a", "b"}]
    assert sum(x[0] == "a" for x in ab) == 2


def test_round_robin_rejects_duplicates():
    with pytest.raises(ValueError):
        round_robin(["a", "a"], 2)


@pytest.mark.parametrize(
    ("scores", "expected"),
    [
        ([1.0, 0.0], [1, 2]),
        ([0.5, 0.5], [1, 1]),
        ([0.0, 0.0, 1.0], [2, 2, 1]),
        ([1.0, 0.0, 1.0, 0.0, 1.0], [1, 4, 1, 4, 1]),  # team game: all winners rank 1
        ([3.0, 2.0, 2.0, 1.0], [1, 2, 2, 4]),
    ],
)
def test_placements_from_scores(scores, expected):
    assert placements_from_scores(scores) == expected


@pytest.mark.parametrize(
    ("placements", "forfeits", "expected"),
    [
        ([1, 2], [], [1, 2]),
        ([2, 1], [0], [2, 1]),
        ([1, 2, 3], [0], [3, 1, 2]),  # a forfeit on the winning side still ranks last
        ([1, 1, 3, 3], [1], [1, 4, 2, 2]),  # the others keep their ties
        ([3, 1, 2, 4], [0, 3], [4, 1, 2, 3]),  # the later forfeit ranks higher
    ],
)
def test_rank_forfeits_last(placements, forfeits, expected):
    assert rank_forfeits_last(placements, forfeits) == expected


# (players, table size, tables). The last group are exact block designs, where every pair
# must meet the same number of times; those are the hard targets for the search.
TABLE_CONFIGS = [
    (8, 3, 2),  # few tables: some players sit out entirely
    (4, 3, 8),
    (5, 2, 10),
    (5, 3, 10),
    (6, 3, 4),
    (6, 3, 20),
    (6, 4, 15),
    (10, 4, 15),
    (12, 4, 30),
    (6, 3, 10),
    (7, 3, 7),
    (8, 4, 14),
    (9, 3, 12),
    (13, 4, 13),
]


@pytest.mark.parametrize(("n", "k", "tables"), TABLE_CONFIGS)
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_table_schedule_balances_pairs_games_and_seats(n, k, tables, seed):
    ids = [f"p{i}" for i in range(n)]
    schedule = table_schedule(ids, k, tables, seed)
    assert len(schedule) == tables
    assert all(len(t) == k and len(set(t)) == k and set(t) <= set(ids) for t in schedule)

    pairs = Counter({frozenset(p): 0 for p in combinations(ids, 2)})
    pairs.update(frozenset(p) for t in schedule for p in combinations(t, 2))
    assert max(pairs.values()) - min(pairs.values()) <= 1

    games = Counter(dict.fromkeys(ids, 0))
    games.update(p for t in schedule for p in t)
    assert max(games.values()) - min(games.values()) <= 1

    for p in ids:
        seats = [sum(t[seat] == p for t in schedule) for seat in range(k)]
        assert max(seats) - min(seats) <= 1, (p, seats)


def test_table_schedule_is_pure():
    ids = [f"p{i}" for i in range(8)]
    assert table_schedule(ids, 4, 14, 7) == table_schedule(ids, 4, 14, 7)
    assert table_schedule(ids, 4, 14, 7) != table_schedule(ids, 4, 14, 8)


def test_table_schedule_keeps_seat_balance_when_pair_balance_is_impossible():
    # 5 tables of 3 from 6 players: 15 pair slots for 15 pairs, so all would have to meet
    # exactly once, and no such design exists on 6 points.
    ids = [f"p{i}" for i in range(6)]
    schedule = table_schedule(ids, 3, 5, 0)
    pairs = Counter(frozenset(p) for t in schedule for p in combinations(t, 2))
    assert max(pairs.values()) == 2  # not perfect, but as even as it gets
    for p in ids:
        seats = [sum(t[seat] == p for t in schedule) for seat in range(3)]
        assert max(seats) - min(seats) <= 1


def test_table_schedule_rejects_bad_input():
    for ids, k, tables in [
        (["a", "a", "b"], 2, 1),
        (["a", "b"], 3, 1),
        (["a", "b"], 1, 1),
        (["a", "b"], 2, 0),
    ]:
        with pytest.raises(ValueError):
            table_schedule(ids, k, tables, 0)
