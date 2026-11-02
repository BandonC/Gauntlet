import asyncio
import json
from collections import Counter

import pytest

from arena.cli import _build_player
from arena.engine import InMemoryTurnStore, MatchRunner, MatchSpec
from arena.engine.match_runner import iter_states
from arena.games.registry import get_game, list_games
from arena.games.word_link import (
    BOARD_SIZE,
    MAX_CLUES,
    NEUTRAL,
    TEAM_WORDS,
    TRAP,
    A,
    B,
    WordLink,
    clue_problem,
    deal_board,
    team_stats,
)
from arena.games.word_link_words import TOPICS, WORDS
from arena.players.base import TurnContext
from arena.players.bots import (
    ScriptedPlayer,
    WordLinkClueBot,
    WordLinkGuessBot,
    WordLinkRandomGuesser,
    build_bot,
)
from arena.ratings.weng_lin import WengLin, rate, rate_teams
from arena.tournaments import team_round_robin
from arena.web import match_result, move_label

G = WordLink()
SEED = 3
STATE = G.initial_state(4, SEED)
KEY = dict(zip(STATE.board, STATE.key, strict=True))


def words(kind):
    return [w for w, k in KEY.items() if k == kind]


def clue(word="xylophone", number=1):
    return {"word": word, "number": number}


def guess(word):
    return {"action": "guess", "word": word}


STOP = {"action": "stop"}


def play(*actions):
    state = G.initial_state(4, SEED)
    for action in actions:
        seat = G.current_player(state)
        assert G.validate_action(state, seat, action) is None, action
        state = G.apply(state, seat, action)
    return state


def run(players, seed=SEED):
    spec = MatchSpec("m", "word_link", seed, [p.player_id for p in players])
    store = InMemoryTurnStore()
    return asyncio.run(MatchRunner(G, players, store).run(spec)), store.turns["m"]


def bots(seed=1):
    return [
        WordLinkClueBot("a"),
        WordLinkGuessBot("b", seed),
        WordLinkClueBot("c"),
        WordLinkGuessBot("d", seed + 1),
    ]


def test_registered_with_two_teams():
    assert "word_link" in list_games()
    assert get_game("word_link").teams(4) == ((0, 1), (2, 3))


def test_the_word_list_is_clean_and_topics_are_legal_clues():
    assert len(WORDS) == len(set(WORDS)) >= 100
    assert all(w.isalpha() and w.islower() for w in WORDS)
    assert not set(TOPICS) & set(WORDS)
    for topic in TOPICS:
        assert all(topic not in w and w not in topic for w in WORDS)


def test_a_board_comes_only_from_the_seed_and_has_the_right_mix():
    for seed in range(50):
        board, key = deal_board(seed)
        assert len(set(board)) == BOARD_SIZE
        assert Counter(key) == {A: TEAM_WORDS, B: TEAM_WORDS, NEUTRAL: 3, TRAP: 1}
    assert deal_board(SEED) == deal_board(SEED)
    assert deal_board(1) != deal_board(2)


def test_turn_order_runs_clue_then_guess_then_the_other_team():
    assert G.current_player(STATE) == 0
    state = play(clue())
    assert G.current_player(state) == 1
    state = play(clue(), guess(words(B)[0]))
    assert G.current_player(state) == 2


def test_an_own_word_lets_you_guess_again_and_stop():
    state = play(clue(number=2), guess(words(A)[0]))
    assert G.current_player(state) == 1
    assert G.current_player(G.apply(state, 1, STOP)) == 2


def test_guesses_run_out_at_the_number_plus_one():
    state = play(clue(number=1), guess(words(A)[0]), guess(words(A)[1]))
    assert G.current_player(state) == 2


def test_a_neutral_word_ends_the_turn():
    state = play(clue(number=3), guess(words(NEUTRAL)[0]))
    assert G.current_player(state) == 2


def test_the_other_teams_word_counts_for_them():
    state = play(clue(number=3), guess(words(B)[0]))
    assert G.player_view(state, 0)["left"] == {A: 6, B: 5}


def test_the_trap_loses_at_once():
    state = play(clue(), guess(words(TRAP)[0]))
    assert G.is_terminal(state)
    assert G.scores(state) == [0.0, 0.0, 1.0, 1.0]


def test_revealing_your_sixth_word_wins():
    own = words(A)
    state = play(clue(number=6), *[guess(w) for w in own])
    assert G.is_terminal(state)
    assert G.scores(state) == [1.0, 1.0, 0.0, 0.0]


def test_revealing_the_other_teams_last_word_gives_them_the_game():
    state = STATE
    for word in words(B):
        state = G.apply(state, 0, clue())
        state = G.apply(state, 1, guess(word))
        if not state.over:
            state = G.forfeit(state, 2)  # team B passes, so team A keeps guessing
    assert G.scores(state) == [0.0, 0.0, 1.0, 1.0]


