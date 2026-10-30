import json
import random
from dataclasses import replace

import pytest

from arena.engine import InMemoryTurnStore, MatchRunner, MatchSpec
from arena.engine.match_runner import iter_states
from arena.games.liars_dice import Bid, LiarsDice, roll
from arena.games.registry import get_game
from arena.players.base import TurnContext
from arena.players.bots import LiarsDiceBot, RandomPlayer

G = LiarsDice()


def bid(q, f):
    return {"action": "bid", "quantity": q, "face": f}


CHALLENGE = {"action": "challenge"}


def state_with(dice, to_move=0, bids=(), seed=7):
    """A mid-round state with chosen dice (counts follow the dice)."""
    s = G.initial_state(len(dice), seed)
    return replace(
        s,
        dice=tuple(tuple(d) for d in dice),
        dice_counts=tuple(len(d) for d in dice),
        to_move=to_move,
        bids=tuple(Bid(*b) for b in bids),
    )


def test_registered_and_rolls_round_dice_from_seed_and_round():
    assert get_game("liars_dice").name == "liars_dice"
    s = G.initial_state(3, seed=42)
    assert s.dice_counts == (5, 5, 5) and s.round == 1 and s.to_move == 0
    rng = random.Random("42:1")
    assert s.dice == tuple(tuple(sorted(rng.randint(1, 6) for _ in range(5))) for _ in range(3))
    assert roll(42, 2, (5, 5, 5)) == roll(42, 2, (5, 5, 5)) != s.dice


@pytest.mark.parametrize("n", [1, 7])
def test_player_count_limits(n):
    with pytest.raises(ValueError):
        G.initial_state(n, 1)


def test_bid_validation():
    s = state_with([(1, 2), (3, 4)])
    assert G.validate_action(s, 0, CHALLENGE) == "there is no bid to challenge; make the first bid"
    assert G.validate_action(s, 1, bid(1, 1)) == "it is not your turn"
    assert G.validate_action(s, 0, bid(5, 2)) == "quantity must be 1-4 (dice in play)"
    assert G.validate_action(s, 0, bid(1, 7)) == "face must be 1-6"
    assert G.validate_action(s, 0, bid(True, 2)) == "a bid needs integer 'quantity' and 'face'"
    assert G.validate_action(s, 0, {"action": "pass"}) == "action must be 'bid' or 'challenge'"
    assert G.validate_action(s, 0, bid(1, 1)) is None

    s = state_with([(1, 2), (3, 4)], to_move=1, bids=[(0, 2, 4)])
    assert G.validate_action(s, 1, bid(2, 5)) is None  # same quantity, higher face
    assert G.validate_action(s, 1, bid(3, 1)) is None  # higher quantity, any face
    assert "does not raise 2 x 4" in G.validate_action(s, 1, bid(2, 3))
    assert "does not raise" in G.validate_action(s, 1, bid(2, 4))
    assert G.validate_action(s, 1, CHALLENGE) is None


def test_hint_lists_exactly_the_legal_moves():
    s = state_with([(1, 2), (3, 4)], to_move=1, bids=[(0, 3, 5)])
    hint = G.legal_actions_hint(s, 1)
    assert hint == [bid(3, 6), bid(4, 1), bid(4, 2), bid(4, 3), bid(4, 4), bid(4, 5)] + [
        bid(4, 6),
        CHALLENGE,
    ]
    assert all(G.validate_action(s, 1, a) is None for a in hint)


def test_true_bid_challenged_costs_the_challenger_who_opens_next_round():
    s = state_with([(2, 2, 3), (2, 5)], to_move=1, bids=[(0, 3, 2)])
    out = G.apply(s, 1, CHALLENGE)
    assert out.dice_counts == (3, 1) and out.round == 2 and out.to_move == 1
    assert out.bids == () and out.dice == roll(s.seed, 2, (3, 1))
    assert out.last_reveal == {
        "round": 1,
        "dice": [[2, 2, 3], [2, 5]],
        "bidder": 0,
        "quantity": 3,
        "face": 2,
        "challenger": 1,
        "actual": 3,
        "loser": 1,
    }


def test_false_bid_challenged_costs_the_bidder_and_ones_are_not_wild():
    s = state_with([(1, 1, 3), (2, 5)], to_move=1, bids=[(0, 2, 3)])
    out = G.apply(s, 1, CHALLENGE)  # only one 3; the ones don't count
    assert out.last_reveal["actual"] == 1 and out.last_reveal["loser"] == 0
    assert out.dice_counts == (2, 2) and out.to_move == 0


def test_eliminated_loser_hands_the_opening_to_the_next_player_with_dice():
    s = state_with([(4,), (2, 2), (3, 3)], to_move=1, bids=[(0, 2, 4)])
    out = G.apply(s, 1, CHALLENGE)  # seat 0 bluffed with its last die
    assert out.dice_counts == (0, 2, 2) and out.to_move == 1 and out.winner is None
    # Turn order skips the eliminated seat: 1 -> 2 -> 1.
    after = G.apply(out, 1, bid(1, 1))
    assert after.to_move == 2
    assert G.apply(after, 2, bid(1, 2)).to_move == 1


