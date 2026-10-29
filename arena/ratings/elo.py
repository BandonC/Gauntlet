"""Two-player Elo. v1 rating system; Glicko-2 (with rating deviation, so the
leaderboard can show confidence) is a planned replacement in v2."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

DEFAULT_RATING = 1500.0


def expected_score(rating_a: float, rating_b: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))


def update(
    rating_a: float, rating_b: float, score_a: float, k: float = 32.0
) -> tuple[float, float]:
    """score_a: 1 win, 0.5 draw, 0 loss (from A's perspective)."""
    delta = k * (score_a - expected_score(rating_a, rating_b))
    return rating_a + delta, rating_b - delta


@dataclass
class EloTable:
    k: float = 32.0
    ratings: dict[str, float] = field(default_factory=dict)
    games: dict[str, int] = field(default_factory=dict)

    def get(self, player_id: str) -> float:
        return self.ratings.get(player_id, DEFAULT_RATING)

    def record(self, a: str, b: str, score_a: float) -> None:
        ra, rb = update(self.get(a), self.get(b), score_a, self.k)
        self.ratings[a], self.ratings[b] = ra, rb
        self.games[a] = self.games.get(a, 0) + 1
        self.games[b] = self.games.get(b, 0) + 1

    def record_all(self, results: Iterable[tuple[str, str, float]]) -> None:
        for a, b, s in results:
            self.record(a, b, s)

    def leaderboard(self) -> list[tuple[str, float, int]]:
        return sorted(
            ((p, r, self.games.get(p, 0)) for p, r in self.ratings.items()),
            key=lambda x: x[1],
            reverse=True,
        )
