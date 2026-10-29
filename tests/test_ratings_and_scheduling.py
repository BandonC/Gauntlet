import pytest

from arena.ratings.elo import EloTable, expected_score, update
from arena.tournaments import round_robin


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
