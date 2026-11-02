import asyncio
from itertools import product

import pytest

from arena.cli import _build_player
from arena.engine import InMemoryTurnStore, MatchRunner, MatchSpec
from arena.engine.match_runner import iter_states
from arena.games.registry import get_game, list_games
from arena.games.split_deal import (
    ITEMS,
    MAX_MESSAGE,
    MAX_OFFERS,
    TOTAL_VALUE,
    SplitDeal,
    classify_value_claims,
    deal_table,
    efficiency,
    max_joint_value,
    seat_stats,
    tables,
    value_sets,
    worth,
)
from arena.players.base import TurnContext
from arena.players.bots import RandomPlayer, ScriptedPlayer, SplitDealBot, build_bot
from arena.players.prompting import load_prompt
from arena.store.ratings import seat_zero_outcome
from arena.web import match_result, move_label

G = SplitDeal()
SEED = 2
STATE = G.initial_state(2, SEED)
Q = STATE.quantities


def offer(*keep, message=""):
    action = {"action": "offer", **dict(zip(ITEMS, keep, strict=True))}
    return action | {"message": message} if message else action


ACCEPT = {"action": "accept"}
ALL = offer(*Q)
NONE = offer(0, 0, 0)


def play(*actions):
    state = G.initial_state(2, SEED)
    for action in actions:
        seat = G.current_player(state)
        assert G.validate_action(state, seat, action) is None, action
        state = G.apply(state, seat, action)
    return state


def run(a, b, seed=SEED):
    spec = MatchSpec("m", "split_deal", seed, [a.player_id, b.player_id])
    store = InMemoryTurnStore()
    return asyncio.run(MatchRunner(G, [a, b], store).run(spec)), store.turns["m"]


def test_registered():
    assert "split_deal" in list_games()
    assert isinstance(get_game("split_deal"), SplitDeal)


def test_every_table_is_worth_exactly_ten_to_both_players():
    for seed in range(200):
        quantities, values = deal_table(seed)
        assert all(1 <= q <= 4 for q in quantities)
        assert values[0] != values[1]
        assert all(worth(v, quantities) == TOTAL_VALUE for v in values)


def test_tables_that_cannot_total_ten_are_never_dealt():
    assert (4, 4, 4) not in tables()
    assert (3, 3, 3) not in tables()
    assert all(len(value_sets(q)) >= 2 for q in tables())


def test_the_table_comes_only_from_the_seed():
    assert deal_table(SEED) == deal_table(SEED)
    assert len({deal_table(s) for s in range(50)}) > 30


def test_accepting_scores_each_side_its_own_value():
    state = play(ALL, ACCEPT)
    assert G.is_terminal(state)
    assert G.scores(state) == [1.0, 0.0]
    state = play(NONE, ACCEPT)
    assert G.scores(state) == [0.0, 1.0]


def test_a_split_uses_each_players_own_values():
    keep = (1, 0, 1)
    state = play(offer(*keep), ACCEPT)
    rest = tuple(q - k for q, k in zip(Q, keep, strict=True))
    assert G.scores(state) == [
        worth(STATE.values[0], keep) / 10,
        worth(STATE.values[1], rest) / 10,
    ]


def test_the_offer_accepted_is_the_latest_one():
    state = play(ALL, NONE, ALL, NONE, ACCEPT)
    assert G.scores(state) == [1.0, 0.0]  # seat 1's NONE keeps nothing, so seat 0 gets it all


def test_no_deal_scores_zero_and_ends_after_six_offers_each():
    state = play(*[ALL] * (2 * MAX_OFFERS))
    assert not G.is_terminal(state)
    assert G.current_player(state) == 0
    assert "no offers left" in G.validate_action(state, 0, ALL)
    state = G.apply(state, 0, {"action": "reject"})
    assert G.is_terminal(state)
    assert G.scores(state) == [0.0, 0.0]


def test_the_last_offer_can_still_be_accepted():
    state = G.apply(play(*[ALL] * (2 * MAX_OFFERS)), 0, ACCEPT)
    assert G.scores(state) == [0.0, 1.0]


@pytest.mark.parametrize(
    ("action", "word"),
    [
        ({"action": "accept"}, "no offer"),
        ({"action": "reject"}, "only reject"),
        ({"action": "pass"}, "must be"),
        ({}, "must be"),
        ({"action": "offer"}, "whole number"),
        ({"action": "offer", "book": 0, "hat": 0}, "whole number"),
        ({"action": "offer", "book": 9, "hat": 0, "ball": 0}, "keep 0 to"),
        ({"action": "offer", "book": -1, "hat": 0, "ball": 0}, "keep 0 to"),
        ({"action": "offer", "book": True, "hat": 0, "ball": 0}, "whole number"),
        ({"action": "offer", "book": 1.5, "hat": 0, "ball": 0}, "whole number"),
        ({"action": "offer", "book": 0, "hat": 0, "ball": 0, "message": 3}, "text"),
        ("offer", "object"),
    ],
)
def test_illegal_actions(action, word):
    assert word in G.validate_action(STATE, 0, action)


