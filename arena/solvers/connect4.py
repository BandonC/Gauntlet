"""Perfect-play Connect 4: bitboard negamax with alpha-beta, a transposition table, threat-based
move ordering and a null-window search. After Pascal Pons's solver design.

Scores follow the same convention: a position is worth (cells left + 1) // 2 to the player who
can force a win, so a faster win scores higher; 0 is a draw; a forced loss is negative.

Board size is a parameter so tests can check the solver against plain minimax on boards small
enough to search exhaustively. The arena's game is 7 wide and 6 tall.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field


class SolveTimeout(Exception):
    """The position needs more search than its time budget allowed."""


@dataclass
class Solver:
    width: int = 7
    height: int = 6
    table: dict[tuple[int, int], tuple[int, int]] = field(default_factory=dict)
    nodes: int = 0

    def __post_init__(self) -> None:
        h1 = self.height + 1
        self.bottom = sum(1 << (c * h1) for c in range(self.width))
        self.board = self.bottom * ((1 << self.height) - 1)
        self.order = sorted(range(self.width), key=lambda c: (abs(2 * c - (self.width - 1)), c))
        self.cells = self.width * self.height
        self.deadline: float | None = None

    # --- positions -------------------------------------------------------------------

    def from_columns(self, columns: Sequence[Sequence[int]]) -> tuple[int, int, int]:
        """(current, mask, moves) for the player to move, from a column list of 0/1 owners.
        The player to move is whoever placed fewer stones (seat 0 moves first)."""
        h1 = self.height + 1
        moves = sum(len(c) for c in columns)
        me = moves % 2
        current = mask = 0
        for c, column in enumerate(columns):
            for r, owner in enumerate(column):
                bit = 1 << (c * h1 + r)
                mask |= bit
                if owner == me:
                    current |= bit
        return current, mask, moves

    def column_mask(self, col: int) -> int:
        h1 = self.height + 1
        return ((1 << self.height) - 1) << (col * h1)

    def top(self, col: int) -> int:
        return 1 << (self.height - 1 + col * (self.height + 1))

    def bottom_of(self, col: int) -> int:
        return 1 << (col * (self.height + 1))

    def can_play(self, mask: int, col: int) -> bool:
        return mask & self.top(col) == 0

    def possible(self, mask: int) -> int:
        return (mask + self.bottom) & self.board

    def winning_cells(self, position: int, mask: int) -> int:
        """Empty cells where `position` would complete four."""
        h = self.height
        r = (position << 1) & (position << 2) & (position << 3)  # vertical
        for step in (h + 1, h, h + 2):  # horizontal and both diagonals
            p = (position << step) & (position << 2 * step)
            r |= p & (position << 3 * step)
            r |= p & (position >> step)
            p = (position >> step) & (position >> 2 * step)
            r |= p & (position << step)
            r |= p & (position >> 3 * step)
        return r & (self.board ^ mask)

    def is_win(self, position: int) -> bool:
        h = self.height
        for step in (1, h + 1, h, h + 2):
            m = position & (position >> step)
            if m & (m >> 2 * step):
                return True
        return False

    # --- search ----------------------------------------------------------------------

    def solve(self, current: int, mask: int, moves: int, time_limit: float | None = None) -> int:
        """Exact score for the player to move. Raises SolveTimeout past `time_limit` seconds."""
        self.deadline = None if time_limit is None else time.monotonic() + time_limit
        if self.winning_cells(current, mask) & self.possible(mask):
            return (self.cells + 1 - moves) // 2
        lo = -((self.cells - moves) // 2)
        hi = (self.cells + 1 - moves) // 2
        while lo < hi:
            mid = lo + (hi - lo) // 2
            if mid <= 0 and lo // 2 < mid:
                mid = lo // 2
            elif mid >= 0 and hi // 2 > mid:
                mid = hi // 2
            r = self._negamax(current, mask, moves, mid, mid + 1)
            if r <= mid:
                hi = r
            else:
                lo = r
        return lo

    def _negamax(self, current: int, mask: int, moves: int, alpha: int, beta: int) -> int:
        self.nodes += 1
        if (
            self.deadline is not None
            and self.nodes & 4095 == 0
            and time.monotonic() > self.deadline
        ):
            raise SolveTimeout
        opponent = current ^ mask
        possible = self.possible(mask)
        opp_wins = self.winning_cells(opponent, mask)
        forced = possible & opp_wins
        if forced:
            if forced & (forced - 1):
                return -((self.cells - moves) // 2)  # two threats: can't stop both
            possible = forced
        safe = possible & ~(opp_wins >> 1)  # never play under the opponent's winning cell
        if not safe:
            return -((self.cells - moves) // 2)
        if moves >= self.cells - 2:
            return 0
        # Parenthesised on purpose: Python's // floors negatives, where the C++ original
        # truncates, and -(n) // 2 would make an odd loss one point deeper than it is.
        low = -((self.cells - 2 - moves) // 2)
        if alpha < low:
            alpha = low
            if alpha >= beta:
                return alpha
        high = (self.cells - 1 - moves) // 2
        key = (current, mask)
        bound = self.table.get(key)
        if bound is not None:
            kind, value = bound
            if kind > 0 and value < high:  # upper bound
                high = value
            elif kind < 0 and value > low:  # lower bound
                low = value
                if alpha < low:
                    alpha = low
                    if alpha >= beta:
                        return alpha
        if beta > high:
            beta = high
            if alpha >= beta:
                return beta
        candidates = []
        for col in self.order:
            move = safe & self.column_mask(col)
            if move:
                threats = self.winning_cells(current | move, mask | move)
                candidates.append((-bin(threats).count("1"), self.order.index(col), move))
        candidates.sort()
        for _, _, move in candidates:
            nxt = current ^ mask  # the opponent becomes the player to move
            score = -self._negamax(nxt, mask | move, moves + 1, -beta, -alpha)
            if score >= beta:
                self.table[key] = (-1, score)
                return score
            if score > alpha:
                alpha = score
        self.table[key] = (1, alpha)
        return alpha

    def move_scores(
        self, current: int, mask: int, moves: int, time_limit: float | None = None
    ) -> dict[int, int]:
        """Exact score of every legal column for the player to move."""
        scores = {}
        for col in range(self.width):
            if not self.can_play(mask, col):
                continue
            move = (mask + self.bottom_of(col)) & self.column_mask(col)
            if self.is_win(current | move):
                scores[col] = (self.cells + 1 - moves) // 2
                continue
            opponent, new_mask = current ^ mask, mask | move
            scores[col] = -self.solve(opponent, new_mask, moves + 1, time_limit)
        return scores


def grade(best: int, played: int) -> str:
    """Optimal keeps the best score. A blunder changes the result (a win thrown to a draw or
    loss, or a draw to a loss). Anything else is an inaccuracy: same result, slower or
    longer-resisting."""
    if played == best:
        return "optimal"
    sign = (best > 0) - (best < 0)
    if (played > 0) - (played < 0) != sign:
        return "blunder"
    return "inaccuracy"
