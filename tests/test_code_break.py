import asyncio
import json
from statistics import mean

import pytest

from arena.cli import _build_player
from arena.engine import InMemoryTurnStore, MatchRunner, MatchSpec
from arena.engine.match_runner import TurnRecord, iter_states
from arena.games.code_break import (
    ALL_CODES,
    COLOURS,
    MAX_GUESSES,
    PEG_FIELDS,
    CodeBreak,
    Guess,
    code_of,
    feedback,
    guess_action,
    make_secret,
    minimax_guess,
    seat_stats,
    solver_guesses,
)
from arena.games.registry import get_game, list_games
from arena.players.base import TurnContext
from arena.players.bots import CodeBreakRandomBot, CodeBreakSolver, ScriptedPlayer, build_bot
from arena.players.prompting import load_prompt
from arena.types import Usage
from arena.web import move_label

G = CodeBreak()
SEED = 7
SECRET = make_secret(SEED)
WRONG = next(c for c in ALL_CODES if not set(c) & set(SECRET))


def play(*codes):
    state = G.initial_state(2, SEED)
    for code in codes:
        seat = G.current_player(state)
        assert G.validate_action(state, seat, {"code": code}) is None
        state = G.apply(state, seat, {"code": code})
    return state


def run(a, b, seed=SEED):
    spec = MatchSpec("m", "code_break", seed, [a.player_id, b.player_id])
    store = InMemoryTurnStore()
    return asyncio.run(MatchRunner(G, [a, b], store).run(spec)), store.turns["m"]


def test_registered():
    assert "code_break" in list_games()
    assert isinstance(get_game("code_break"), CodeBreak)


def test_the_secret_comes_only_from_the_seed():
    assert make_secret(SEED) == make_secret(SEED)
    assert len({make_secret(s) for s in range(50)}) > 30
    assert G.initial_state(2, SEED).secret == SECRET


@pytest.mark.parametrize(
    ("guess", "secret", "told"),
    [
        ("RGBY", "RGBY", (4, 0)),
        ("RGBY", "YBGR", (0, 4)),
        ("RRGG", "RGRG", (2, 2)),
        ("RRRR", "RGBY", (1, 0)),
        ("RGGG", "GGRR", (1, 2)),
        ("OOPP", "RGBY", (0, 0)),
        ("RRBB", "RBRY", (1, 2)),  # the second B has nothing left to match
    ],
)
def test_feedback(guess, secret, told):
    assert feedback(guess, secret) == told
    assert feedback(secret, guess) == told


def test_fewer_guesses_wins_whichever_seat():
    assert G.scores(play(SECRET, WRONG)) == [1.0, 0.0]
    assert G.scores(play(WRONG, SECRET)) == [0.0, 1.0]


def test_the_second_player_gets_a_last_chance_to_tie():
    state = play(SECRET)
    assert not G.is_terminal(state)
    assert G.current_player(state) == 1
    assert G.player_view(state, 1)["opponent_solved"] is True
    assert G.scores(play(SECRET, SECRET)) == [0.5, 0.5]


def test_seat_one_solving_first_ends_the_game_at_once():
    state = play(WRONG, SECRET)
    assert G.is_terminal(state)
    assert len(state.guesses[0]) == 1


def test_seat_zero_plays_on_up_to_the_other_seats_guess():
    state = play(WRONG, WRONG, WRONG, SECRET)
    assert G.is_terminal(state)
    assert G.scores(state) == [0.0, 1.0]


def test_a_seat_that_solved_is_skipped_until_the_end():
    state = play(WRONG, WRONG, SECRET)
    assert G.current_player(state) == 1
    assert G.player_view(state, 1)["guess_limit"] == 2
    state = G.apply(state, 1, {"code": WRONG})
    assert G.is_terminal(state)


def test_neither_solving_in_the_limit_is_a_draw():
    state = play(*[WRONG] * (2 * MAX_GUESSES))
    assert G.is_terminal(state)
    assert G.scores(state) == [0.5, 0.5]


