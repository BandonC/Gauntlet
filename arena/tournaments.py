"""Tournament scheduling. Produces seatings only; the queue/worker runs them."""

from __future__ import annotations

import math
from collections.abc import Collection
from dataclasses import dataclass
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


# --- Swiss ------------------------------------------------------------------------
# Each pairing plays GAMES_PER_PAIRING games with seats swapped (first-move advantage
# cancels, as in round_robin). A bye is worth BYE_POINTS: the same as winning both games
# of a pairing. Byes are not matches, so they never touch Elo or Glicko-2.
GAMES_PER_PAIRING = 2
BYE_POINTS = 2.0


@dataclass(frozen=True)
class SwissEntrant:
    player_id: str
    points: float  # tournament points so far, byes included
    rating: float  # tie-break and round-1 seed: conservative Glicko (rating - 2*RD)
    had_bye: bool = False


def default_swiss_rounds(n_players: int) -> int:
    return max(1, math.ceil(math.log2(n_players)))


def standings(entrants: list[SwissEntrant]) -> list[SwissEntrant]:
    """Points, then rating, then id: a total order, so pairing is deterministic."""
    return sorted(entrants, key=lambda e: (-e.points, -e.rating, e.player_id))


def swiss_pairings(
    entrants: list[SwissEntrant],
    played: Collection[frozenset[str]],
    first_round: bool = False,
) -> tuple[list[tuple[str, str]], str | None]:
    """Pair one round. Returns ((higher, lower) pairs in standings order, bye or None).

    - Odd field: the lowest-ranked entrant without a bye sits out (lowest overall if
      everyone has had one).
    - Round 1: top half of the seeding plays the bottom half (1 v N/2+1, ...).
    - Later rounds: each entrant plays the nearest-ranked opponent it hasn't met;
      backtracks if that strands someone. Rematches only if no rematch-free pairing
      of the whole field exists.
    """
    if len({e.player_id for e in entrants}) != len(entrants):
        raise ValueError("duplicate player ids")
    order = standings(entrants)
    bye = None
    if len(order) % 2:
        fresh = [e for e in order if not e.had_bye]
        bye = (fresh or order)[-1].player_id
        order = [e for e in order if e.player_id != bye]
    ids = [e.player_id for e in order]
    if first_round:
        half = len(ids) // 2
        return list(zip(ids[:half], ids[half:], strict=True)), bye
    pairs = _pair_avoiding(ids, set(played)) or _pair_avoiding(ids, set())
    assert pairs is not None  # with no constraints, adjacent pairing always works
    return pairs, bye


def _pair_avoiding(ids: list[str], played: set[frozenset[str]]) -> list[tuple[str, str]] | None:
    if not ids:
        return []
    first, rest = ids[0], ids[1:]
    for i, opp in enumerate(rest):
        if frozenset((first, opp)) in played:
            continue
        tail = _pair_avoiding(rest[:i] + rest[i + 1 :], played)
        if tail is not None:
            return [(first, opp), *tail]
    return None


def swiss_seatings(pairs: list[tuple[str, str]]) -> list[list[str]]:
    """GAMES_PER_PAIRING games per pairing, alternating who takes seat 0."""
    return [[a, b] if i % 2 == 0 else [b, a] for a, b in pairs for i in range(GAMES_PER_PAIRING)]
