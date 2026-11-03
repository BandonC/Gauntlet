"""liars_dice_small (2 players, 2 dice, faces 1-4) and the CFR bot that plays it."""

import numpy as np
import pytest

from arena.engine import InMemoryTurnStore, MatchRunner, MatchSpec
from arena.games.liars_dice import RULES, LiarsDice, LiarsDiceSmall, roll
from arena.games.registry import get_game
from arena.players.base import TurnContext
from arena.players.bots import (
    CFR_POLICY_PATH,
    LiarsDiceCFRBot,
    RandomPlayer,
    build_bot,
    load_cfr_policy,
)
from arena.solvers.liars_dice_cfr import export_policy, solve

G = LiarsDiceSmall()


def test_the_main_game_is_unchanged():
    full = LiarsDice()
    assert full.rules_text() == RULES and "starts with 5 dice" in RULES and "(1-6)" in RULES
    face = full.action_spec(full.initial_state(2, 1), 0).input_schema["properties"]["face"]
    assert face["maximum"] == 6 and face["description"] == "For a bid: the face value, 1-6."
    assert full.initial_state(3, 9).dice == roll(9, 1, (5, 5, 5))  # same seeded dice as before


def test_small_variant_rules_and_limits():
    assert get_game("liars_dice_small") is not None
    text = G.rules_text()
    assert "starts with 2 dice" in text and "(1-4)" in text
    s = G.initial_state(2, 4)
    assert [len(d) for d in s.dice] == [2, 2] and all(1 <= x <= 4 for d in s.dice for x in d)
    assert (
        G.validate_action(s, 0, {"action": "bid", "quantity": 1, "face": 5}) == "face must be 1-4"
    )
    assert G.validate_action(s, 0, {"action": "bid", "quantity": 5, "face": 1}) is not None
    with pytest.raises(ValueError, match="takes 2 players"):
        G.initial_state(3, 1)


def spec(seed, seats):
    return MatchSpec(f"lds{seed}", "liars_dice_small", seed, seats)


def test_lookup_matches_the_exported_strategy(tmp_path):
    sol = solve(dice=2, faces=2, iterations=30, sub_iterations=30, threshold=0.0, check_every=30)
    path = tmp_path / "p.bin"
    export_policy(sol, path)
    policy = load_cfr_policy(path)
    assert (policy.faces, policy.dice) == (2, 2)
    for state, rnd in sol.rounds.items():
        avg = rnd.average(np.arange(rnd.n))
        for node in (0, 1, 5, rnd.n - 2):
            for r in range(rnd.nr[rnd.mover(bin(node).count("1"))]):
                expected = [int(x) for x in np.rint(avg[node, r] * 255)]
                assert policy.probabilities(state, node, r) == expected


def test_the_stored_policy_is_for_this_variant():
    policy = load_cfr_policy(CFR_POLICY_PATH)
    assert (policy.faces, policy.dice) == (G.faces, G.dice_per_player)
    assert set(policy.rounds) == {(1, 1, 0), (1, 1, 1), (1, 2, 0), (2, 1, 1), (2, 2, 0)}


async def test_cfr_bot_plays_whole_games_legally_from_either_seat():
    for seed in range(40):
        bot = build_bot("liars_dice_cfr", "cfr", 0)
        players = [bot, RandomPlayer("rnd", seed)] if seed % 2 else [RandomPlayer("rnd", seed), bot]
        out = await MatchRunner(G, players, InMemoryTurnStore()).run(
            spec(seed, [p.player_id for p in players])
        )
        assert sum(out.scores) == 1.0 and out.forfeits == []
        assert all(t.attempts == 1 for t in out.turns)


async def test_cfr_bot_beats_random_play_and_is_stateless():
    # `LiarsDiceBot` can't be the opponent: it assumes six faces and would bid illegal ones.
    wins = 0
    games = 100
    for seed in range(games):
        cfr, rnd = LiarsDiceCFRBot("cfr", seed), RandomPlayer("rnd", seed)
        players = [cfr, rnd] if seed % 2 == 0 else [rnd, cfr]
        out = await MatchRunner(G, players, InMemoryTurnStore()).run(
            spec(seed, [p.player_id for p in players])
        )
        wins += out.scores[seed % 2] == 1.0
    assert wins > 75, wins  # a sanity check, not a measurement
    s = G.initial_state(2, 3)
    ctx = TurnContext("liars_dice_small", 0, "", G.player_view(s, 0), "", G.action_spec(s, 0), None)
    bot = LiarsDiceCFRBot("cfr", 7)
    assert (await bot.act(ctx)).action == (await bot.act(ctx)).action
