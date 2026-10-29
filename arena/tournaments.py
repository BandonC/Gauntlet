"""Tournament scheduling. Produces seatings only; the queue/worker runs them."""

from __future__ import annotations

from itertools import combinations


def round_robin(player_ids: list[str], games_per_pair: int) -> list[list[str]]:
    """Every pair plays `games_per_pair` games, alternating who takes seat 0
    (moves first), so first-move advantage cancels out."""
    if len(set(player_ids)) != len(player_ids):
        raise ValueError("duplicate player ids")
    seatings: list[list[str]] = []
    for a, b in combinations(player_ids, 2):
        for i in range(games_per_pair):
            seatings.append([a, b] if i % 2 == 0 else [b, a])
    return seatings


# v2: swiss(player_ids, ratings, rounds) -> pair similar ratings to cut match count.
