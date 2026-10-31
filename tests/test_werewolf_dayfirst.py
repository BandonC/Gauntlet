"""werewolf_dayfirst: Werewolf opening on day 1. `werewolf` itself must not change."""

import copy
import json
import pathlib
from collections import Counter

import pytest

from arena.engine import InMemoryTurnStore, MatchRunner, MatchSpec
from arena.engine.match_runner import iter_states
from arena.games.registry import get_game, list_games
from arena.games.werewolf import (
    DAY_FIRST_RULES,
    RULES,
    SEATS,
    SEER,
    VILLAGER,
    WOLF,
    Werewolf,
    WerewolfDayFirst,
)
from arena.players.bots import WerewolfBot
from arena.players.prompting import load_prompt
from arena.roster import config_hash, load_roster

G = WerewolfDayFirst()
STYLES = ("plain", "cautious", "aggressive", "analytical")
EXAMPLE = pathlib.Path(__file__).parent.parent / "roster.example.toml"
ROLES = [WOLF, WOLF, SEER, VILLAGER, VILLAGER, VILLAGER, VILLAGER]
OPTIONS = {"roles": ROLES}
SAY = {"action": "say", "message": "hello"}


def bots(seed=0):
    return [WerewolfBot(f"b{i}", seed * 10 + i) for i in range(SEATS)]


def spec(seed, options=None):
    return MatchSpec(f"df{seed}", G.name, seed, [f"b{i}" for i in range(SEATS)], options)


def start(seed=7, options=OPTIONS):
    return G.initial_state(SEATS, seed, options)


def play(s, *actions):
    for a in actions:
        seat = G.current_player(s)
        assert G.validate_action(s, seat, a) is None, (seat, a)
        s = G.apply(s, seat, a)
    return s


def kill(t):
    return {"action": "kill", "target": t}


def inspect(t):
    return {"action": "inspect", "target": t}


def vote(t):
    return {"action": "vote", "target": t}


def to_vote(s):
    while s.phase == "discussion":
        s = play(s, SAY)
    return s


def votes_for(s, target):
    """Everyone votes target, and target votes for another living seat."""
    other = next(seat for seat in s.alive if seat != target)
    return play(s, *[vote(target if seat != target else other) for seat in s.alive])


# --- what differs from werewolf -------------------------------------------------------


def test_day_one_starts_with_all_seven_alive_and_nobody_dead():
    s = start()
    assert s.alive == tuple(range(7)) and s.deaths == () and s.night_votes == ()
    assert s.phase == "discussion" and s.day == 1 and s.discussion_round == 1
    assert G.current_player(s) == 0 and s.inspections == ()


def test_day_one_is_two_rounds_of_all_seven_then_a_vote():
    s = start()
    for round_no in (1, 2):
        assert s.discussion_round == round_no and len(s.queue) == 7
        s = play(s, *[SAY] * 7)
    assert s.phase == "vote" and s.queue == s.alive and len(s.messages) == 14


def test_the_first_kill_and_inspection_come_on_night_two():
    s = votes_for(to_vote(start()), 3)
    assert s.phase == "night" and s.day == 2 and len(s.alive) == 6
    assert list(s.queue) == [0, 1, 2]
    s = play(s, kill(4), kill(4), inspect(0))
    assert [(d.day, d.seat, d.cause) for d in s.deaths] == [(1, 3, "vote"), (2, 4, "night")]
    assert s.inspections[0].night == 2 and s.phase == "discussion" and s.day == 2


def test_a_tied_first_vote_goes_to_night_two_with_everyone_alive():
    s = to_vote(start())
    s = play(s, vote(1), vote(0), vote(0), vote(1), vote(0), vote(1), vote(2))
    assert s.alive == tuple(range(7)) and s.phase == "night" and s.day == 2


def test_nothing_about_the_opening_shows_who_acts_at_night():
    view = G.player_view(start(), 3)
    assert view["seer_results"] == [] and view["wolf_votes"] == []
    assert G.render_view(view).count("--- Day") == 1
    assert "Day 1, discussion round 1 of 2" in G.render_view(view)


def test_only_the_opening_line_of_the_rules_changes():
    assert "starting with day 1" in DAY_FIRST_RULES and "night 1" not in DAY_FIRST_RULES
    assert DAY_FIRST_RULES.replace("day 1", "night 1") == RULES
    assert G.rules_text() == DAY_FIRST_RULES


def test_the_token_limit_measures_this_games_rules():
    class LongRules(WerewolfDayFirst):
        def rules_text(self):
            return "x" * 100_000

    s = votes_for(to_vote(start()), 3)
    s = play(s, kill(4), kill(4), inspect(0))
    assert not any(d["reduced"] for d in G.player_view(s, 0)["days"])
    assert LongRules().player_view(s, 0)["days"][0]["reduced"]


def test_registered_under_its_own_id_and_werewolf_is_unchanged():
    assert get_game("werewolf_dayfirst").name == "werewolf_dayfirst"
    assert {"werewolf", "werewolf_dayfirst"} <= set(list_games())
    old = Werewolf().initial_state(SEATS, 7, OPTIONS)
    assert old.phase == "night" and old.day == 1 and Werewolf().rules_text() == RULES
    assert "starting with night 1" in RULES


def test_the_same_seed_deals_the_same_roles_as_werewolf():
    for seed in range(20):
        assert G.initial_state(SEATS, seed).roles == Werewolf().initial_state(SEATS, seed).roles


# --- the same engine checks as werewolf -----------------------------------------------


def test_only_seven_seats_and_only_a_permutation_of_the_roles():
    with pytest.raises(ValueError):
        G.initial_state(6, 1)
    with pytest.raises(ValueError):
        G.initial_state(SEATS, 1, {"roles": [WOLF] * 7})