def test_the_clue_limit_ends_a_game_of_skipped_clues_in_a_draw():
    state = STATE
    for _ in range(MAX_CLUES):
        assert not G.is_terminal(state)
        state = G.forfeit(state, G.current_player(state))
    assert G.is_terminal(state)
    assert G.scores(state) == [0.5, 0.5, 0.5, 0.5]


def test_the_clue_limit_goes_to_the_team_with_more_revealed():
    state = play(clue(number=1), guess(words(A)[0]), STOP)
    while not G.is_terminal(state):
        state = G.forfeit(state, G.current_player(state))
    assert G.scores(state) == [1.0, 1.0, 0.0, 0.0]


@pytest.mark.parametrize(
    ("word", "reason"),
    [
        (None, "must be a word"),
        (3, "must be a word"),
        ("two words", "one word"),
        ("well-known", "one word"),
        ("r2d2", "one word"),
        ("", "one word"),
    ],
)
def test_clues_must_be_one_word(word, reason):
    assert reason in clue_problem(word, STATE.board)


def test_clues_cannot_be_near_a_board_word():
    word = STATE.board[0]
    assert "too close" in clue_problem(word, STATE.board)
    assert "too close" in clue_problem(word.upper(), STATE.board)
    assert "too close" in clue_problem(word + "s", STATE.board)
    assert "too close" in clue_problem(word[:-1] if len(word) > 3 else word, STATE.board)
    assert clue_problem("xylophone", STATE.board) is None


@pytest.mark.parametrize(
    ("action", "word"),
    [
        ({"word": "xylophone"}, "whole number"),
        ({"word": "xylophone", "number": 0}, "1 to"),
        ({"word": "xylophone", "number": 7}, "1 to"),
        ({"word": "xylophone", "number": True}, "whole number"),
        ({"number": 2}, "must be a word"),
        ("xylophone", "object"),
    ],
)
def test_illegal_clues(action, word):
    assert word in G.validate_action(STATE, 0, action)


def test_illegal_guesses():
    state = play(clue())
    assert "at least one" in G.validate_action(state, 1, STOP)
    assert "not on the board" in G.validate_action(state, 1, guess("xylophone"))
    assert "needs a word" in G.validate_action(state, 1, {"action": "guess"})
    assert "must be" in G.validate_action(state, 1, {"action": "pass"})
    state = play(clue(number=2), guess(words(A)[0]))
    assert "already" in G.validate_action(state, 1, guess(words(A)[0]))


def test_out_of_turn_and_after_the_end():
    assert "not your turn" in G.validate_action(STATE, 1, clue())
    assert "over" in G.validate_action(play(clue(), guess(words(TRAP)[0])), 2, clue())


def test_guesses_ignore_case_and_a_bare_word_is_filled_in():
    word = words(A)[0]
    state = play(clue())
    assert G.validate_action(state, 1, guess(word.upper())) is None
    assert G.fill_action(state, 1, {"word": word}) == guess(word)
    assert G.fill_action(STATE, 0, {"word": word}) == {"word": word}


def test_apply_does_not_mutate():
    before = repr(STATE)
    G.apply(STATE, 0, clue())
    assert repr(STATE) == before


def test_only_clue_givers_see_the_key():
    state = play(clue())
    assert "key" in G.player_view(state, 0)
    assert "key" in G.player_view(state, 2)
    assert "key" not in G.player_view(state, 1)
    assert "key" not in G.player_view(state, 3)


def test_a_guessers_view_holds_nothing_about_hidden_kinds():
    state = play(clue(number=2), guess(words(A)[0]))
    view = json.dumps(G.player_view(state, 1))
    for kind in (TRAP, NEUTRAL):
        assert f'"{kind}"' not in view
    shuffled = type(STATE)(STATE.board, tuple(reversed(STATE.key)))
    assert G.player_view(shuffled, 1) == G.player_view(STATE, 1)


def test_a_guessers_text_lists_words_without_sides():
    text = G.render_view(G.player_view(play(clue("xylophone", 2)), 1))
    assert "Your clue: xylophone 2" in text
    assert "The trap" not in text and "Neutral words" not in text


def test_a_clue_givers_text_lists_every_side():
    text = G.render_view(G.player_view(STATE, 0))
    assert f"The trap: {words(TRAP)[0]}" in text
    assert "Your team's words: " in text


def test_the_tool_lists_only_legal_moves():
    state = play(clue())
    spec = G.action_spec(state, 1).input_schema
    assert spec["properties"]["action"]["enum"] == ["guess"]
    state = play(clue(number=2), guess(words(A)[0]))
    spec = G.action_spec(state, 1).input_schema
    assert spec["properties"]["action"]["enum"] == ["guess", "stop"]
    assert words(A)[0] not in spec["properties"]["word"]["enum"]
    assert G.action_spec(STATE, 0).name == "word_link_clue"


def test_bots_play_through_the_real_engine_and_replay():
    for seed in range(30):
        result, turns = run(bots(seed), seed)
        spec = MatchSpec("m", "word_link", seed, ["a", "b", "c", "d"])
        *_, final = iter_states(G, spec, turns)
        assert G.scores(final) == result.scores
        assert sum(result.scores) == 2.0


