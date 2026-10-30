import pytest

from arena.ratings.glicko2 import DEFAULT_RD, Glicko, update, update_pair


def test_matches_worked_example_in_glickmans_paper():
    # "Example of the Glicko-2 system" (Glickman, 2013), section 3: tau = 0.5.
    player = Glicko(1500, 200, 0.06)
    results = [(Glicko(1400, 30), 1.0), (Glicko(1550, 100), 0.0), (Glicko(1700, 300), 0.0)]
    out = update(player, results, tau=0.5)
    assert out.rating == pytest.approx(1464.06, abs=0.01)
    assert out.rd == pytest.approx(151.52, abs=0.01)
    assert out.vol == pytest.approx(0.05999, abs=1e-5)


def test_no_games_changes_nothing():
    # Skill is fixed by config_hash: no RD inflation for inactivity.
    p = Glicko(1600, 80, 0.05)
    assert update(p, []) == p


def test_pair_update_is_symmetric_and_shrinks_rd():
    a, b = update_pair(Glicko(), Glicko(), 1.0)
    assert a.rating > 1500 > b.rating
    assert a.rating - 1500 == pytest.approx(1500 - b.rating)
    assert a.rd == pytest.approx(b.rd) and a.rd < DEFAULT_RD
    draw_a, draw_b = update_pair(Glicko(), Glicko(), 0.5)
    assert draw_a.rating == pytest.approx(1500) and draw_b.rating == pytest.approx(1500)
