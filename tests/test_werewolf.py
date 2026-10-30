import json
import random
from collections import Counter
from dataclasses import replace

import pytest

from arena.engine import InMemoryTurnStore, MatchRunner, MatchSpec
from arena.engine.match_runner import iter_states
from arena.games.connect4 import Connect4
from arena.games.liars_dice import LiarsDice
from arena.games.registry import get_game
from arena.games.werewolf import (
    DEFAULT_CONTEXT_TOKEN_LIMIT,
    MAX_MESSAGE_CHARS,
    RULES,
    SEATS,
    SEER,
    VILLAGER,
    WOLF,
    Werewolf,
    classify_claims,
)
from arena.players.base import Decision
from arena.players.bots import ScriptedPlayer, WerewolfBot, build_bot
from arena.players.prompting import load_prompt

G = Werewolf()
ROLES = [WOLF, WOLF, SEER, VILLAGER, VILLAGER, VILLAGER, VILLAGER]
OPTIONS = {"roles": ROLES}
SAY = {"action": "say", "message": "hello"}


def bots(seed=0):
    return [WerewolfBot(f"b{i}", seed * 10 + i) for i in range(SEATS)]


def spec(seed, options=None):
    return MatchSpec(f"ww{seed}", "werewolf", seed, [f"b{i}" for i in range(SEATS)], options)


def start(options=OPTIONS, seed=7):
    return G.initial_state(SEATS, seed, options)


def play(s, *actions):
    """Apply legal actions, each by whoever must move."""
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


def night_one(victim=3, seen=0):
    return play(start(), kill(victim), kill(victim), inspect(seen))


def to_vote(s):
    while s.phase == "discussion":
        s = play(s, SAY)
    return s


# --- rules ----------------------------------------------------------------------------


def test_registered_and_starts_with_night_one():
    assert get_game("werewolf").name == "werewolf"
    s = start()
    assert (s.phase, s.day, s.queue) == ("night", 1, (0, 1, 2))
    assert G.current_player(s) == 0


def test_only_seven_seats_and_only_a_permutation_of_the_roles():
    with pytest.raises(ValueError):
        G.initial_state(6, 1)
    with pytest.raises(ValueError):
        G.initial_state(SEATS, 1, {"roles": [WOLF] * SEATS})


def test_without_options_roles_are_a_seeded_shuffle():
    assert sorted(G.initial_state(SEATS, 1).roles) == sorted(ROLES)
    assert G.initial_state(SEATS, 1).roles == G.initial_state(SEATS, 1).roles
    assert len({G.initial_state(SEATS, seed).roles for seed in range(30)}) > 1


def test_other_games_ignore_options():
    assert Connect4().initial_state(2, 1, {"roles": []}) == Connect4().initial_state(2, 1)
    assert LiarsDice().initial_state(2, 1, {"roles": []}) == LiarsDice().initial_state(2, 1)


def test_the_agreed_night_kill_happens_and_day_one_opens():
    s = night_one(victim=3, seen=0)
    assert (s.phase, s.day, s.discussion_round) == ("discussion", 1, 1)
    assert 3 not in s.alive and s.deaths[0].role == VILLAGER and s.deaths[0].cause == "night"


def test_tied_night_kill_is_drawn_from_the_seed_and_always_resolves():
    victims = set()
    for seed in range(40):
        s = play(start(seed=seed), kill(3), kill(4), inspect(0))
        assert (3 in s.alive) != (4 in s.alive)  # exactly one died
        victims.add(s.deaths[0].seat)
        again = play(start(seed=seed), kill(3), kill(4), inspect(0))
        assert again == s
    assert victims == {3, 4}


def test_kill_target_gone_by_resolution_still_kills_someone():
    # Both wolves pick the seer, who then forfeits before the night resolves.
    s = play(start(), kill(2), kill(2))
    s = G.forfeit(s, 2)
    assert [d.cause for d in s.deaths] == ["forfeit", "night"]
    assert s.deaths[1].role == VILLAGER and s.phase == "discussion"


