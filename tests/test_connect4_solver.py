"""The Connect 4 solver against exhaustive minimax, where exhaustive search is affordable."""

import random
from functools import cache

import pytest

from arena.solvers.connect4 import Solver, grade


def random_columns(rng, width, height, plies):
    """A random legal position with no four in a row yet, as owner lists per column."""
    solver = Solver(width, height)
    while True:
        columns = [[] for _ in range(width)]
        for i in range(plies):
            open_cols = [c for c in range(width) if len(columns[c]) < height]
            columns[rng.choice(open_cols)].append(i % 2)
        current, mask, _ = solver.from_columns(columns)
        if not solver.is_win(current) and not solver.is_win(current ^ mask):
            return columns


def minimax(width, height):
    """Plain negamax over every move, same score convention. Memoised so it finishes, and kept
    to boards and depths where the cache stays small: a 5 by 5 board from 10 plies filled
    several gigabytes and was killed for memory."""
    solver = Solver(width, height)
    cells = width * height

    @cache
    def value(current, mask, moves):
        if moves == cells:
            return 0
        best = -cells
        for col in range(width):
            if not solver.can_play(mask, col):
                continue
            move = (mask + solver.bottom_of(col)) & solver.column_mask(col)
            if solver.is_win(current | move):
                return (cells + 1 - moves) // 2
            best = max(best, -value(current ^ mask, mask | move, moves + 1))
        return best

    return solver, value


@pytest.mark.parametrize(("width", "height", "plies"), [(4, 4, 4), (5, 4, 8), (4, 5, 8)])
def test_matches_exhaustive_minimax_on_small_boards(width, height, plies):
    rng = random.Random(width * 100 + height)
    reference, value = minimax(width, height)
    for _ in range(25):
        columns = random_columns(rng, width, height, plies)
        current, mask, moves = reference.from_columns(columns)
        solver = Solver(width, height)
        assert solver.solve(current, mask, moves) == value(current, mask, moves)


def test_matches_exhaustive_minimax_late_on_the_real_board():
    rng = random.Random(7)
    reference, value = minimax(7, 6)
    for _ in range(15):
        columns = random_columns(rng, 7, 6, 32)
        current, mask, moves = reference.from_columns(columns)
        assert Solver().solve(current, mask, moves) == value(current, mask, moves)


def test_every_move_score_matches_minimax():
    """One solver for every position, as the grader runs it: a transposition table reused
    across solves once returned wrong scores here (a cached lower bound reaching beta went on
    searching with an inverted window), and so did Python flooring -(n) // 2."""
    rng = random.Random(11)
    reference, value = minimax(5, 4)
    solver = Solver(5, 4)
    for _ in range(40):
        columns = random_columns(rng, 5, 4, rng.randrange(6, 12))
        current, mask, moves = reference.from_columns(columns)
        scores = solver.move_scores(current, mask, moves)
        for col, score in scores.items():
            move = (mask + reference.bottom_of(col)) & reference.column_mask(col)
            if reference.is_win(current | move):
                expected = (20 + 1 - moves) // 2
            else:
                expected = -value(current ^ mask, mask | move, moves + 1)
            assert score == expected


def test_a_win_in_one_scores_highest_and_a_missed_block_loses():
    solver = Solver()
    # Seat 0 has three in column 0; seat 1 has three in column 1; seat 0 to move.
    columns = [[0, 0, 0], [1, 1, 1], [], [], [], [], []]
    current, mask, moves = solver.from_columns(columns)
    assert solver.solve(current, mask, moves) == (42 + 1 - 6) // 2
    # Columns 2-6 neither win nor block, so seat 1 wins next move. Column 1 blocks and leads
    # to a long opening solve, so it is left out: these tests must stay fast.
    for col in range(2, 7):
        move = (mask + solver.bottom_of(col)) & solver.column_mask(col)
        after = -solver.solve(current ^ mask, mask | move, moves + 1)
        assert after == -((42 + 1 - 7) // 2)


def test_full_columns_are_skipped():
    # Late in the game, so the solve is instant, with column 0 full. A full solve from an
    # opening position takes minutes here and filled a gigabyte of transposition table.
    solver = Solver()
    rng = random.Random(3)
    while True:
        columns = random_columns(rng, 7, 6, 32)
        if len(columns[0]) == 6 and len(columns[6]) < 6:
            break
    scores = solver.move_scores(*solver.from_columns(columns))
    assert 0 not in scores
    assert set(scores) == {c for c in range(7) if len(columns[c]) < 6}


def test_a_timeout_is_raised_not_guessed():
    from arena.solvers.connect4 import SolveTimeout

    solver = Solver()
    with pytest.raises(SolveTimeout):
        solver.solve(*solver.from_columns([[] for _ in range(7)]), time_limit=0.2)


@pytest.mark.parametrize(
    ("best", "played", "label"),
    [
        (5, 5, "optimal"),
        (5, 3, "inaccuracy"),
        (5, 0, "blunder"),
        (5, -2, "blunder"),
        (0, -1, "blunder"),
        (0, 0, "optimal"),
        (-3, -5, "inaccuracy"),
        (-3, -3, "optimal"),
    ],
)
def test_grades(best, played, label):
    assert grade(best, played) == label