def test_wraparound_opener_and_last_player_standing_wins():
    s = state_with([(3, 3), (), (6,)], to_move=2, bids=[(0, 1, 3)])
    out = G.apply(s, 2, bid(1, 6))
    assert out.to_move == 0  # seat 1 has no dice: skipped
    end = G.apply(out, 0, CHALLENGE)  # seat 2 has one 6: bid holds, seat 0 loses a die
    assert end.dice_counts == (1, 0, 1) and end.to_move == 0

    s = state_with([(2,), (5,)], to_move=1, bids=[(0, 1, 2)])
    final = G.apply(s, 1, CHALLENGE)
    assert G.is_terminal(final) and final.winner == 0 and G.scores(final) == [1.0, 0.0]


def test_forfeit_removes_the_player_and_the_game_goes_on_if_others_remain():
    s = G.initial_state(3, 5)
    out = G.forfeit(s, 0)
    assert out.dice_counts == (0, 5, 5) and out.to_move == 1 and not G.is_terminal(out)
    assert G.scores(G.forfeit(out, 1)) == [0.0, 0.0, 1.0]


def test_apply_does_not_mutate():
    s = state_with([(1, 2), (3, 4)], to_move=1, bids=[(0, 1, 2)])
    before = repr(s)
    G.apply(s, 1, bid(2, 2))
    G.apply(s, 1, CHALLENGE)
    assert repr(s) == before


def _random_game_states(n_players: int, seed: int):
    """Every state of a random game, played with random legal moves."""
    rng = random.Random(seed)
    s = G.initial_state(n_players, seed)
    states = [s]
    while not G.is_terminal(s):
        seat = G.current_player(s)
        s = G.apply(s, seat, rng.choice(G.legal_actions_hint(s, seat)))
        states.append(s)
    return states


@pytest.mark.parametrize("n_players", [2, 3, 6])
def test_player_view_never_depends_on_other_players_dice(n_players):
    for seed in range(15):
        for s in _random_game_states(n_players, seed):
            for seat in range(n_players):
                # Scramble every OTHER seat's current dice: this seat's view must not move.
                scrambled = replace(
                    s,
                    dice=tuple(
                        d if i == seat else tuple((x % 6) + 1 for x in d)
                        for i, d in enumerate(s.dice)
                    ),
                )
                view = G.player_view(s, seat)
                assert json.dumps(view, sort_keys=True) == json.dumps(
                    G.player_view(scrambled, seat), sort_keys=True
                )
                assert view["your_dice"] == list(s.dice[seat])
                assert "dice" not in view  # only your_dice and the PREVIOUS reveal


def test_spectator_view_shows_all_dice():
    s = state_with([(1, 2), (3, 4, 5)])
    assert G.spectator_view(s)["dice"] == [[1, 2], [3, 4, 5]]


def test_render_view_mentions_dice_bids_and_reveal():
    s = state_with([(2, 2, 3), (2, 5)], to_move=1, bids=[(0, 3, 2)])
    text = G.render_view(G.player_view(s, 1))
    assert "Your dice: 2 5" in text and "player 0: 3 x 2" in text and "challenge" in text
    nxt = G.apply(s, 1, CHALLENGE)
    text = G.render_view(G.player_view(nxt, 0))
    assert "Revealed: player 0: 2 2 3; player 1: 2 5" in text and "Actual count: 3" in text


def spec(seed, seats):
    return MatchSpec(f"ld{seed}", "liars_dice", seed, seats)


async def test_replay_reproduces_the_final_state():
    store = InMemoryTurnStore()
    players = [LiarsDiceBot("bot"), RandomPlayer("rnd", 3)]
    out = await MatchRunner(G, players, store).run(spec(11, ["bot", "rnd"]))
    turns = await store.load_turns("ld11")
    first = list(iter_states(G, spec(11, ["bot", "rnd"]), turns))
    second = list(iter_states(G, spec(11, ["bot", "rnd"]), turns))
    assert first == second and G.is_terminal(first[-1])
    assert G.scores(first[-1]) == out.scores


async def test_bot_beats_random_most_of_the_time_and_every_game_finishes():
    bot_wins = 0
    games = 200
    for seed in range(games):
        bot_seat = seed % 2  # alternate who opens
        players = [RandomPlayer("rnd", seed), LiarsDiceBot("bot")]
        if bot_seat == 0:
            players.reverse()
        out = await MatchRunner(G, players, InMemoryTurnStore()).run(
            spec(seed, [p.player_id for p in players])
        )
        assert sum(out.scores) == 1.0 and out.forfeits == []
        bot_wins += out.scores[bot_seat] == 1.0
    assert bot_wins / games > 0.75, bot_wins


async def test_bot_is_stateless_and_legal():
    bot = LiarsDiceBot()
    for s in _random_game_states(2, 3)[:-1]:
        seat = G.current_player(s)
        ctx = TurnContext(
            "liars_dice",
            seat,
            G.rules_text(),
            G.player_view(s, seat),
            "",
            G.action_spec(s, seat),
            None,
        )
        first, again = await bot.act(ctx), await bot.act(ctx)
        assert first == again  # same view, same move
        assert G.validate_action(s, seat, first.action) is None


async def test_engine_runs_multiplayer_games_through_the_match_runner():
    # Tournaments are heads-up for now, but the engine itself takes 2-6 seats.
    seats = ["a", "b", "c"]
    players = [RandomPlayer(p, i) for i, p in enumerate(seats)]
    out = await MatchRunner(G, players, InMemoryTurnStore()).run(spec(5, seats))
    assert sorted(out.scores) == [0.0, 0.0, 1.0]