def test_village_wins_when_no_wolf_is_left():
    s = votes_for(to_vote(start()), 0)
    s = play(s, kill(5), inspect(1))
    s = votes_for(to_vote(s), 1)
    assert s.winner == "village" and s.phase == "over"
    assert G.scores(s) == [0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    assert G.placements(s) == [6, 6, 1, 1, 1, 1, 1]


def test_wolves_win_at_parity_after_a_vote():
    s = votes_for(to_vote(start()), 3)
    s = play(s, kill(4), kill(4), inspect(0))
    assert len(s.alive) == 5 and s.winner is None
    s = votes_for(to_vote(s), 5)
    assert s.winner == "wolves" and s.phase == "over" and len(s.alive) == 4
    assert G.scores(s) == [1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]


def test_wolves_win_at_parity_after_a_kill():
    s = votes_for(to_vote(start()), 3)
    s = play(s, kill(4), kill(4), inspect(0))
    s = play(to_vote(s), vote(2), vote(5), vote(5), vote(2), vote(0))  # 2-2, nobody goes
    assert len(s.alive) == 5 and s.phase == "night" and s.day == 3
    s = play(s, kill(6), kill(6), inspect(1))
    assert s.winner == "wolves" and s.deaths[-1].cause == "night"


def test_illegal_actions_are_rejected():
    s = start()
    assert G.validate_action(s, 1, SAY) == "it is not your turn"
    assert G.validate_action(s, 0, kill(3)) == "action must be 'say' now"
    assert G.validate_action(s, 0, {"action": "say", "message": "  "})
    v = to_vote(s)
    assert "must be one of" in G.validate_action(v, 0, vote(0))
    assert "must be one of" in G.validate_action(v, 0, vote(9))
    assert G.validate_action(v, 0, kill(3)) == "action must be 'vote' now"


def test_apply_does_not_mutate():
    s = start()
    before = copy.deepcopy(s)
    G.apply(s, 0, SAY)
    assert s == before


def test_forfeits_skip_the_action_and_keep_the_seat():
    s = G.forfeit(start(), 0)
    assert s.messages == () and 0 in s.alive and s.forfeited == (0,)
    s = votes_for(to_vote(s), 3)
    s = G.forfeit(G.forfeit(s, 0), 1)  # no wolf votes: the kill is drawn from the seed
    s = play(s, inspect(0))
    assert s.deaths[-1].cause == "night" and s.deaths[-1].role != WOLF


@pytest.mark.parametrize("seed", range(6))
async def test_player_view_never_depends_on_roles_the_seat_cannot_know(seed):
    out = await MatchRunner(G, bots(seed), InMemoryTurnStore()).run(spec(seed))
    states = list(iter_states(G, spec(seed), out.turns))
    roles = states[0].roles
    for s in states:
        for seat in s.alive:
            view = G.player_view(s, seat)
            text = json.dumps(view) + G.render_view(view)
            assert view["role"] == roles[seat]
            if roles[seat] != WOLF:
                assert view["fellow_wolves"] == [] and view["wolf_votes"] == []
                assert "fellow wolves" not in text.lower()
            if roles[seat] != SEER:
                assert view["seer_results"] == []
            dead = {d["seat"] for day in view["days"] for d in day["deaths"]}
            assert dead == {d.seat for d in s.deaths}


@pytest.mark.parametrize("seed", range(20))
async def test_every_bot_game_terminates_and_replays_exactly(seed):
    out = await MatchRunner(G, bots(seed), InMemoryTurnStore()).run(spec(seed))
    assert sum(out.scores) in (2.0, 5.0)
    first = list(iter_states(G, spec(seed), out.turns))
    assert first == list(iter_states(G, spec(seed), out.turns))
    assert first[0].phase == "discussion" and len(first[0].alive) == 7
    assert first[-1].winner is not None


async def test_both_teams_win_some_bot_games():
    wins = Counter()
    for seed in range(60):
        out = await MatchRunner(G, bots(seed), InMemoryTurnStore()).run(spec(seed))
        wins[sum(out.scores)] += 1
    assert wins[2.0] and wins[5.0]


async def test_a_match_resumes_from_its_log_on_a_new_runner():
    store = InMemoryTurnStore()
    whole = await MatchRunner(G, bots(3), InMemoryTurnStore()).run(spec(3))
    for turn in whole.turns[:9]:
        await store.append_turn(spec(3).match_id, turn)
    resumed = await MatchRunner(G, bots(3), store).run(spec(3))
    assert resumed.scores == whole.scores and len(resumed.turns) == len(whole.turns)


# --- prompts and roster ---------------------------------------------------------------


@pytest.mark.parametrize("style", STYLES)
def test_prompts_have_the_same_style_and_length_line_as_werewolf_v2(style):
    prompt = load_prompt(f"werewolf_dayfirst/v1-{style}")
    v2 = load_prompt(f"werewolf/v2-{style}")
    assert prompt.system == v2.system and prompt.turn == v2.turn
    assert "under 300 characters" in prompt.system and "night" not in prompt.system.lower()


def test_the_example_roster_has_eight_new_day_first_players():
    players = [
        p
        for p in load_roster(EXAMPLE).players.values()
        if p.prompt.startswith("werewolf_dayfirst/")
    ]
    assert len(players) == 8 and len({config_hash(p) for p in players}) == 8
    assert all("/wwdf-v1-" in p.id for p in players)
    assert {p.prompt for p in players} == {f"werewolf_dayfirst/v1-{s}" for s in STYLES}
    assert {p.model for p in players} == {"claude-haiku-5-5", "claude-haiku-4-5-20251001"}