@pytest.mark.parametrize(
    ("action", "word"),
    [
        ({"code": "RGB"}, "peg1"),
        ({"code": "RGBYO"}, "peg1"),
        ({"code": "RGBX"}, "peg1"),
        ({"code": "rgby"}, "peg1"),
        ({"code": 1234}, "peg1"),
        ({}, "peg1"),
        ({"peg1": "R", "peg2": "G", "peg3": "B"}, "peg1"),
        ({"peg1": "R", "peg2": "G", "peg3": "B", "peg4": "X"}, "peg1"),
        ({"peg1": "R", "peg2": "G", "peg3": "B", "peg4": 1}, "peg1"),
        ({"peg1": "RR", "peg2": "G", "peg3": "B", "peg4": "Y"}, "peg1"),
        ({"peg1": "R", "peg2": "G", "peg3": "B", "peg4": "Y", "peg5": "O"}, "peg1"),
        ({"code": "RGBY", "peg1": "R"}, "peg1"),
        ("RGBY", "object"),
    ],
)
def test_illegal_actions(action, word):
    assert word in G.validate_action(G.initial_state(2, SEED), 0, action)


def test_out_of_turn_and_after_the_end():
    state = G.initial_state(2, SEED)
    assert "not your turn" in G.validate_action(state, 1, {"code": "RGBY"})
    assert "over" in G.validate_action(play(WRONG, SECRET), 0, {"code": "RGBY"})


def test_apply_does_not_mutate():
    state = G.initial_state(2, SEED)
    before = repr(state)
    G.apply(state, 0, {"code": WRONG})
    assert repr(state) == before


def test_views_never_hold_the_secret_or_the_other_seats_guesses():
    state = play(WRONG, "RGBY", "OOPP")
    for seat in (0, 1):
        view = G.player_view(state, seat)
        assert SECRET not in json.dumps(view) or SECRET in {g.code for g in state.guesses[seat]}
        assert "secret" not in view
        other = {g.code for g in state.guesses[1 - seat]} - {g.code for g in state.guesses[seat]}
        assert not any(code in json.dumps(view) for code in other)
    assert G.player_view(state, 0)["opponent_guesses"] == 1


def test_a_view_does_not_change_with_the_secret_except_through_feedback():
    a = G.initial_state(2, 1)
    b = G.initial_state(2, 2)
    assert a.secret != b.secret
    assert G.player_view(a, 0) == G.player_view(b, 0)


def test_spectators_see_everything():
    view = G.spectator_view(play(WRONG, SECRET))
    assert view["secret"] == SECRET
    assert view["guesses"][1][0]["exact"] == 4
    assert view["scores"] == [0.0, 1.0]
    assert view["to_move"] is None


def test_render_tells_a_player_their_feedback_and_the_limit():
    text = G.render_view(G.player_view(play(WRONG, SECRET), 0))
    assert f"1. {WRONG}: exact 0, colour 0" in text
    assert "Your opponent has solved it" in text


def test_replay_is_deterministic():
    result, turns = run(CodeBreakRandomBot("a", 1), CodeBreakRandomBot("b", 2))
    spec = MatchSpec("m", "code_break", SEED, ["a", "b"])
    *_, final = iter_states(G, spec, turns)
    assert G.scores(final) == result.scores


def test_a_forfeit_skips_the_guess_and_removes_nobody():
    bad = {"code": "nope"}
    a = ScriptedPlayer([bad, bad, bad], "a")
    b = ScriptedPlayer([{"code": SECRET}], "b")
    # Seat 0 forfeits its guess, then seat 1 solves on guess 1: the game is over.
    result, turns = run(a, b)
    assert result.scores == [0.0, 1.0]
    assert turns[0].forfeited
    assert result.forfeits == [0]
    assert result.placements == [2, 1]


def test_a_forfeit_counts_towards_the_limit_and_can_still_draw():
    state = G.initial_state(2, SEED)
    state = G.forfeit(state, 0)
    assert state.guesses[0] == (Guess(None),)
    assert state.to_move == 1
    state = G.apply(G.apply(state, 1, {"code": SECRET}), 0, {"code": SECRET})
    assert G.is_terminal(state)
    assert G.scores(state) == [0.0, 1.0]  # seat 1 solved on guess 1, seat 0 on 2


def test_placements_ignore_forfeits_when_a_forfeiter_wins():
    state = G.forfeit(G.initial_state(2, SEED), 0)
    state = G.apply(state, 1, {"code": WRONG})
    state = G.apply(state, 0, {"code": SECRET})
    state = G.apply(state, 1, {"code": WRONG})
    assert G.is_terminal(state)
    assert G.scores(state) == [1.0, 0.0]
    assert G.final_placements(state, [0]) == [1, 2]


def test_the_solver_never_needs_more_than_five_on_any_secret():
    counts = [solver_guesses(secret) for secret in ALL_CODES]
    assert max(counts) <= 5
    assert mean(counts) < 4.5


def test_the_solver_plays_through_the_real_engine():
    result, turns = run(CodeBreakSolver("s"), ScriptedPlayer([{"code": WRONG}] * 5, "w"))
    assert result.scores == [1.0, 0.0]
    assert sum(t.seat == 0 for t in turns) == solver_guesses(SECRET)


