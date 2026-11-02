"""Weng-Lin Plackett-Luce (Weng & Lin, "A Bayesian Approximation Method for Online
Ranking", JMLR 2011, Algorithm 4), the rating that updates on every match at any table
size, so a game has one rating. Elo and Glicko-2 are heads-up-only extras beside it.

`rate_teams` is the model itself: a team's mu and variance are the sums of its members',
and each member takes a share of the update in proportion to their variance. `rate` is the
case where every seat is its own one-player team, with ties (equal ranks) expressing shared
results, which is how Werewolf is rated.

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
    return [team[0] for team in rate_teams([[p] for p in players], ranks)]


def rate_teams(teams: Sequence[Sequence[WengLin]], ranks: Sequence[int]) -> list[list[WengLin]]:
    """New ratings after one match, team by team. ranks: one per team, lower is better."""
    if len(teams) != len(ranks) or len(teams) < 2 or not all(teams):
        raise ValueError("rate_teams needs one rank per team and at least 2 teams")
    mus = [sum(p.mu for p in team) for team in teams]
    variances = [sum(p.sigma * p.sigma for p in team) for team in teams]
    c = math.sqrt(sum(v + BETA * BETA for v in variances))
    strength = [math.exp(mu / c) for mu in mus]
    sides = range(len(teams))
    # sum_q: strength of every team ranked q or worse. a: how many teams share q's rank.
    sum_q = [sum(strength[s] for s in sides if ranks[s] >= ranks[q]) for q in sides]
    a = [sum(r == ranks[q] for r in ranks) for q in sides]

    rated = []
    for i, team in enumerate(teams):
        omega = delta = 0.0
        for q in sides:
            if ranks[q] > ranks[i]:
                continue
            p_iq = strength[i] / sum_q[q]
            delta += p_iq * (1.0 - p_iq) / a[q]
            omega += (1.0 - p_iq if q == i else -p_iq) / a[q]
        variance = variances[i]
        gamma = math.sqrt(variance) / c
        team_mu_step = variance / c * omega
        team_shrink = variance / (c * c) * delta * gamma
        members = []
        for p in team:
            share = p.sigma * p.sigma / variance
            shrink = max(1.0 - share * team_shrink, KAPPA)
            members.append(WengLin(p.mu + share * team_mu_step, p.sigma * math.sqrt(shrink)))
        rated.append(members)
    return rated
