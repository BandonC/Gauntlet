"""Bradley-Terry: the "final rating" of a finished tournament. A maximum-likelihood fit to
all of its results at once, so unlike the per-match live ratings it can't depend on the
order games were played in: the same results always give the same ratings.

Player i beats j with probability exp(t_i) / (exp(t_i) + exp(t_j)). A draw counts as half a
win each way. The fit uses Hunter's MM algorithm (2004). The 95% intervals come from the
Fisher information with the ratings constrained to average 1500, so they say how far a
player is from the field: ratings mean nothing across tournaments, and two players'
intervals overlapping is not a test of the gap between them.

Heads-up only: a table of N seats isn't N-1 independent games. The MLE is finite only
when every player can be linked to every other by a chain of wins (the win graph is
strongly connected); an unbeaten or winless player has no finite estimate, so `fit`
returns None rather than inventing one with a prior.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass

RATING_SCALE = 400 / math.log(10)  # the Elo scale: 400 points is 10:1 odds
MEAN_RATING = 1500.0
Z_95 = 1.959963984540054
TOLERANCE = 1e-11
MAX_ITERATIONS = 200_000


@dataclass(frozen=True)
class FinalRating:
    rating: float
    low: float
    high: float


def fit(results: Iterable[tuple[str, str, float]]) -> dict[str, FinalRating] | None:
    """Ratings from (player a, player b, a's score) with score 1 win, 0.5 draw, 0 loss.
    None if there are no results, or no finite estimate exists."""
    rows = list(results)
    players = sorted({p for a, b, _ in rows for p in (a, b)})
    index = {p: i for i, p in enumerate(players)}
    k = len(players)
    wins = [[0.0] * k for _ in range(k)]
    for a, b, score in rows:
        wins[index[a]][index[b]] += score
        wins[index[b]][index[a]] += 1.0 - score
    if k < 2 or not _strongly_connected(wins):
        return None

    games = [[wins[i][j] + wins[j][i] for j in range(k)] for i in range(k)]
    theta = _maximise(wins, games)
    p = [[math.exp(ti) / (math.exp(ti) + math.exp(tj)) for tj in theta] for ti in theta]
    info = [
        [
            sum(games[i][m] * p[i][m] * p[m][i] for m in range(k) if m != i)
            if i == j
            else -games[i][j] * p[i][j] * p[j][i]
            for j in range(k)
        ]
        for i in range(k)
    ]
    # info is singular (only differences are identified). Adding 1/k to every cell makes
    # it invertible, and subtracting 1/k after gives the covariance with mean(theta) = 0.
    inverse = _invert([[info[i][j] + 1 / k for j in range(k)] for i in range(k)])
    out = {}
    for i, player in enumerate(players):
        rating = MEAN_RATING + RATING_SCALE * theta[i]
        margin = Z_95 * RATING_SCALE * math.sqrt(inverse[i][i] - 1 / k)
        out[player] = FinalRating(rating, rating - margin, rating + margin)
    return out


def _maximise(wins: list[list[float]], games: list[list[float]]) -> list[float]:
    k = len(wins)
    strength = [1.0] * k
    for _ in range(MAX_ITERATIONS):
        next_strength = [
            sum(wins[i])
            / sum(games[i][j] / (strength[i] + strength[j]) for j in range(k) if j != i)
            for i in range(k)
        ]
        centre = sum(math.log(s) for s in next_strength) / k
        next_strength = [s / math.exp(centre) for s in next_strength]
        moved = max(abs(math.log(a / b)) for a, b in zip(next_strength, strength, strict=True))
        strength = next_strength
        if moved < TOLERANCE:
            break
    return [math.log(s) for s in strength]


def _strongly_connected(wins: list[list[float]]) -> bool:
    k = len(wins)

    def reach(edge: Callable[[int, int], float]) -> set[int]:
        seen, todo = {0}, [0]
        while todo:
            i = todo.pop()
            for j in range(k):
                if j not in seen and edge(i, j) > 0:
                    seen.add(j)
                    todo.append(j)
        return seen

    forward = reach(lambda i, j: wins[i][j])
    backward = reach(lambda i, j: wins[j][i])
    return len(forward) == k and len(backward) == k


def _invert(matrix: list[list[float]]) -> list[list[float]]:
    """Gauss-Jordan with partial pivoting. No numpy in core modules."""
    k = len(matrix)
    a = [row[:] + [float(i == j) for j in range(k)] for i, row in enumerate(matrix)]
    for col in range(k):
        pivot = max(range(col, k), key=lambda r: abs(a[r][col]))
        a[col], a[pivot] = a[pivot], a[col]
        head = a[col][col]
        a[col] = [x / head for x in a[col]]
        for r in range(k):
            if r != col and a[r][col]:
                factor = a[r][col]
                a[r] = [x - factor * y for x, y in zip(a[r], a[col], strict=True)]
    return [row[k:] for row in a]