def test_speaking_order_rotates_each_round_and_skips_the_dead():
    s = night_one(victim=3)
    first = []
    while s.phase == "discussion":
        first.append(s.queue)
        s = play(s, *[SAY] * len(s.queue))
    assert first == [(0, 1, 2, 4, 5, 6), (1, 2, 4, 5, 6, 0)]
    s = play(s, vote(1), *[vote(0)] * 5)  # seat 0 out
    s = play(s, kill(4), inspect(1))
    orders = []
    while s.phase == "discussion":
        orders.append(s.queue)
        s = play(s, *[SAY] * len(s.queue))
    assert orders[0][0] == 2 and orders[1][0] == 5  # the next living seats after 1, then 2


def test_day_votes_stay_hidden_until_everyone_has_voted():
    s = to_vote(night_one(victim=3))
    s = play(s, vote(1), vote(0))
    for seat in s.alive:
        view = G.player_view(s, seat)
        assert view["days"][0]["votes"] == []
        assert view["your_vote"] == {0: 1, 1: 0}.get(seat)
    s = play(s, *[vote(0)] * 4)
    assert len(G.player_view(s, 1)["days"][0]["votes"]) == 6


def test_most_voted_is_eliminated_and_a_tied_vote_eliminates_nobody():
    s = to_vote(night_one(victim=3))
    out = play(s, vote(1), vote(0), vote(0), vote(1), vote(1), vote(0))  # 3-3
    assert len(out.alive) == 6 and out.phase == "night" and out.day == 2
    out = play(s, vote(1), vote(0), vote(0), vote(0), vote(0), vote(0))
    assert 0 not in out.alive and out.deaths[-1].cause == "vote" and out.deaths[-1].role == WOLF


