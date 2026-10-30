"""Weng-Lin Plackett-Luce (Weng & Lin, "A Bayesian Approximation Method for Online
Ranking", JMLR 2011, Algorithm 4), the rating that updates on every match at any table
size, so a game has one rating. Elo and Glicko-2 are heads-up-only extras beside it.

Every seat is its own one-player team: `rate` takes one rating and one rank per seat,
and ties (equal ranks) are how shared results are expressed.

There is no additive dynamics term (openskill calls it tau): a player's skill is fixed
by its config_hash, so sigma only ever shrinks. The same reasoning as Glicko-2's RD.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

DEFAULT_MU = 25.0
DEFAULT_SIGMA = DEFAULT_MU / 3
BETA = DEFAULT_MU / 6
KAPPA = 0.0001  # floor on sigma's squared shrink factor
ORDINAL_Z = 3.0


@dataclass(frozen=True)
class WengLin:
    mu: float = DEFAULT_MU
    sigma: float = DEFAULT_SIGMA

    @property
    def ordinal(self) -> float:
        return self.mu - ORDINAL_Z * self.sigma


def rate(players: Sequence[WengLin], ranks: Sequence[int]) -> list[WengLin]:
    """New ratings after one match, in seat order. ranks: lower is better, equal is a
    tie; only their order matters."""
    if len(players) != len(ranks) or len(players) < 2:
        raise ValueError("rate needs one rank per player and at least 2 players")
    c = math.sqrt(sum(p.sigma * p.sigma + BETA * BETA for p in players))
    strength = [math.exp(p.mu / c) for p in players]
    seats = range(len(players))
    # sum_q: strength of everyone ranked q or worse. a: how many seats share q's rank.
    sum_q = [sum(strength[s] for s in seats if ranks[s] >= ranks[q]) for q in seats]
    a = [sum(r == ranks[q] for r in ranks) for q in seats]

    rated = []
    for i, p in enumerate(players):
        omega = delta = 0.0
        for q in seats:
            if ranks[q] > ranks[i]:
                continue
            p_iq = strength[i] / sum_q[q]
            delta += p_iq * (1.0 - p_iq) / a[q]
            omega += (1.0 - p_iq if q == i else -p_iq) / a[q]
        variance = p.sigma * p.sigma
        gamma = p.sigma / c
        shrink = max(1.0 - variance / (c * c) * delta * gamma, KAPPA)
        rated.append(WengLin(p.mu + variance / c * omega, p.sigma * math.sqrt(shrink)))
    return rated