def test_out_of_turn_and_after_the_end():
    assert "not your turn" in G.validate_action(STATE, 1, ALL)
    assert "over" in G.validate_action(play(ALL, ACCEPT), 0, ALL)


def test_apply_does_not_mutate():
    before = repr(STATE)
    G.apply(STATE, 0, ALL)
    assert repr(STATE) == before


def test_a_players_view_never_depends_on_the_other_players_values():
    other = (STATE.values[1][2], STATE.values[1][0], STATE.values[1][1])
    swapped = type(STATE)(Q, (STATE.values[0], other))
    assert G.player_view(STATE, 0) == G.player_view(swapped, 0)
    assert "values" not in G.player_view(STATE, 0)


def test_spectators_see_both_value_sets():
    view = G.spectator_view(play(ALL, ACCEPT))
    assert view["values"] == [list(v) for v in STATE.values]
    assert view["scores"] == [1.0, 0.0]
    assert view["shares"][0] == list(Q)


def test_messages_reach_the_other_player():
    state = play(offer(0, 0, 0, message="hats are worth nothing to me"))
    text = G.render_view(G.player_view(state, 1))
    assert "hats are worth nothing to me" in text
    assert "Standing offer: you would get" in text


def test_long_messages_are_cut_and_short_ones_left_alone():
    long = offer(0, 0, 0, message="x" * (MAX_MESSAGE + 50))
    assert len(G.normalize_action(STATE, 0, long)["message"]) == MAX_MESSAGE
    assert G.normalize_action(STATE, 0, offer(0, 0, 0, message="hi")) == offer(
        0, 0, 0, message="hi"
    )
    assert G.normalize_action(STATE, 0, ALL) == ALL


def test_render_tells_a_player_what_a_standing_offer_is_worth():
    text = G.render_view(G.player_view(play(NONE), 1))
    assert f"worth {TOTAL_VALUE} to you" in text


def test_the_action_schema_lists_the_table():
    schema = G.action_spec(STATE, 0).input_schema
    assert schema["required"] == ["action"]
    assert schema["properties"]["book"]["maximum"] == Q[0]
    assert schema["properties"]["message"]["maxLength"] == MAX_MESSAGE
    assert schema["additionalProperties"] is False


def test_random_players_can_play_through_the_real_engine():
    for seed in range(30):
        result, turns = run(RandomPlayer("a", seed), RandomPlayer("b", seed + 1), seed)
        spec = MatchSpec("m", "split_deal", seed, ["a", "b"])
        *_, final = iter_states(G, spec, turns)
        assert G.scores(final) == result.scores


def test_a_forfeit_uses_an_offer_clears_the_table_and_removes_nobody():
    state = G.forfeit(play(ALL), 1)
    assert state.pending is None
    assert state.used == (1, 1)
    assert state.to_move == 0
    assert "no offer" in G.validate_action(state, 0, ACCEPT)


def test_three_bad_attempts_forfeit_through_the_real_engine():
    bad = {"action": "pass"}
    a = ScriptedPlayer([NONE, ALL, ACCEPT], "a")
    b = ScriptedPlayer([bad, bad, bad, NONE], "b")
    result, turns = run(a, b)
    assert result.forfeits == [1]
    assert turns[1].forfeited
    assert result.scores == [1.0, 0.0]


def test_a_forfeit_with_no_offers_left_ends_with_no_deal():
    state = G.forfeit(play(*[ALL] * (2 * MAX_OFFERS)), 0)
    assert G.is_terminal(state)
    assert G.scores(state) == [0.0, 0.0]


def test_a_forfeiter_keeps_its_placement():
    state = G.apply(G.forfeit(STATE, 0), 1, NONE)
    state = G.apply(state, 0, ACCEPT)
    assert G.scores(state) == [1.0, 0.0]  # seat 1 kept nothing, so seat 0 took the table
    assert G.final_placements(state, [0]) == [1, 2]  # a forfeit doesn't drop the winner


def test_the_bot_concedes_step_by_step_and_never_asks_for_less_than_half():
    bot = SplitDealBot()
    targets = [bot._target(left) for left in range(MAX_OFFERS, 0, -1)]
    assert targets[0] == 10 and targets[-1] == 5
    assert targets == sorted(targets, reverse=True)
    assert len(set(targets)) == MAX_OFFERS


