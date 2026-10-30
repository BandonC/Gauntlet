"""Weng-Lin Plackett-Luce against reference values.

Generated once with openskill 6.2.0 (a throwaway script, not a dependency):
    PlackettLuce(tau=0.0).rate([[model.rating(mu=m, sigma=s)] for m, s in priors], ranks=ranks)
tau has to go to the constructor: rate(tau=0) falls back to the model's default.
openskill 6.2.0's end-of-_compute tie averaging is a no-op (it compares objects it has
already mutated), so tied seats move differently, as in the paper's Algorithm 4.
"""

import pytest

from arena.ratings.weng_lin import DEFAULT_MU, DEFAULT_SIGMA, WengLin, rate

D = (25.0, 8.333333333333334)

CASES = {
    "heads_up_defaults": (
        [D, D],
        [1, 2],
        [(27.63523138347365, 8.065506316323548), (22.36476861652635, 8.065506316323548)],
    ),
    "heads_up_draw_defaults": (
        [D, D],
        [1, 1],
        [(25.0, 8.065506316323548), (25.0, 8.065506316323548)],
    ),
    "heads_up_upset": (
        [(30.0, 2.0), (22.0, 6.0)],
        [2, 1],
        [(29.668607205366467, 1.9974807237667733), (24.982535151701803, 5.792478378226315)],
    ),
    "heads_up_draw_unequal": (
        [(30.0, 2.0), (22.0, 6.0)],
        [1, 1],
        [(29.899976171550374, 1.9974807237667733), (22.900214456046616, 5.792478378226315)],
    ),
    "three_defaults": (
        [D, D, D],
        [1, 2, 3],
        [
            (27.868876552746237, 8.204837030780652),
            (25.717219138186557, 8.057829747583874),
            (21.413904309067206, 8.057829747583874),
        ],
    ),
    "three_unsorted_ranks": (
        [(29.0, 3.0), (25.0, 8.0), (20.0, 5.0)],
        [3, 1, 2],
        [
            (28.16975185475061, 2.989695692207033),
            (28.51186471770366, 7.750694326307138),
            (20.93442269256198, 4.933352370495797),
        ],
    ),
    "four_with_tie_unequal": (
        [(31.0, 4.0), (26.0, 2.5), (19.0, 7.0), (24.0, 6.0)],
        [2, 1, 4, 2],
        [
            (30.550107518298613, 3.973545941663301),
            (26.3498584741892, 2.4984160219655043),
            (17.694074124253778, 6.85008381020344),
            (23.956529017944536, 5.894219947962925),
        ],
    ),
    "winners_tie_losers_tie": (
        [(28.0, 3.0), (24.0, 5.0), (26.0, 4.0), (21.0, 6.0), (23.0, 2.0)],
        [1, 3, 1, 3, 3],
        [
            (28.16388307635929, 2.9967013491815697),
            (23.578014124643513, 4.948327146251335),
            (26.334679935729543, 3.9905893584229166),
            (20.697351906428697, 5.906608618265482),
            (22.944638399250668, 1.9987398402031016),
        ],
    ),
    "all_tied_unequal": (
        [(30.0, 2.0), (22.0, 6.0), (25.0, 4.0)],
        [1, 1, 1],
        [
            (29.94340220976611, 1.9982220500619003),
            (22.383587857739343, 5.899375046569093),
            (25.05590766860697, 3.9761446454212415),
        ],
    ),
    "six_strict": (
        [(25.0, 8.0), (30.0, 1.5), (18.0, 7.0), (22.0, 3.0), (27.0, 5.0), (35.0, 4.0)],
        [4, 2, 6, 1, 3, 5],
        [
            (25.71396756582062, 7.6986176047199155),
            (30.07868484921678, 1.4998083085701512),
            (15.625949145506855, 6.821093539996907),
            (22.481253420963995, 2.9990167186281997),
            (27.61037638478102, 4.966228122055021),
            (33.79096915849229, 3.968256404819579),
        ],
    ),
    "skip_ranks_competition_style": (
        [D, D, D, D],
        [1, 2, 2, 4],
        [
            (27.795084971874736, 8.263160757613477),
            (24.68943500312503, 8.179213704945203),
            (24.68943500312503, 8.179213704945203),
            (22.826045021875203, 8.179213704945203),
        ],
    ),
}

