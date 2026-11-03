"""CFR+ for two-player Liar's Dice: tiny variants only, so these stay fast."""

import math

import numpy as np
import pytest

from arena.solvers.liars_dice_cfr import (
    Round,
    bid_of,
    estimate_bytes,
    index_of,
    reachable,
    rolls,
    solve,
)


def test_rolls_cover_every_outcome_with_the_right_odds():
    out, probs = rolls(2, 4)
    assert len(out) == 10 and math.isclose(probs.sum(), 1.0)
    assert probs[out.index((1, 1))] == 1 / 16 and probs[out.index((1, 2))] == 2 / 16


def test_bid_indices_follow_the_raise_order():
    order = [bid_of(i, 4) for i in range(8)]
    assert order == [(1, 1), (1, 2), (1, 3), (1, 4), (2, 1), (2, 2), (2, 3), (2, 4)]
    assert all(index_of(q, f, 4) == i for i, (q, f) in enumerate(order))


def test_rounds_are_solved_after_the_rounds_they_lead_to():
    states = reachable(2)
    assert states[-1] == (2, 2, 0)
    assert set(states) == {(1, 1, 0), (1, 1, 1), (1, 2, 0), (2, 1, 1), (2, 2, 0)}
    assert states.index((1, 1, 1)) < states.index((1, 2, 0))
    assert states.index((1, 1, 0)) < states.index((2, 1, 1))


def naive_best_response(rnd: Round, p: int, u0: np.ndarray) -> float:
    """Plain recursion over histories, one roll at a time: a second implementation to check the
    layered numpy one against."""
    avg = rnd.average(np.arange(rnd.n))
    opp = 1 - p
    nb = rnd.nb

    def value(node: int, depth: int, rp: int, w: np.ndarray) -> float:
        m = rnd.mover(depth)
        total = -math.inf if m == p else 0.0
        for a in range(rnd.a):
            if not rnd.legal[node, a]:
                continue
            if a == nb:
                u = u0[m, rnd.last[node]]
                if p == 0:
                    pay = u[rp, : rnd.nr[1]]
                else:
                    pay = 1.0 - u[: rnd.nr[0], rp]
                ww = w if m == p else w * avg[node, : rnd.nr[opp], a]
                v = float(ww @ pay)
            else:
                ww = w if m == p else w * avg[node, : rnd.nr[opp], a]
                v = value(node | (1 << a), depth + 1, rp, ww)
            total = max(total, v) if m == p else total + v
        return total

    prior_p, prior_o = rnd.rolls[p][1], rnd.rolls[opp][1]
    return sum(prior_p[r] * value(0, 0, r, prior_o.copy()) for r in range(rnd.nr[p]))


@pytest.mark.parametrize(("c0", "c1", "opener"), [(1, 1, 0), (1, 1, 1), (2, 1, 1), (1, 2, 0)])
def test_layered_best_response_matches_plain_recursion(c0, c1, opener):
    rnd = Round(c0, c1, opener, faces=2)
    u0 = rnd.payoffs(lambda s: 0.0 if s[0] == 0 else 1.0 if s[1] == 0 else 0.3 + 0.1 * s[2])
    for _ in range(7):  # a strategy that's neither uniform nor solved
        rnd.iterate(u0)
    for p in (0, 1):
        assert math.isclose(rnd.value(p, u0, "br"), naive_best_response(rnd, p, u0), abs_tol=1e-12)


def test_cfr_plus_drives_exploitability_towards_zero():
    sol = solve(dice=1, faces=3, iterations=300, sub_iterations=300, threshold=0.0, check_every=50)
    curve = [e for _, e in sol.curve]
    assert curve[0] > curve[-1] and curve[-1] < 1e-4
    assert all(e >= -1e-12 for e in curve)  # a best response never loses to the solution


def test_the_whole_game_composes_rounds_and_stays_a_probability():
    sol = solve(dice=2, faces=2, iterations=200, sub_iterations=200, threshold=0.0, check_every=100)
    assert all(0.0 <= v <= 1.0 for v in sol.eq.values())
    # A best responder in either seat does at least as well as the solution itself.
    assert sol.best_response(0) >= sol.eq[(2, 2, 0)] - 1e-9
    assert sol.best_response(1) >= 1 - sol.eq[(2, 2, 0)] - 1e-9
    assert sol.exploitability() < 0.002


def test_the_real_variant_fits_the_memory_limit():
    assert estimate_bytes(2, 2, 4) < 400 * 2**20
    assert estimate_bytes(3, 3, 6) > 2**30  # the reason the full game isn't solved here
