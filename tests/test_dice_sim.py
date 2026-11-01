"""liars-dice-bot-v2 and the free simulation that tuned it."""

import pytest

from arena.cli import _build_player
from arena.dice_sim import Bluffer, evaluate, head_to_head, play, sweep
from arena.engine import InMemoryTurnStore, MatchRunner, MatchSpec
from arena.games.liars_dice import LiarsDice
from arena.players.base import TurnContext
from arena.players.bots import (
    LIARS_DICE_V1_THRESHOLD,
    LIARS_DICE_V2_THRESHOLD,
    LiarsDiceBot,
    build_bot,
)
from arena.roster import PlayerConfig, config_hash
from arena.web import wilson


def view(own, total, bids):
    bids = [{"seat": 0, "quantity": q, "face": f} for q, f in bids]
    return {"your_dice": own, "total_dice": total, "bids": bids}


def ctx(own, total, bids=()):
    return TurnContext("liars_dice", 1, "", view(own, total, list(bids)), "", None, None)


def test_v1_is_unchanged_so_earlier_tournaments_stay_comparable():
    assert LIARS_DICE_V1_THRESHOLD == 0.5
    assert LiarsDiceBot().challenge_below == 0.5
    assert build_bot("liars_dice", "liars-dice-bot", 1).challenge_below == 0.5
    cfg = PlayerConfig(id="liars-dice-bot", provider="bot", model="liars_dice", seed=1)
    assert config_hash(cfg) == "bot-liars_dice-1"


def test_v2_is_a_separate_player_with_its_own_identity():
    v2 = build_bot("liars_dice_v2", "liars-dice-bot-v2", 1)
    assert v2.challenge_below == LIARS_DICE_V2_THRESHOLD < LIARS_DICE_V1_THRESHOLD
    cfg = PlayerConfig(id="liars-dice-bot-v2", provider="bot", model="liars_dice_v2", seed=1)
    assert config_hash(cfg) == "bot-liars_dice_v2-1"


def test_arena_play_knows_v2_by_name():
    bot, _ = _build_player(None, "liars-dice-bot-v2")
    assert bot.challenge_below == LIARS_DICE_V2_THRESHOLD


async def test_v2_raises_a_bid_v1_would_challenge():
    # No twos held and 5 unseen dice: 2 x 2 is p = 0.196, under v1's 0.5 and over v2's 0.1.
    move = ctx([1, 3, 4, 5, 6], 10, [(2, 2)])
    assert (await LiarsDiceBot("v1", 0.5).act(move)).action == {"action": "challenge"}
    assert (await LiarsDiceBot("v2", 0.1).act(move)).action["action"] == "bid"


async def test_v2_challenges_a_bid_below_its_threshold():
    move = ctx([1, 3, 4, 5, 6], 10, [(3, 2)])  # p = 0.035
    assert (await LiarsDiceBot("v2", 0.1).act(move)).action == {"action": "challenge"}


async def test_v2_challenges_when_no_raise_exists():
    move = ctx([6, 6], 2, [(2, 6)])  # the bid is true and nothing is higher
    assert (await LiarsDiceBot("v2", 0.1).act(move)).action == {"action": "challenge"}


async def test_v2_makes_the_most_likely_legal_raise():
    move = ctx([6, 6, 6, 1, 2], 10, [(2, 3)])
    action = (await LiarsDiceBot("v2", 0.1).act(move)).action
    assert action == {"action": "bid", "quantity": 2, "face": 6}  # already true from own dice


async def test_a_bot_decides_from_the_view_alone():
    bot = LiarsDiceBot("v2", 0.1)
    move = ctx([2, 2, 5], 8, [(2, 4)])
    assert [(await bot.act(move)).action for _ in range(3)] == [(await bot.act(move)).action] * 3


async def test_v2_never_sees_what_a_seat_cannot_see():
    # Same own dice and public bids, different hidden dice: the same move.
    game = LiarsDice()
    a, b = game.initial_state(2, 1), game.initial_state(2, 1)
    b = type(b)(**{**b.__dict__, "dice": (a.dice[0], (6, 6, 6, 6, 6))})
    bot = LiarsDiceBot("v2", 0.1)
    moves = []
    for state in (a, b):
        spec = game.action_spec(state, 0)
        view_ = game.player_view(state, 0)
        moves.append(
            (await bot.act(TurnContext("liars_dice", 0, "", view_, "", spec, None))).action
        )
    assert moves[0] == moves[1]


async def test_a_game_through_the_real_runner_is_legal_and_replays():
    bots = [LiarsDiceBot("v1"), build_bot("liars_dice_v2", "v2", 1)]
    spec = MatchSpec("m", "liars_dice", 7, ["v1", "v2"])
    store = InMemoryTurnStore()
    await MatchRunner(LiarsDice(), bots, store).run(spec)
    turns = store.turns["m"]
    assert turns and not any(t.forfeited or t.attempts > 1 for t in turns)


async def test_simulation_is_deterministic_and_alternates_seats():
    first, second = LiarsDiceBot("a", 0.1), LiarsDiceBot("b", 0.5)
    assert await head_to_head(first, second, range(40)) == await head_to_head(
        first, second, range(40)
    )
    assert await play([first, second], 3) == await play([first, second], 3)


async def test_a_mirror_match_is_even_once_seats_alternate():
    bot = LiarsDiceBot("a", 0.5)
    wins, games = await head_to_head(bot, LiarsDiceBot("b", 0.5), range(2_000))
    low, high = wilson(wins, games)
    assert low < 0.5 < high


async def test_a_bluffer_only_makes_legal_moves():
    await head_to_head(Bluffer("b", 2, 0.5), LiarsDiceBot("c", 0.1), range(200))  # raises if not


async def test_an_illegal_bot_raises_rather_than_counting_as_a_game():
    class Illegal:
        player_id = "illegal"

        async def act(self, ctx):
            return type(await LiarsDiceBot().act(ctx))(action={"action": "challenge"})

    with pytest.raises(ValueError, match="illegal"):
        await play([Illegal(), LiarsDiceBot()], 1)


async def test_lower_thresholds_beat_v1_in_a_short_sweep():
    rows = {r["threshold"]: r["wins"] / r["games"] for r in await sweep(200)}
    assert rows[LIARS_DICE_V2_THRESHOLD] > rows[LIARS_DICE_V1_THRESHOLD]


@pytest.mark.slow
async def test_v2_beats_v1_over_a_thousand_unseen_seeds():
    wins, games = await evaluate(1_000)
    assert games == 1_000 and wilson(wins, games)[0] > 0.5
