"""Grading recorded Connect 4 moves. Time limits stay tiny: early positions can't solve fast."""

from arena.solvers.c4_grade import OPENING_SCORES, MoveGrade, grade_moves, summarize


def test_first_moves_come_from_theory():
    assert grade_moves([3])[0].grade == "optimal"
    assert grade_moves([2])[0].grade == "blunder"  # a win thrown to a draw
    assert grade_moves([0])[0].grade == "blunder"
    assert grade_moves([0])[0].source == "theory"
    assert OPENING_SCORES.index(max(OPENING_SCORES)) == 3


def test_missing_a_win_in_one_is_a_blunder_and_taking_it_is_optimal():
    # X stacks column 0, O stacks column 1; at ply 6 X has three and wins by playing 0.
    early = [0, 1, 0, 1, 0, 1]
    took = grade_moves([*early, 0], time_limit=0.1)[-1]
    assert (took.grade, took.best, took.played) == ("optimal", 18, 18)
    missed = grade_moves([*early, 5], time_limit=0.1)[-1]
    assert missed.grade == "blunder" and missed.played < 0


def test_a_slow_position_is_unsolved_not_guessed():
    g = grade_moves([3, 3], time_limit=0.05)[1]
    assert g.grade == "unsolved" and g.best is None and g.source == "timeout"


def test_summarize_counts_per_player():
    g = MoveGrade(0, 0, 3, "optimal", 1, 1, "theory")
    b = MoveGrade(1, 1, 0, "blunder", 1, -1, "search")
    assert summarize([("a", g), ("a", b), ("b", b)]) == {
        "a": {"optimal": 1, "blunder": 1},
        "b": {"blunder": 1},
    }