def test_two_bots_nearly_always_reach_a_deal():
    deals = sum(
        max(run(SplitDealBot("a"), SplitDealBot("b"), seed)[0].scores) > 0 for seed in range(200)
    )
    assert deals >= 190  # about 1 in 80 ends with a last offer worth 0 to the bot


def test_the_bot_takes_any_value_rather_than_no_deal_at_the_end():
    keeps_one_less = offer(Q[0] - 1, *Q[1:])  # leaves one book, for a bot that values books
    state = play(*[ALL, keeps_one_less] * MAX_OFFERS)
    ctx_view = G.player_view(state, 0)
    ctx = TurnContext("split_deal", 0, "", ctx_view, "", None, None)  # type: ignore
    worth_it = STATE.values[0][0] > 0
    action = asyncio.run(SplitDealBot().act(ctx)).action
    assert action == (ACCEPT if worth_it else {"action": "reject"})


def test_the_bot_is_stateless():
    state = play(ALL)
    ctx = TurnContext("split_deal", 1, "", G.player_view(state, 1), "", None, None)  # type: ignore
    assert asyncio.run(SplitDealBot("a").act(ctx)) == asyncio.run(SplitDealBot("b").act(ctx))


def test_efficiency_measures_value_made_against_the_best_split():
    a, b = STATE.values
    best = tuple(q if x > y else 0 for q, x, y in zip(Q, a, b, strict=True))
    state = play(offer(*best), ACCEPT)
    assert max_joint_value(state) >= TOTAL_VALUE
    assert efficiency(state) == pytest.approx(1.0)
    assert efficiency(play(*[ALL] * 12, {"action": "reject"})) == 0.0


@pytest.mark.parametrize(
    ("message", "claims"),
    [
        ("Hats are worth 3 to me", [("hat", 3)]),
        ("a book is worth nothing to me", [("book", 0)]),
        ("I value balls at 2", [("ball", 2)]),
        ("I value the books at four", [("book", 4)]),
        ("balls are worthless to me", [("ball", 0)]),
        ("hats are useless to me", [("hat", 0)]),
        ("I don't need hats", [("hat", 0)]),
        ("I do not care about the balls", [("ball", 0)]),
        ("I don't want any books", [("book", 0)]),
        ("books are worth 2 and hats are worth 1", [("book", 2), ("hat", 1)]),
        ("hats are worth 3 to you", []),
        ("I really like hats", []),
        ("Let's split it evenly", []),
        ("", []),
    ],
)
def test_value_claims_are_literal(message, claims):
    assert classify_value_claims(message) == claims


def test_stats_separate_true_and_false_claims():
    item, value = ITEMS[0], STATE.values[0][0]
    wrong = (value + 1) % 10
    state = play(
        offer(0, 0, 0, message=f"{item}s are worth {value}"),
        ALL,
        offer(0, 0, 0, message=f"{item}s are worth {wrong}"),
        ACCEPT,
    )
    stats = seat_stats(state, 0)
    assert (stats["true_claims"], stats["false_claims"]) == (1, 1)
    assert stats["offers"] == 2
    assert stats["deal"] is True


def test_the_prompt_worked_example_is_right():
    system = load_prompt("split_deal/v1").system
    assert worth((3, 2, 0), (2, 2, 2)) == 10
    assert worth((3, 2, 0), (2, 1, 0)) == 8
    assert "= 0.8" in system


def test_ratings_read_placements_because_scores_are_not_zero_sum():
    no_deal = play(*[ALL] * 12, {"action": "reject"})
    assert seat_zero_outcome(G.final_placements(no_deal, [])) == 0.5
    assert seat_zero_outcome([1, 2]) == 1.0
    assert seat_zero_outcome([2, 1]) == 0.0


def test_bots_reach_tournaments_through_the_roster_and_the_cli():
    assert isinstance(build_bot("split_deal", "x", 1), SplitDealBot)
    assert isinstance(_build_player(None, "split-deal-bot")[0], SplitDealBot)


def test_labels_and_results():
    assert move_label("split_deal", offer(1, 0, 2)) == "keep 1 book, 0 hat, 2 ball"
    assert move_label("split_deal", ACCEPT) == "accept"
    assert move_label("split_deal", {"action": "reject"}) == "reject"
    m = {"game": "split_deal", "status": "completed", "seats": ["a", "b"], "scores": [0.0, 0.0]}
    assert match_result(m) == "No deal"
    assert match_result(m | {"scores": [0.7, 0.4]}) == "a won"
    assert match_result(m | {"scores": [0.5, 0.5]}) == "Draw"


def test_every_table_has_a_legal_split():
    for q in tables():
        assert list(product(*(range(n + 1) for n in q)))