def test_the_bot_team_beats_a_random_guesser_team():
    wins = 0
    for seed in range(40):
        team = [WordLinkClueBot("a"), WordLinkGuessBot("b", seed)]
        rand = [WordLinkClueBot("c"), WordLinkRandomGuesser("d", seed)]
        result, _ = run(team + rand if seed % 2 else rand + team, seed)
        wins += result.scores[0 if seed % 2 else 2] == 1.0
    assert wins >= 30


def test_clue_bot_never_breaks_the_rules():
    for seed in range(60):
        _, turns = run(bots(seed), seed)
        assert not any(t.rejections for t in turns)


def test_the_guess_bot_is_stateless_and_seeded():
    state = play(clue())
    ctx = TurnContext("word_link", 1, "", G.player_view(state, 1), "", None, None)  # type: ignore
    assert asyncio.run(WordLinkGuessBot("a", 2).act(ctx)) == asyncio.run(
        WordLinkGuessBot("b", 2).act(ctx)
    )


def test_a_forfeited_clue_passes_and_a_forfeited_guess_ends_the_turn():
    state = G.forfeit(STATE, 0)
    assert G.current_player(state) == 2
    state = G.forfeit(play(clue()), 1)
    assert G.current_player(state) == 2


def test_a_forfeit_through_the_engine_removes_nobody():
    bad = {"word": "two words", "number": 1}
    trap = words(TRAP)[0]
    players = [
        ScriptedPlayer([bad, bad, bad], "a"),
        ScriptedPlayer([], "b"),
        ScriptedPlayer([clue()], "c"),
        ScriptedPlayer([guess(trap)], "d"),
    ]
    result, turns = run(players)
    assert result.forfeits == [0]
    assert turns[0].forfeited
    assert result.scores == [1.0, 1.0, 0.0, 0.0]
    assert result.placements == [1, 1, 3, 3]


def test_team_stats():
    state = play(
        clue(number=2), guess(words(A)[0]), guess(words(B)[0]), clue(), guess(words(TRAP)[0])
    )
    assert team_stats(state, 0)["own_words"] == 1
    assert team_stats(state, 0)["guesses"] == 2
    assert team_stats(state, 1)["trap_hits"] == 1
    assert team_stats(state, 0)["won"] is True


def test_team_ratings_match_the_one_player_case():
    a, b = WengLin(30.0, 2.0), WengLin(22.0, 6.0)
    assert [t[0] for t in rate_teams([[a], [b]], [2, 1])] == rate([a, b], [2, 1])


def test_team_ratings_share_the_result_by_uncertainty():
    sure, unsure = WengLin(25.0, 2.0), WengLin(25.0, 8.0)
    other = [WengLin(), WengLin()]
    (first, second), _ = rate_teams([[sure, unsure], other], [1, 2])
    assert first.mu > sure.mu and second.mu > unsure.mu
    assert second.mu - unsure.mu > first.mu - sure.mu  # the less certain player moves further


def test_a_team_win_moves_both_teams_opposite_ways_and_a_draw_is_level():
    teams = [[WengLin(), WengLin()], [WengLin(), WengLin()]]
    won, lost = rate_teams(teams, [1, 2])
    assert all(p.mu > 25 for p in won) and all(p.mu < 25 for p in lost)
    drew = rate_teams(teams, [1, 1])
    assert all(p.mu == pytest.approx(25.0) for team in drew for p in team)


def test_rate_teams_refuses_bad_input():
    with pytest.raises(ValueError):
        rate_teams([[WengLin()]], [1])
    with pytest.raises(ValueError):
        rate_teams([[WengLin()], []], [1, 2])


def test_team_schedule_swaps_who_goes_first():
    seatings = team_round_robin([["a", "b"], ["c", "d"], ["e", "f"]], 4)
    assert len(seatings) == 12
    firsts = Counter((s[0], s[2]) for s in seatings)
    assert firsts[("a", "c")] == firsts[("c", "a")] == 2
    assert all(len(set(s)) == 4 for s in seatings)


def test_bots_reach_tournaments_through_the_roster_and_the_cli():
    assert isinstance(build_bot("word_link_clue", "x", 1), WordLinkClueBot)
    assert isinstance(build_bot("word_link_guess", "x", 1), WordLinkGuessBot)
    assert isinstance(build_bot("word_link_random", "x", 1), WordLinkRandomGuesser)
    assert isinstance(_build_player(None, "word-link-clue-bot")[0], WordLinkClueBot)


def test_labels_and_results():
    assert move_label("word_link", clue("ocean", 2)) == "clue ocean 2"
    assert move_label("word_link", guess("whale")) == "guess whale"
    assert move_label("word_link", STOP) == "stop"
    m = {"game": "word_link", "status": "completed", "seats": list("abcd")}
    assert match_result(m | {"scores": [1, 1, 0, 0]}) == "a and b won"
    assert match_result(m | {"scores": [0.5] * 4}) == "Draw"