def test_village_wins_when_no_wolf_is_left():
    s = to_vote(night_one(victim=3))
    s = play(s, vote(1), *[vote(0)] * 5)  # wolf 0 out
    s = play(s, kill(4), inspect(1))
    s = to_vote(s)
    s = play(s, vote(2), *[vote(1)] * 3)  # seat 1 votes elsewhere; 2, 5 and 6 vote it out
    assert s.winner == "village" and G.is_terminal(s) and s.phase == "over"
    assert G.scores(s) == [0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    assert G.placements(s) == [6, 6, 1, 1, 1, 1, 1]  # all winners rank 1, dead or alive


def test_wolves_win_at_parity():
    s = to_vote(night_one(victim=3))
    s = play(s, vote(6), vote(6), vote(6), vote(6), vote(6), vote(0))  # villager out
    assert s.alive == (0, 1, 2, 4, 5)
    s = play(s, kill(4), kill(4), inspect(0))
    assert s.alive == (0, 1, 2, 5) and s.winner == "wolves"
    assert G.scores(s) == [1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]


def test_dead_players_neither_speak_nor_vote():
    s = to_vote(night_one(victim=3))
    assert 3 not in s.queue
    assert all(a["target"] != 3 for a in G.legal_actions_hint(s, 0))
    assert G.validate_action(s, 0, vote(3)) is not None


def test_a_forfeit_eliminates_with_role_revealed_and_the_game_goes_on():
    s = G.forfeit(start(), 0)
    assert s.deaths[0].cause == "forfeit" and s.deaths[0].role == WOLF
    assert not G.is_terminal(s) and s.queue == (1, 2)
    assert G.forfeit(s, 1).winner == "village"


async def test_a_forfeit_on_the_winning_team_ranks_last_but_keeps_the_score():
    # Seat 6 quits on day 1; the village then votes out both wolves.
    class Plan:
        def __init__(self, seat):
            self.player_id = f"b{seat}"

        async def act(self, ctx):
            v = ctx.view
            if v["phase"] == "night":
                if v["role"] == WOLF:
                    return Decision(action=kill(3 if v["day"] == 1 else 4))
                return Decision(action=inspect(v["day"] - 1))
            if v["phase"] == "discussion":
                return Decision(action=None if v["you"] == 6 else SAY)
            wolf = v["day"] - 1
            return Decision(action=vote(1 if v["you"] == 0 else 2 if v["you"] == 1 else wolf))

    runner = MatchRunner(G, [Plan(s) for s in range(SEATS)], InMemoryTurnStore())
    out = await runner.run(spec(1, OPTIONS))
    assert out.forfeits == [6] and out.scores == [0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    assert out.placements[6] == SEATS  # its team won, but a forfeit ranks last
    assert out.placements[:6] == [5, 5, 1, 1, 1, 1]


# --- actions --------------------------------------------------------------------------


def test_wrong_phase_or_malformed_actions_are_rejected():
    s = start()
    assert "kill" in G.validate_action(s, 0, SAY)
    assert "target" in G.validate_action(s, 0, kill(1))  # can't target a fellow wolf
    assert "target" in G.validate_action(s, 0, {"action": "kill"})
    assert "target" in G.validate_action(s, 0, {"action": "kill", "target": True})
    assert "turn" in G.validate_action(s, 1, kill(3))
    assert G.validate_action(s, 0, "nope") == "action must be an object"
    d = night_one()
    assert "say" in G.validate_action(d, 0, vote(1))
    assert "non-empty" in G.validate_action(d, 0, {"action": "say", "message": "  "})
    assert "non-empty" in G.validate_action(d, 0, {"action": "say", "message": 5})


def test_action_specs_follow_the_phase_and_forbid_extra_fields():
    s = start()
    schema = G.action_spec(s, 0).input_schema
    assert schema["properties"]["target"]["enum"] == [2, 3, 4, 5, 6]
    assert schema["additionalProperties"] is False
    assert G.action_spec(s, 2).input_schema["properties"]["action"]["enum"] == ["inspect"]
    assert "message" in G.action_spec(night_one(), 0).input_schema["properties"]


async def test_long_messages_are_truncated_flagged_and_not_invalid():
    long = "x" * (MAX_MESSAGE_CHARS + 50)

    class Rambler(WerewolfBot):
        async def act(self, ctx):
            if ctx.view["phase"] == "discussion":
                return Decision(action={"action": "say", "message": long}, raw=long)
            return await super().act(ctx)

    players = bots()
    players[5] = Rambler("b5", 3)
    store = InMemoryTurnStore()
    out = await MatchRunner(G, players, store).run(spec(2, OPTIONS))

    said = [t for t in out.turns if t.seat == 5 and t.action and t.action["action"] == "say"]
    assert said and all(t.truncated and t.attempts == 1 and not t.forfeited for t in said)
    assert all(len(t.action["message"]) == MAX_MESSAGE_CHARS for t in said)
    assert said[0].raw == long  # the model's full text stays in raw
    assert not any(t.truncated for t in out.turns if t.seat != 5)

    *_, final = iter_states(G, spec(2, OPTIONS), out.turns)  # replays cleanly
    assert all(len(m.text) <= MAX_MESSAGE_CHARS for m in final.messages)


async def test_exactly_the_limit_is_not_truncated():
    message = "y" * MAX_MESSAGE_CHARS
    action = {"action": "say", "message": message}
    assert G.normalize_action(night_one(), 0, action) is action


async def test_invalid_attempts_get_feedback_then_forfeit():
    p = ScriptedPlayer([{"action": "say", "message": "wrong phase"}, kill(1), None])
    players = [p, *bots()[1:]]
    out = await MatchRunner(G, players, InMemoryTurnStore()).run(spec(1, OPTIONS))
    first = out.turns[0]
    assert first.forfeited and first.attempts == 3 and out.forfeits == [0]
    assert p.seen[1].previous_error == "action must be 'kill' now"


# --- hidden information ---------------------------------------------------------------


def visible_seats(state, seat):
    known = {seat} | {d.seat for d in state.deaths}
    if state.roles[seat] == WOLF:
        known |= {s for s, r in enumerate(state.roles) if r == WOLF}
    if state.roles[seat] == SEER:
        known |= {i.target for i in state.inspections}
    return known


def scramble(state, seat, rng):
    """The same state with every role this seat can't know shuffled among the unknowns."""
    known = visible_seats(state, seat)
    unknown = [s for s in range(SEATS) if s not in known]
    shuffled = rng.sample([state.roles[s] for s in unknown], len(unknown))
    roles = list(state.roles)
    for s, role in zip(unknown, shuffled, strict=True):
        roles[s] = role
    # Kill targets a wolf could legally pick don't change: wolves' seats are all known.
    return replace(state, roles=tuple(roles))


async def bot_game(seed, options=None):
    out = await MatchRunner(G, bots(seed), InMemoryTurnStore()).run(spec(seed, options))
    return list(iter_states(G, spec(seed, options), out.turns))


@pytest.mark.parametrize("seed", range(12))
async def test_player_view_never_depends_on_roles_the_seat_cannot_know(seed):
    states = await bot_game(seed)
    rng = random.Random(seed)
    for state in states:
        for seat in range(SEATS):
            view = G.player_view(state, seat)
            for _ in range(3):
                assert G.player_view(scramble(state, seat, rng), seat) == view


@pytest.mark.parametrize("seed", range(6))
async def test_views_name_only_the_roles_a_seat_may_see(seed):
    states = await bot_game(seed)
    for state in states:
        for seat in range(SEATS):
            view = G.player_view(state, seat)
            role = state.roles[seat]
            wolves = {s for s, r in enumerate(state.roles) if r == WOLF}
            assert view["role"] == role
            assert set(view["fellow_wolves"]) == (wolves - {seat} if role == WOLF else set())
            if role != WOLF:
                assert view["wolf_votes"] == []
            if role != SEER:
                assert view["seer_results"] == []
            revealed = {d["seat"] for day in view["days"] for d in day["deaths"]}
            assert revealed == {d.seat for d in state.deaths}
            assert "to_move" not in view  # night order would expose wolves and the seer


def test_wolves_see_each_others_night_votes_as_they_are_cast():
    s = play(start(), kill(5))
    assert G.player_view(s, 1)["wolf_votes"] == [{"day": 1, "seat": 0, "target": 5}]
    assert "player 0 -> 5" in G.render_view(G.player_view(s, 1))
    assert G.player_view(s, 2)["wolf_votes"] == []


def test_the_seer_alone_gets_its_results():
    s = night_one(seen=0)
    assert G.player_view(s, 2)["seer_results"] == [{"night": 1, "seat": 0, "wolf": True}]
    assert "you inspected player 0: a wolf" in G.render_view(G.player_view(s, 2))
    other = night_one(seen=5)
    assert G.player_view(other, 2)["seer_results"][0]["wolf"] is False


def test_views_are_json_and_rendered_text_keeps_roles_private():
    s = to_vote(night_one())
    text = G.render_view(G.player_view(s, 4))
    json.dumps(G.player_view(s, 4))
    assert "Your role: villager" in text and "fellow wolves" not in text
    json.dumps(G.spectator_view(s))
    assert G.spectator_view(s)["roles"] == ROLES


# --- context limit --------------------------------------------------------------------


def long_day_state(limit):
    s = start({"roles": ROLES, "context_token_limit": limit})
    s = play(s, kill(3), kill(3), inspect(0))
    words = {"action": "say", "message": "z" * MAX_MESSAGE_CHARS}
    while s.phase == "discussion":
        s = play(s, words)
    s = play(s, vote(1), vote(0), vote(0), vote(1), vote(1), vote(0))  # tied: all live on
    s = play(s, kill(4), kill(4), inspect(1))
    while s.phase == "discussion":
        s = play(s, words)
    return s


def test_full_text_is_kept_by_default():
    view = G.player_view(long_day_state(DEFAULT_CONTEXT_TOKEN_LIMIT), 0)
    assert not any(d["reduced"] for d in view["days"])
    assert all(d["messages"] for d in view["days"])


def test_over_the_limit_the_oldest_day_keeps_only_votes_and_deaths():
    s = long_day_state(1000)
    full = G.player_view(long_day_state(DEFAULT_CONTEXT_TOKEN_LIMIT), 0)
    old, current = G.player_view(s, 0)["days"]
    assert old["reduced"] and old["messages"] == []
    assert old["votes"] == full["days"][0]["votes"] and old["votes"]
    assert old["deaths"] == full["days"][0]["deaths"] and old["deaths"][0]["role"] == VILLAGER
    assert not current["reduced"] and len(current["messages"]) == 10
    assert "discussion omitted" in G.render_view(G.player_view(s, 0))


def test_reduction_only_touches_views():
    assert long_day_state(1000).messages == long_day_state(DEFAULT_CONTEXT_TOKEN_LIMIT).messages


async def test_a_seven_seat_game_of_maximum_length_messages_never_hits_the_default():
    class Rambler(WerewolfBot):
        async def act(self, ctx):
            if ctx.view["phase"] == "discussion":
                return Decision(action={"action": "say", "message": "w" * MAX_MESSAGE_CHARS})
            return await super().act(ctx)

    longest = 0
    for seed in range(40):
        players = [Rambler(f"b{i}", seed * 10 + i) for i in range(SEATS)]
        out = await MatchRunner(G, players, InMemoryTurnStore()).run(spec(seed))
        for state in iter_states(G, spec(seed), out.turns):
            for seat in state.alive:
                view = G.player_view(state, seat)
                assert not any(d["reduced"] for d in view["days"])
                longest = max(longest, len(RULES + G.render_view(view)))
    assert longest // 4 < DEFAULT_CONTEXT_TOKEN_LIMIT / 2  # plenty of headroom


# --- whole games ----------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(150))
async def test_every_bot_game_terminates(seed):
    runner = MatchRunner(G, bots(seed), InMemoryTurnStore(), max_turns=300)
    out = await runner.run(spec(seed))
    assert out.forfeits == [] and set(out.scores) == {0.0, 1.0}
    winners = sum(out.scores)
    assert winners in (2.0, 5.0)  # the wolves, or everyone else
    assert out.placements.count(1) == winners


async def test_both_teams_win_some_bot_games():
    wins = Counter()
    for seed in range(60):
        out = await MatchRunner(G, bots(seed), InMemoryTurnStore()).run(spec(seed))
        wins[sum(out.scores)] += 1
    assert wins[2.0] and wins[5.0]


@pytest.mark.parametrize("options", [None, OPTIONS])
async def test_seeded_replay_is_exact(options):
    match = spec(5, options)
    out = await MatchRunner(G, bots(5), InMemoryTurnStore()).run(match)
    first = list(iter_states(G, match, out.turns))
    assert list(iter_states(G, match, out.turns)) == first
    assert G.scores(first[-1]) == out.scores
    assert (await MatchRunner(G, bots(5), InMemoryTurnStore()).run(match)).scores == out.scores


async def test_a_match_resumes_from_its_log_on_a_new_runner():
    match = spec(11, OPTIONS)
    ref = await MatchRunner(G, bots(11), InMemoryTurnStore()).run(match)
    store = InMemoryTurnStore()
    store.turns[match.match_id] = list(ref.turns[:17])
    out = await MatchRunner(G, bots(11), store).run(match)
    assert out.turns == ref.turns and out.scores == ref.scores


def test_apply_does_not_mutate():
    s = start()
    before = repr(s)
    G.apply(s, 0, kill(3))
    assert repr(s) == before


def test_bot_factory_and_prompt():
    assert isinstance(build_bot("werewolf", "w", 1), WerewolfBot)
    with pytest.raises(ValueError):
        build_bot("chess", "w", 1)
    assert load_prompt("werewolf/v1").key == "werewolf/v1"


# --- role claims ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "speaker", "expected"),
    [
        ("I am a villager.", 4, [(4, VILLAGER, "true")]),
        ("I'm the seer", 2, [(2, SEER, "true")]),
        ("i am the seer", 4, [(4, SEER, "false")]),
        ("I am a villager, trust me", 0, [(0, VILLAGER, "false")]),  # a wolf's bluff
        ("Fine, I am a wolf.", 0, [(0, WOLF, "leak")]),
        ("I'm a werewolf", 1, [(1, WOLF, "leak")]),
        ("Player 1 is a wolf", 0, [(1, WOLF, "leak")]),  # naming its partner
        ("seat 1 is the wolf", 0, [(1, WOLF, "leak")]),
        ("Player 5 is a wolf", 0, [(5, WOLF, "false")]),  # a wolf framing a villager
        ("Player 0 is a wolf.", 2, [(0, WOLF, "true")]),  # a seer's true accusation
        ("Player 3 is a wolf", 2, [(3, WOLF, "false")]),
        (
            "I am the seer and player 1 is a wolf",
            2,
            [(2, SEER, "true"), (1, WOLF, "true")],
        ),
    ],
)
def test_role_claims_are_classified(message, speaker, expected):
    got = classify_claims(message, speaker, ROLES)
    assert [(c.seat, c.role, c.kind) for c in got] == expected


@pytest.mark.parametrize(
    "message",
    [
        "I can see who is guilty",  # paraphrase
        "I am not a wolf",
        "Player 9 is a wolf",  # no such seat
        "the wolf is lying",
        "I suspect player 3.",
    ],
)
def test_anything_but_a_literal_claim_is_ignored(message):
    assert classify_claims(message, 4, ROLES) == []
