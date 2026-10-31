"""The balance simulator, held to the real engine on the current rules."""

import random

import pytest

from arena.balance import (
    CURRENT,
    DEFAULT_P,
    DOCTOR,
    VARIANTS,
    Ruleset,
    play,
    protect,
    simulate,
    vote_target,
)
from arena.engine import InMemoryTurnStore, MatchRunner, MatchSpec
from arena.engine.match_runner import iter_states
from arena.games.registry import get_game
from arena.games.werewolf import ROLES, SEATS, SEER, VILLAGER, WOLF
from arena.players.bots import AccuracyVoteBot, build_bot

GAMES = 400
DAY_FIRST = VARIANTS[1]


def test_the_current_ruleset_is_the_engines():
    assert sorted(CURRENT.roles()) == sorted(ROLES)


def test_variants_seat_the_roles_they_promise():
    roles = {r.name[0]: r.roles() for r in VARIANTS}
    assert roles["c"].count(WOLF) == 2 and roles["c"].count(VILLAGER) == 6 and len(roles["c"]) == 9
    assert (
        roles["d"].count(DOCTOR) == 1 and roles["d"].count(VILLAGER) == 3 and len(roles["d"]) == 7
    )
    assert roles["e"] == roles["d"]
    assert all(r.count(SEER) == 1 for r in roles.values())


def test_play_is_deterministic_for_a_seed():
    for rules in VARIANTS:
        assert play(rules, 11) == play(rules, 11)


def test_a_vote_is_legal_for_every_seat():
    roles = list(ROLES)
    for seed in range(200):
        for seat in range(SEATS):
            target = vote_target(roles, list(range(SEATS)), seat, 1, seed, 0.5, 0.5)
            assert target != seat


@pytest.mark.slow
@pytest.mark.parametrize("focus", [0.0, 0.5, 1.0])
def test_focus_leaves_a_voters_accuracy_at_p(focus):
    roles, alive = list(ROLES), list(range(SEATS))
    votes = [vote_target(roles, alive, 3, 1, seed, 0.6, focus) for seed in range(4000)]
    assert sum(roles[t] == WOLF for t in votes) / len(votes) == pytest.approx(0.6, abs=0.03)


def test_focus_lines_votes_up():
    roles, alive = list(ROLES), list(range(SEATS))

    def matching(focus):
        pairs = [
            (
                vote_target(roles, alive, 3, 1, s, 0.6, focus),
                vote_target(roles, alive, 4, 1, s, 0.6, focus),
            )
            for s in range(1000)
        ]
        return sum(a == b for a, b in pairs)

    assert matching(1.0) > matching(0.0) * 2


@pytest.mark.slow
def test_a_certain_village_always_wins_and_a_blind_one_always_loses():
    assert simulate(CURRENT, 300, p=1.0, focus=1.0).village_rate == 1.0
    assert simulate(CURRENT, 300, p=0.0, focus=1.0).village_rate == 0.0


def test_a_doctor_never_protects_itself_twice_running():
    rng = random.Random(1)
    alive = [0, 2, 5]
    after_self = {protect(rng, alive, 2, 2) for _ in range(200)}
    assert after_self == {0, 5}
    assert {protect(rng, alive, 2, 5) for _ in range(200)} == {0, 2, 5}
    assert {protect(rng, alive, 2, None) for _ in range(200)} == {0, 2, 5}
    assert protect(rng, [2, 4], 2, 2) == 4


@pytest.mark.slow
def test_a_doctor_adds_a_night_turn():
    assert simulate(Ruleset("d", doctor=True), 300).mean_turns > simulate(CURRENT, 300).mean_turns


async def engine_game(game, seed, p, focus):
    roles = list(game.initial_state(SEATS, seed).roles)
    players = [AccuracyVoteBot(f"b{s}", seed, roles, p, focus) for s in range(SEATS)]
    spec = MatchSpec(f"sim{seed}", game.name, seed, [q.player_id for q in players])
    out = await MatchRunner(game, players, InMemoryTurnStore()).run(spec)
    *_, final = iter_states(game, spec, out.turns)
    return final.winner == "village", len(out.turns)


def test_smoke_a_few_games_of_every_variant():
    for rules in VARIANTS:
        r = simulate(rules, 20, focus=0.7)
        assert r.games == sum(r.first_vote.values()) == 20 and r.mean_turns > 15


async def test_smoke_the_bot_finishes_a_real_game():
    for name in ("werewolf", "werewolf_dayfirst"):
        _, turns = await engine_game(get_game(name), 1, DEFAULT_P, 0.7)
        assert turns > 15


@pytest.mark.slow
@pytest.mark.parametrize(
    ("name", "rules", "focus"),
    [
        ("werewolf", CURRENT, 0.0),
        ("werewolf", CURRENT, 0.7),
        ("werewolf_dayfirst", DAY_FIRST, 0.0),
        ("werewolf_dayfirst", DAY_FIRST, 0.7),
    ],
)
async def test_the_simulator_matches_the_real_engine(name, rules, focus):
    game = get_game(name)
    played = [await engine_game(game, seed, DEFAULT_P, focus) for seed in range(GAMES)]
    wins = sum(won for won, _ in played) / GAMES
    turns = sum(t for _, t in played) / GAMES
    sim = simulate(rules, 2000, DEFAULT_P, focus)
    assert wins == pytest.approx(sim.village_rate, abs=0.06)
    assert turns == pytest.approx(sim.mean_turns, abs=1.0)


@pytest.mark.slow
@pytest.mark.parametrize(
    ("rules", "rate"), [(CURRENT, 0.224), (DAY_FIRST, 0.414)], ids=["current", "day-first"]
)
def test_the_published_predictions_still_hold(rules, rate):
    # Seeded, so these are exact: the numbers quoted when day-first was chosen.
    assert simulate(rules, 2000, DEFAULT_P, 0.7).village_rate == pytest.approx(rate, abs=0.0006)


@pytest.mark.parametrize("kind", ["accuracy", "accuracy_vote"])
def test_the_bot_that_knows_every_role_is_not_reachable_from_a_roster(kind):
    with pytest.raises(ValueError):
        build_bot(kind, "x", 1)
