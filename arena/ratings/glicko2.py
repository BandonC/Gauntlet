"""Glicko-2 (Glickman, "Example of the Glicko-2 system", 2013). Runs beside Elo as a
post-v1 addition: Elo stays the v1 rating; Glicko-2 adds a rating deviation (RD) so the
leaderboard can show confidence.

We treat every match as its own rating period and never apply the "no games played"
RD inflation: a player's skill is fixed by its config_hash, so inactivity doesn't make
us less sure of it. For the same reason the default volatility constraint TAU is low.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

SCALE = 173.7178  # Glicko-2 <-> Glicko rating scale factor
DEFAULT_RATING = 1500.0
DEFAULT_RD = 350.0
DEFAULT_VOL = 0.06
TAU = 0.3
EPSILON = 1e-6


@dataclass(frozen=True)
class Glicko:
    rating: float = DEFAULT_RATING
    rd: float = DEFAULT_RD
    vol: float = DEFAULT_VOL


def _g(phi: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))


def _e(mu: float, mu_j: float, phi_j: float) -> float:
    return 1.0 / (1.0 + math.exp(-_g(phi_j) * (mu - mu_j)))


def update(
    player: Glicko,
    results: Sequence[tuple[Glicko, float]],
    tau: float = TAU,
    epsilon: float = EPSILON,
) -> Glicko:
    """New rating after one rating period. results: (opponent's pre-period rating,
    score) with score 1 win, 0.5 draw, 0 loss. An empty period changes nothing."""
    if not results:
        return player
    # Step 2: convert to the Glicko-2 scale.
    mu = (player.rating - DEFAULT_RATING) / SCALE
    phi = player.rd / SCALE
    sigma = player.vol
    opps = [((o.rating - DEFAULT_RATING) / SCALE, o.rd / SCALE, s) for o, s in results]

    # Steps 3-4: estimated variance v and improvement delta.
    v_inv = 0.0
    delta_sum = 0.0
    for mu_j, phi_j, s in opps:
        g, e = _g(phi_j), _e(mu, mu_j, phi_j)
        v_inv += g * g * e * (1.0 - e)
        delta_sum += g * (s - e)
    v = 1.0 / v_inv
    delta = v * delta_sum

    # Step 5: new volatility (Illinois algorithm).
    a = math.log(sigma * sigma)

    def f(x: float) -> float:
        ex = math.exp(x)
        num = ex * (delta * delta - phi * phi - v - ex)
        den = 2.0 * (phi * phi + v + ex) ** 2
        return num / den - (x - a) / (tau * tau)

    big_a = a
    if delta * delta > phi * phi + v:
        big_b = math.log(delta * delta - phi * phi - v)
    else:
        k = 1
        while f(a - k * tau) < 0:
            k += 1
        big_b = a - k * tau
    f_a, f_b = f(big_a), f(big_b)
    while abs(big_b - big_a) > epsilon:
        c = big_a + (big_a - big_b) * f_a / (f_b - f_a)
        f_c = f(c)
        if f_c * f_b <= 0:
            big_a, f_a = big_b, f_b
        else:
            f_a /= 2.0
        big_b, f_b = c, f_c
    new_sigma = math.exp(big_a / 2.0)

    # Steps 6-8: new RD and rating, back on the Glicko scale.
    phi_star = math.sqrt(phi * phi + new_sigma * new_sigma)
    new_phi = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)
    new_mu = mu + new_phi * new_phi * delta_sum
    return Glicko(SCALE * new_mu + DEFAULT_RATING, SCALE * new_phi, new_sigma)


def update_pair(a: Glicko, b: Glicko, score_a: float, tau: float = TAU) -> tuple[Glicko, Glicko]:
    """One two-player match as a rating period: both sides use pre-match ratings."""
    return update(a, [(b, score_a)], tau), update(b, [(a, 1.0 - score_a)], tau)
