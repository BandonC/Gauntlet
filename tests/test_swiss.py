import pytest

from arena.tournaments import (
    SwissEntrant,
    default_swiss_rounds,
    swiss_pairings,
    swiss_seatings,
)


def entrants(*specs):
    """specs: (id, points, rating[, had_bye])"""
    return [SwissEntrant(*s) for s in specs]


def test_round_one_is_top_half_against_bottom_half_by_rating():
    field = entrants(*[(f"p{i}", 0.0, 2000 - 100 * i) for i in range(6)])
    pairs, bye = swiss_pairings(field, played=[], first_round=True)
    assert pairs == [("p0", "p3"), ("p1", "p4"), ("p2", "p5")] and bye is None


def test_later_rounds_pair_neighbours_by_points_then_rating():
    field = entrants(("a", 2, 1500), ("b", 2, 1600), ("c", 0, 1700), ("d", 0, 1400))
    pairs, _ = swiss_pairings(field, played=[])
    assert pairs == [("b", "a"), ("c", "d")]


def test_avoids_rematch_when_possible():
    field = entrants(("a", 4, 1500), ("b", 4, 1500), ("c", 2, 1500), ("d", 2, 1500))
    played = [frozenset("ab"), frozenset("cd")]
    pairs, _ = swiss_pairings(field, played)
    assert pairs == [("a", "c"), ("b", "d")]


def test_backtracks_instead_of_stranding_a_player():
    # Greedy a-b leaves c-d, which already played. a-c / b-d is the only clean pairing.
    field = entrants(("a", 3, 1500), ("b", 2, 1500), ("c", 1, 1500), ("d", 0, 1500))
    played = [frozenset("cd"), frozenset("ad")]
    pairs, _ = swiss_pairings(field, played)
    assert pairs == [("a", "c"), ("b", "d")]


def test_rematch_only_when_unavoidable():
    field = entrants(("a", 2, 1500), ("b", 0, 1500))
    pairs, _ = swiss_pairings(field, played=[frozenset("ab")])
    assert pairs == [("a", "b")]


def test_bye_goes_to_lowest_ranked_without_one():
    field = entrants(("a", 4, 1500), ("b", 2, 1500), ("c", 0, 1500, True), ("d", 0, 1400))
    field.append(SwissEntrant("e", 0, 1300, True))
    pairs, bye = swiss_pairings(field, played=[])
    assert bye == "d"  # e and c already had byes
    assert sorted(p for pair in pairs for p in pair) == ["a", "b", "c", "e"]


def test_everyone_had_a_bye_falls_back_to_lowest_ranked():
    field = entrants(("a", 2, 1500, True), ("b", 2, 1400, True), ("c", 2, 1300, True))
    assert swiss_pairings(field, played=[])[1] == "c"


def test_seatings_swap_seats_within_each_pairing():
    assert swiss_seatings([("a", "b"), ("c", "d")]) == [
        ["a", "b"],
        ["b", "a"],
        ["c", "d"],
        ["d", "c"],
    ]


@pytest.mark.parametrize(("n", "rounds"), [(2, 1), (4, 2), (5, 3), (8, 3), (9, 4), (16, 4)])
def test_default_rounds_is_ceil_log2(n, rounds):
    assert default_swiss_rounds(n) == rounds


def test_duplicate_ids_rejected():
    with pytest.raises(ValueError):
        swiss_pairings(entrants(("a", 0, 1), ("a", 0, 1)), played=[])