# 25 matches among 5 players at mixed table sizes, so ratings carry across updates:
# (seats, ranks), then every player's final (mu, sigma).
CHAIN = [
    ([4, 2, 1], [1, 3, 3]),
    ([2, 0, 1, 4], [4, 2, 2, 2]),
    ([0, 2], [1, 2]),
    ([4, 3, 1, 0, 2], [4, 3, 4, 4, 2]),
    ([2, 3, 4], [3, 3, 1]),
    ([0, 3, 4], [1, 3, 2]),
    ([4, 1, 2, 0, 3], [1, 4, 2, 1, 4]),
    ([3, 0, 4, 2, 1], [1, 4, 3, 1, 1]),
    ([3, 2, 0], [2, 1, 2]),
    ([4, 2, 0, 3], [3, 3, 2, 1]),
    ([4, 2], [2, 2]),
    ([2, 0, 3], [2, 2, 3]),
    ([3, 0, 1, 2], [2, 2, 4, 2]),
    ([1, 0], [2, 2]),
    ([4, 1, 3, 2, 0], [4, 3, 3, 3, 1]),
    ([1, 2, 0], [3, 2, 3]),
    ([1, 3, 0, 4], [2, 3, 2, 1]),
    ([3, 0], [1, 2]),
    ([1, 0, 3, 2], [1, 1, 1, 2]),
    ([3, 1, 4], [2, 3, 2]),
    ([4, 1, 2], [3, 2, 1]),
    ([0, 1], [1, 1]),
    ([4, 0], [2, 2]),
    ([2, 1, 0], [2, 3, 2]),
    ([4, 2, 1, 0, 3], [4, 5, 5, 3, 2]),
]
CHAIN_FINAL = [
    (25.905910842630945, 5.942617654433875),
    (20.087700028148085, 6.277612053790841),
    (26.445266286126884, 6.245670599846256),
    (26.28029614326176, 6.579669290452215),
    (25.603101251004652, 6.295075977953649),
]


@pytest.mark.parametrize("name", CASES)
def test_matches_openskill_reference_values(name):
    priors, ranks, expected = CASES[name]
    out = rate([WengLin(m, s) for m, s in priors], ranks)
    assert [(p.mu, p.sigma) for p in out] == pytest.approx(
        [pytest.approx(e, rel=1e-12) for e in expected]
    )


def test_matches_openskill_across_a_chain_of_mixed_table_sizes():
    state = [WengLin() for _ in range(5)]
    for seats, ranks in CHAIN:
        for seat, new in zip(seats, rate([state[s] for s in seats], ranks), strict=True):
            state[seat] = new
    for got, (mu, sigma) in zip(state, CHAIN_FINAL, strict=True):
        assert (got.mu, got.sigma) == pytest.approx((mu, sigma), rel=1e-12)


def test_defaults_and_ordinal():
    assert (WengLin().mu, WengLin().sigma) == (DEFAULT_MU, DEFAULT_SIGMA) == D
    assert WengLin(30.0, 2.0).ordinal == 24.0
    assert WengLin().ordinal == pytest.approx(0.0)


def test_only_the_order_of_ranks_matters():
    priors = [WengLin(29.0, 3.0), WengLin(25.0, 8.0), WengLin(20.0, 5.0)]
    assert rate(priors, [3, 1, 2]) == rate(priors, [30, 7, 12])


def test_rejects_mismatched_or_solo_input():
    with pytest.raises(ValueError):
        rate([WengLin()], [1])
    with pytest.raises(ValueError):
        rate([WengLin(), WengLin()], [1])