def test_random_consistent_guesses_never_contradict_their_feedback():
    for seed in range(20):
        _, turns = run(CodeBreakRandomBot("a", seed), CodeBreakRandomBot("b", seed + 100), seed)
        spec = MatchSpec("m", "code_break", seed, ["a", "b"])
        *_, final = iter_states(G, spec, turns)
        assert seat_stats(final, 0)["inconsistent"] == 0
        assert seat_stats(final, 1)["inconsistent"] == 0


def test_bots_are_stateless_and_seeded():
    ctx = TurnContext(
        "code_break",
        0,
        "",
        G.player_view(play(WRONG, WRONG), 0),
        "",
        None,
        None,  # type: ignore
    )
    assert asyncio.run(CodeBreakRandomBot("a", 3).act(ctx)) == asyncio.run(
        CodeBreakRandomBot("b", 3).act(ctx)
    )
    assert code_of(asyncio.run(CodeBreakSolver().act(ctx)).action) in ALL_CODES


def test_stats_count_guesses_that_contradict_earlier_feedback():
    # Guessing WRONG twice: the second can't be the secret, since the first scored 0 exact.
    stats = seat_stats(play(WRONG, WRONG, WRONG, WRONG, SECRET, WRONG), 0)
    assert stats["inconsistent"] == 1
    assert stats["solved"] is True
    assert stats["above_solver"] == 3 - solver_guesses(SECRET)
    assert seat_stats(play(WRONG, WRONG, WRONG, WRONG, SECRET, WRONG), 1)["above_solver"] is None


def test_minimax_opens_with_knuths_guess_and_fails_on_impossible_feedback():
    assert minimax_guess(()) == "RRGG"
    with pytest.raises(ValueError):
        minimax_guess((("RRRR", (4, 0)), ("GGGG", (4, 0))))


def test_bots_reach_tournaments_through_the_roster_and_the_cli():
    assert isinstance(build_bot("code_break", "x", 1), CodeBreakSolver)
    assert isinstance(build_bot("code_break_random", "x", 1), CodeBreakRandomBot)
    assert isinstance(_build_player(None, "code-break-solver")[0], CodeBreakSolver)
    assert isinstance(_build_player(None, "code-break-random")[0], CodeBreakRandomBot)


def test_move_label():
    assert move_label("code_break", {"code": "RGBY"}) == "guess RGBY"
    assert move_label("code_break", guess_action("RGBY")) == "guess RGBY"
    assert move_label("code_break", None, True) == "forfeit"


def test_both_action_forms_play_the_same_guess():
    pegs = guess_action(WRONG)
    state = G.initial_state(2, SEED)
    assert set(pegs) == set(PEG_FIELDS)
    assert G.validate_action(state, 0, pegs) is None
    assert G.validate_action(state, 0, {"code": WRONG}) is None
    assert G.apply(state, 0, pegs) == G.apply(state, 0, {"code": WRONG})


def test_a_log_in_the_old_form_still_replays():
    record = TurnRecord(0, 0, {"code": SECRET}, False, 1, None, None, Usage(), 0.0, 0)
    spec = MatchSpec("m", "code_break", SEED, ["a", "b"])
    *_, state = iter_states(G, spec, [record])
    assert state.guesses[0][0].solved


def test_the_tool_schema_has_one_required_enum_per_peg():
    schema = G.action_spec(G.initial_state(2, SEED), 0).input_schema
    assert schema["required"] == list(PEG_FIELDS)
    assert all(schema["properties"][f]["enum"] == list(COLOURS) for f in PEG_FIELDS)
    assert schema["additionalProperties"] is False


def test_the_v2_prompt_worked_example_is_right():
    prompt = load_prompt("code_break/v2")
    assert feedback("RBOB", "RGBB") == (2, 1)
    for word in ("R G B B", "R B O B", "exact 2, colour 1"):
        assert word in prompt.system
    assert "right colour, wrong place" in prompt.turn


def test_v2_renders_with_a_real_view():
    state = play(WRONG, WRONG)
    view = G.player_view(state, 0)
    ctx = TurnContext(
        "code_break",
        0,
        G.rules_text(),
        view,
        G.render_view(view),
        G.action_spec(state, 0),
        None,
    )
    prompt = load_prompt("code_break/v2")
    assert "exact 0, colour 0" in prompt.render_turn(ctx)
    assert "Rules:" in prompt.render_system(ctx)
