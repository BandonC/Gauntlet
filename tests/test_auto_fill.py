"""A reply that left out a field with only one legal value is completed, and says so."""

import pytest

from arena.engine import InMemoryTurnStore, MatchRunner, MatchSpec
from arena.engine.match_runner import iter_states
from arena.games.liars_dice import LiarsDice
from arena.games.registry import get_game
from arena.games.werewolf import MAX_MESSAGE_CHARS
from arena.players.base import Decision
from arena.players.bots import ScriptedPlayer, WerewolfBot

DAY_FIRST = get_game("werewolf_dayfirst")
SEATS = [f"p{i}" for i in range(7)]


class DropsAction:
    """Plays a real bot's move with `action` removed, as Sonnet 5.5 did."""

    def __init__(self, player_id, seed):
        self.player_id = player_id
        self.bot = WerewolfBot(player_id, seed)

    async def act(self, ctx):
        action = (await self.bot.act(ctx)).action
        return Decision(action={k: v for k, v in action.items() if k != "action"})


class FirstMove:
    """Sends the given moves on the first turn, then plays as a bot."""

    def __init__(self, player_id, moves):
        self.player_id = player_id
        self.moves = list(moves)
        self.bot = WerewolfBot(player_id, 0)

    async def act(self, ctx):
        if self.moves:
            return Decision(action=self.moves.pop(0))
        return await self.bot.act(ctx)


async def run(players, game=DAY_FIRST):
    store = InMemoryTurnStore()
    spec = MatchSpec("m", game.name, 3, [p.player_id for p in players])
    await MatchRunner(game, players, store).run(spec)
    return spec, store.turns["m"]


async def first_turn(*moves):
    players = [FirstMove("p0", moves)] + [WerewolfBot(p, i) for i, p in enumerate(SEATS[1:], 1)]
    _, turns = await run(players)
    return turns[0]


async def test_a_whole_game_of_missing_actions_is_played_and_replays():
    spec, turns = await run([DropsAction(p, i) for i, p in enumerate(SEATS)])
    assert turns and all(t.auto_filled and t.attempts == 1 and not t.forfeited for t in turns)
    assert all(t.action["action"] in {"say", "vote", "kill", "inspect"} for t in turns)
    list(iter_states(DAY_FIRST, spec, turns))  # replay re-validates every stored action


async def test_a_complete_action_is_not_flagged():
    _, turns = await run([WerewolfBot(p, i) for i, p in enumerate(SEATS)])
    assert not any(t.auto_filled for t in turns)


def test_a_complete_action_is_returned_untouched():
    state = DAY_FIRST.initial_state(7, 1)
    action = {"action": "say", "message": "hello"}
    assert DAY_FIRST.fill_action(state, 0, action) is action


async def test_a_wrong_action_is_still_rejected_and_logged_as_sent():
    wrong = {"action": "vote", "message": "hello"}
    turn = await first_turn(wrong, {"message": "hello"})
    assert turn.attempts == 2 and turn.auto_filled
    assert turn.rejections[0]["action"] == wrong
    assert "must be 'say'" in turn.rejections[0]["reason"]


async def test_a_filled_action_must_still_be_valid_and_rejections_keep_what_was_sent():
    turn = await first_turn({"message": "  "}, {"action": "say", "message": "ok"})
    assert turn.attempts == 2 and not turn.auto_filled
    assert turn.rejections[0]["action"] == {"message": "  "}


async def test_three_bad_replies_still_forfeit_even_with_filling():
    turn = await first_turn({"message": ""}, {"message": ""}, {"message": ""})
    assert turn.forfeited and not turn.auto_filled


async def test_filling_and_truncation_are_flagged_apart():
    turn = await first_turn({"message": "x" * (MAX_MESSAGE_CHARS + 5)})
    assert turn.auto_filled and turn.truncated
    assert len(turn.action["message"]) == MAX_MESSAGE_CHARS and turn.action["action"] == "say"


async def test_truncation_alone_is_not_an_auto_fill():
    turn = await first_turn({"action": "say", "message": "x" * (MAX_MESSAGE_CHARS + 5)})
    assert turn.truncated and not turn.auto_filled


@pytest.mark.parametrize("bad", ["say", 3, None, ["say"]])
def test_a_reply_that_is_not_an_object_is_left_for_validation(bad):
    state = DAY_FIRST.initial_state(7, 1)
    assert DAY_FIRST.fill_action(state, 0, bad) == bad


async def test_the_original_werewolf_fills_too():
    game = get_game("werewolf")
    _, turns = await run([DropsAction(p, i) for i, p in enumerate(SEATS)], game)
    assert all(t.auto_filled for t in turns)


async def test_other_games_never_fill():
    game = LiarsDice()
    missing = {"quantity": 1, "face": 2}
    assert game.fill_action(game.initial_state(2, 1), 0, missing) is missing
    players = [
        ScriptedPlayer([missing, missing, missing], "a"),
        ScriptedPlayer([], "b"),
    ]
    _, turns = await run(players, game)
    assert turns[0].forfeited and not turns[0].auto_filled
