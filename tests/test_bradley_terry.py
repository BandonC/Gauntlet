"""The MLE is checked against closed forms and its own defining property (the score
equations: at the maximum, each player's expected wins equal its actual wins)."""

import math
import random

import pytest

from arena.ratings.bradley_terry import MEAN_RATING, RATING_SCALE, Z_95, fit


def series(a, b, a_wins, b_wins, draws=0):
    return [(a, b, 1.0)] * a_wins + [(a, b, 0.0)] * b_wins + [(a, b, 0.5)] * draws


# Tournament 4's real results: the Haikus split 5-5, and the bot beat each of them 8-2.
LIARS_DICE = series("h45", "h55", 5, 5) + series("bot", "h55", 8, 2) + series("bot", "h45", 8, 2)


def test_two_players_match_the_closed_form():
    out = fit(series("a", "b", 8, 2))
    gap = RATING_SCALE * math.log(4)  # 8:2 is odds of 4
    assert out["a"].rating - out["b"].rating == pytest.approx(gap, abs=1e-6)
    assert out["a"].rating + out["b"].rating == pytest.approx(2 * MEAN_RATING)
    # Var(difference) = 1 / (n p q), and each player is half the difference from the mean.
    se_each = RATING_SCALE * math.sqrt(1 / (10 * 0.8 * 0.2)) / 2
    assert out["a"].high - out["a"].rating == pytest.approx(Z_95 * se_each, rel=1e-6)
    assert out["a"].rating - out["a"].low == pytest.approx(Z_95 * se_each, rel=1e-6)


def test_tournament_four_puts_the_haikus_level_and_the_bot_four_to_one_above():
    out = fit(LIARS_DICE)
    assert out["h45"].rating == pytest.approx(out["h55"].rating, abs=1e-6)
    # Haiku's 7 wins = 5 of 10 against the other Haiku + 10p, so p(beat the bot) = 0.2.
    assert out["bot"].rating - out["h45"].rating == pytest.approx(
        RATING_SCALE * math.log(4), abs=1e-6
    )


def test_the_order_of_results_cannot_matter():
    reference = fit(LIARS_DICE)
    for seed in range(20):
        shuffled = list(LIARS_DICE)
        random.Random(seed).shuffle(shuffled)
        assert fit(shuffled) == reference


def test_which_side_a_result_is_written_from_cannot_matter():
    flipped = fit([(b, a, 1.0 - s) for a, b, s in LIARS_DICE])
    for player, r in fit(LIARS_DICE).items():
        assert flipped[player].rating == pytest.approx(r.rating)


def test_expected_wins_equal_actual_wins_at_the_maximum():
    rng = random.Random(11)
    players = [f"p{i}" for i in range(6)]
    results = []
    strength = {p: i * 0.5 for i, p in enumerate(players)}
    for i, a in enumerate(players):
        for b in players[i + 1 :]:
            for _ in range(12):
                p = 1 / (1 + math.exp(strength[b] - strength[a]))
                results.append((a, b, float(rng.random() < p)))
    out = fit(results)
    theta = {p: (r.rating - MEAN_RATING) / RATING_SCALE for p, r in out.items()}
    expected = dict.fromkeys(players, 0.0)
    actual = dict.fromkeys(players, 0.0)
    for a, b, score in results:
        p = math.exp(theta[a]) / (math.exp(theta[a]) + math.exp(theta[b]))
        expected[a] += p
        expected[b] += 1 - p
        actual[a] += score
        actual[b] += 1 - score
    assert expected == pytest.approx(actual, abs=1e-6)


def test_draws_count_half_each_way():
    assert fit(series("a", "b", 4, 4, draws=2))["a"].rating == pytest.approx(MEAN_RATING)
    with_draws = fit(series("a", "b", 6, 2, draws=2))  # 7 : 3
    gap = RATING_SCALE * math.log(7 / 3)
    assert with_draws["a"].rating - with_draws["b"].rating == pytest.approx(gap, abs=1e-6)


def test_more_games_narrow_the_interval():
    def width(n):
        r = fit(series("a", "b", 3 * n, n))["a"]
        return r.high - r.low

    assert width(40) < width(10) < width(2)


@pytest.mark.parametrize(
    "results",
    [
        [],
        series("a", "b", 5, 0),  # unbeaten
        series("a", "b", 3, 1) + series("a", "c", 2, 0) + series("b", "c", 2, 0),  # c never won
        series("a", "b", 2, 2) + series("c", "d", 2, 2),  # two separate groups
    ],
)
def test_no_finite_estimate_returns_none(results):
    assert fit(results) is None


def test_a_cycle_of_wins_is_enough():
    out = fit(series("a", "b", 1, 0) + series("b", "c", 1, 0) + series("c", "a", 1, 0))
    assert out["a"].rating == pytest.approx(MEAN_RATING, abs=1e-6)
