"""Werewolf: 7 seats, 2 wolves, 1 seer, 4 villagers. Hidden roles, free-text discussion.

Rules:
- Roles come from options["roles"] (tournaments balance them per player), otherwise a
  shuffle seeded by Random(f"{seed}:roles"). Wolves know each other from the start.
- The game opens with night 1. Living wolves each vote to kill a living non-wolf, seeing
  each other's votes; the living seer then inspects one seat and privately learns
  whether it is a wolf. The most-voted seat dies; a tie is drawn with
  Random(f"{seed}:night:{n}"), so the night kill always resolves and the game must end.
- Day n: two discussion rounds, then a vote. The first speaker moves to the next living
  seat every round. Votes stay hidden until every living player has voted; the most-voted
  seat is eliminated and a tied vote eliminates nobody. Every death reveals the role.
- Wolves win at parity (wolves >= everyone else alive). The village wins when no wolf is
  alive. A win applies to the whole team, dead members included.
- Forfeit: the seat skips this action and stays alive. A night action is lost, a vote is
  an abstention and a discussion turn is silence. If no wolf votes, the kill is still drawn
  from the seed, because the night kill is what guarantees the game ends. Placements
  ignore forfeits; they're counted by store.metrics instead.

Messages over MAX_MESSAGE_CHARS are valid: normalize_action truncates them and the
runner flags the turn. The log keeps full text unless a seat's prompt would pass
context_token_limit (estimated at 4 characters a token); then player_view reduces the
oldest finished days to their votes and deaths. Reduction only changes views, never state.

WerewolfDayFirst (`werewolf_dayfirst`) is the same game opening on day 1 with all seven alive:
there is no night 1, so the first kill and the seer's first inspection come on night 2.
"""

from __future__ import annotations

import random
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from arena.games.base import Game
from arena.types import JSON, Action, ActionSpec, is_int

WOLF = "wolf"
SEER = "seer"
VILLAGER = "villager"
ROLES = (WOLF, WOLF, SEER, VILLAGER, VILLAGER, VILLAGER, VILLAGER)
SEATS = len(ROLES)
WOLVES_WIN, VILLAGE_WINS = "wolves", "village"

DISCUSSION_ROUNDS = 2
MAX_MESSAGE_CHARS = 300
CHARS_PER_TOKEN = 4
# Far above a 7-seat game's worst case, so only explicit options or longer games reduce.
DEFAULT_CONTEXT_TOKEN_LIMIT = 20_000

RULES = f"""\
Werewolf. {SEATS} players: 2 wolves, 1 seer and 4 villagers. Your role is secret. Wolves know
each other; nobody else knows anyone's role. The wolves and the village are teams: every
member wins or loses together, dead or alive.

The game alternates night and day, starting with night 1.
- Night: each living wolf votes to kill a living non-wolf, seeing the other wolf's votes.
  The most-voted player dies; ties are drawn at random. The living seer then inspects one
  player and privately learns whether that player is a wolf.
- Day: everyone alive speaks in {DISCUSSION_ROUNDS} rounds of discussion, in turn, then
  votes to eliminate a player. Messages over {MAX_MESSAGE_CHARS} characters are cut off.
  Votes stay hidden until everyone has voted. The most-voted player is eliminated; on a
  tie nobody is. Dead players neither speak nor vote.

Every dead player's role is revealed. The village wins when no wolf is alive. The wolves
win when living wolves are at least as many as all other living players. Lying is allowed."""
DAY_FIRST_RULES = RULES.replace("starting with night 1", "starting with day 1")

Phase = Literal["night", "discussion", "vote", "over"]


@dataclass(frozen=True)
class Vote:
    day: int
    seat: int
    target: int


@dataclass(frozen=True)
class Inspection:
    night: int
    target: int
    is_wolf: bool


@dataclass(frozen=True)
class Message:
    day: int
    round: int
    seat: int
    text: str


@dataclass(frozen=True)
class Death:
    day: int
    seat: int
    role: str
    cause: Literal["night", "vote"]


@dataclass(frozen=True)
class WWState:
    seed: int
    roles: tuple[str, ...]
    alive: tuple[int, ...]  # sorted
    queue: tuple[int, ...]  # seats still to act in this phase, next first
    context_token_limit: int = DEFAULT_CONTEXT_TOKEN_LIMIT
    phase: Phase = "night"
    day: int = 1  # night n comes before day n
    discussion_round: int = 0
    first_speaker: int = -1  # who opened the latest discussion round
    night_votes: tuple[Vote, ...] = ()
    day_votes: tuple[Vote, ...] = ()
    inspections: tuple[Inspection, ...] = ()
    messages: tuple[Message, ...] = ()
    deaths: tuple[Death, ...] = ()
    forfeited: tuple[int, ...] = ()
    winner: str | None = None


def _wolves(state: WWState) -> list[int]:
    return [s for s, role in enumerate(state.roles) if role == WOLF]


def _night_queue(state: WWState) -> tuple[int, ...]:
    wolves = [s for s in state.alive if state.roles[s] == WOLF]
    return (*wolves, *(s for s in state.alive if state.roles[s] == SEER))


def _discussion_order(state: WWState) -> tuple[int, tuple[int, ...]]:
    """(the next first speaker, the speaking order from them)."""
    start = next((s for s in state.alive if s > state.first_speaker), state.alive[0])
    i = state.alive.index(start)
    return start, state.alive[i:] + state.alive[:i]


def winner_of(alive: tuple[int, ...], roles: tuple[str, ...]) -> str | None:
    wolves = sum(roles[s] == WOLF for s in alive)
    if wolves == 0:
        return VILLAGE_WINS
    return WOLVES_WIN if wolves >= len(alive) - wolves else None


def _kill(state: WWState, seat: int, cause: Literal["night", "vote"]) -> WWState:
    alive = tuple(s for s in state.alive if s != seat)
    death = Death(state.day, seat, state.roles[seat], cause)
    state = replace(state, alive=alive, deaths=state.deaths + (death,))
    winner = winner_of(alive, state.roles)
    return replace(state, winner=winner, phase="over", queue=()) if winner else state


def _top(votes: list[int]) -> list[int]:
    """The most-voted seats, sorted."""
    counts = Counter(votes)
    most = max(counts.values())
    return sorted(s for s, n in counts.items() if n == most)


def _start_discussion(state: WWState, round_no: int) -> WWState:
    start, order = _discussion_order(state)
    return replace(
        state, phase="discussion", discussion_round=round_no, first_speaker=start, queue=order
    )


def _resolve_night(state: WWState) -> WWState:
    votes = [v.target for v in state.night_votes if v.day == state.day]
    tied = _top(votes) if votes else [s for s in state.alive if state.roles[s] != WOLF]
    victim = tied[0]
    if len(tied) > 1:
        victim = random.Random(f"{state.seed}:night:{state.day}").choice(tied)
    state = _kill(state, victim, "night")
    return state if state.winner else _start_discussion(state, 1)


def _resolve_vote(state: WWState) -> WWState:
    votes = [v.target for v in state.day_votes if v.day == state.day]
    tied = _top(votes) if votes else []
    if len(tied) == 1:
        state = _kill(state, tied[0], "vote")
        if state.winner:
            return state
    next_night = replace(state, phase="night", day=state.day + 1, discussion_round=0)
    return replace(next_night, queue=_night_queue(next_night))


def _advance(state: WWState) -> WWState:
    """Move on once everyone in the phase has acted."""
    if state.queue or state.winner:
        return state
    if state.phase == "night":
        return _resolve_night(state)
    if state.phase == "discussion":
        if state.discussion_round < DISCUSSION_ROUNDS:
            return _start_discussion(state, state.discussion_round + 1)
        return replace(state, phase="vote", queue=state.alive)
    return _resolve_vote(state)


def _estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


class Werewolf(Game):
    name = "werewolf"
    min_players = SEATS
    max_players = SEATS

    def rules_text(self) -> str:
        return RULES

    def role_pool(self, num_players: int) -> list[str]:
        return list(ROLES)

    def initial_state(self, num_players: int, seed: int, options: JSON | None = None) -> WWState:
        if num_players != SEATS:
            raise ValueError(f"werewolf takes {SEATS} players")
        options = options or {}
        roles = list(ROLES)
        if "roles" in options:
            roles = list(options["roles"])
            if sorted(roles) != sorted(ROLES):
                raise ValueError(f"werewolf roles must be a permutation of {ROLES}")
        else:
            random.Random(f"{seed}:roles").shuffle(roles)
        state = WWState(
            seed=seed,
            roles=tuple(roles),
            alive=tuple(range(SEATS)),
            queue=(),
            context_token_limit=options.get("context_token_limit", DEFAULT_CONTEXT_TOKEN_LIMIT),
        )
        return replace(state, queue=_night_queue(state))

    def current_player(self, state: WWState) -> int:
        return state.queue[0]

    # --- views ------------------------------------------------------------------------

    def _legal_targets(self, state: WWState, seat: int) -> list[int]:
        if seat not in state.alive or state.phase in ("discussion", "over"):
            return []
        if state.phase == "vote":
            return [s for s in state.alive if s != seat]
        if state.roles[seat] == WOLF:
            return [s for s in state.alive if state.roles[s] != WOLF]
        if state.roles[seat] == SEER:
            return [s for s in state.alive if s != seat]
        return []

    def _day_log(self, state: WWState, day: int) -> JSON:
        # This day's votes stay hidden until the vote resolves, which ends the phase.
        votes_open = day < state.day or state.phase != "vote"
        return {
            "day": day,
            "deaths": [_death_json(d) for d in state.deaths if d.day == day],
            "messages": [
                {"round": m.round, "seat": m.seat, "text": m.text}
                for m in state.messages
                if m.day == day
            ],
            "votes": [_vote_json(v) for v in state.day_votes if v.day == day and votes_open],
            "reduced": False,
        }

    def player_view(self, state: WWState, seat: int) -> JSON:
        # No to_move here: at night, who acts next is exactly who the wolves and seer are.
        role = state.roles[seat]
        view = {
            "you": seat,
            "role": role,
            "phase": state.phase,
            "day": state.day,
            "discussion_round": state.discussion_round,
            "your_turn": bool(state.queue) and state.queue[0] == seat,
            "alive": list(state.alive),
            "fellow_wolves": [s for s in _wolves(state) if s != seat] if role == WOLF else [],
            "wolf_votes": [_vote_json(v) for v in state.night_votes] if role == WOLF else [],
            "seer_results": (
                [{"night": i.night, "seat": i.target, "wolf": i.is_wolf} for i in state.inspections]
                if role == SEER
                else []
            ),
            "your_vote": next(
                (v.target for v in state.day_votes if v.day == state.day and v.seat == seat), None
            ),
            "legal_targets": self._legal_targets(state, seat),
            "days": [self._day_log(state, d) for d in range(1, state.day + 1)],
            "winner": state.winner,
        }
        return self._within_token_limit(view, state.context_token_limit)

    def _within_token_limit(self, view: JSON, limit: int) -> JSON:
        """Reduce the oldest finished days, one at a time, until the prompt fits."""
        days = view["days"]
        for i in range(len(days) - 1):  # the current day always stays in full
            if _estimate_tokens(self.rules_text() + self.render_view(view)) <= limit:
                break
            days[i] = days[i] | {"messages": [], "reduced": True}
        return view

    def spectator_view(self, state: WWState) -> JSON:
        return {
            "phase": state.phase,
            "day": state.day,
            "discussion_round": state.discussion_round,
            "to_move": state.queue[0] if state.queue else None,
            "roles": list(state.roles),
            "alive": list(state.alive),
            "wolf_votes": [_vote_json(v) for v in state.night_votes if v.day == state.day],
            "inspections": [
                {"night": i.night, "seat": i.target, "wolf": i.is_wolf} for i in state.inspections
            ],
            "messages": [
                {"round": m.round, "seat": m.seat, "text": m.text}
                for m in state.messages
                if m.day == state.day
            ],
            "votes": [_vote_json(v) for v in state.day_votes if v.day == state.day],
            "deaths": [_death_json(d) for d in state.deaths],
            "forfeited": list(state.forfeited),
            "winner": state.winner,
        }

    def render_view(self, view: JSON) -> str:
        lines = [f"You are player {view['you']}. Your role: {view['role']}."]
        if view["fellow_wolves"]:
            lines.append(f"Your fellow wolves: {_players(view['fellow_wolves'])}.")
        lines.append(f"Alive: {_players(view['alive'])}.")
        for r in view["seer_results"]:
            verdict = "a wolf" if r["wolf"] else "not a wolf"
            lines.append(f"Night {r['night']}: you inspected player {r['seat']}: {verdict}.")
        for d in view["days"]:
            lines.append(f"\n--- Day {d['day']} ---")
            for death in d["deaths"]:
                how = {"night": "was killed in the night", "vote": "was voted out"}[death["cause"]]
                lines.append(f"Player {death['seat']} ({death['role']}) {how}.")
            if d["reduced"]:
                lines.append("(discussion omitted to save space)")
            for m in d["messages"]:
                lines.append(f"Player {m['seat']} (round {m['round']}): {m['text']}")
            if d["votes"]:
                lines.append(
                    "Votes: "
                    + ", ".join(f"player {v['seat']} -> {v['target']}" for v in d["votes"])
                )
        if view["wolf_votes"]:
            lines.append(
                "\nWolf votes: "
                + ", ".join(
                    f"night {v['day']}: player {v['seat']} -> {v['target']}"
                    for v in view["wolf_votes"]
                )
            )
        lines.append("")
        lines.append(_instruction(view))
        return "\n".join(lines)

    # --- actions ----------------------------------------------------------------------

    def _expected_action(self, state: WWState, seat: int) -> str:
        if state.phase == "discussion":
            return "say"
        if state.phase == "vote":
            return "vote"
        return "kill" if state.roles[seat] == WOLF else "inspect"

    def action_spec(self, state: WWState, seat: int) -> ActionSpec:
        kind = self._expected_action(state, seat)
        props: JSON = {"action": {"type": "string", "enum": [kind]}}
        if kind == "say":
            props["message"] = {
                "type": "string",
                "maxLength": MAX_MESSAGE_CHARS,
                "description": f"What you say to the table (max {MAX_MESSAGE_CHARS} characters).",
            }
            required = ["action", "message"]
            description = "Speak to the table."
        else:
            props["target"] = {
                "type": "integer",
                "enum": self._legal_targets(state, seat),
                "description": "The player number you choose.",
            }
            required = ["action", "target"]
            description = {
                "kill": "Vote for the player to kill tonight.",
                "inspect": "Choose a player to inspect tonight.",
                "vote": "Vote for the player to eliminate.",
            }[kind]
        return ActionSpec(
            name="werewolf_move",
            description=description,
            input_schema={
                "type": "object",
                "properties": props,
                "required": required,
                "additionalProperties": False,
            },
        )

    def legal_actions_hint(self, state: WWState, seat: int) -> list[Action] | None:
        kind = self._expected_action(state, seat)
        if kind == "say":
            return None  # free text can't be enumerated
        return [{"action": kind, "target": t} for t in self._legal_targets(state, seat)]

    def validate_action(self, state: WWState, seat: int, action: Action) -> str | None:
        if self.is_terminal(state):
            return "the game is over"
        if seat != self.current_player(state):
            return "it is not your turn"
        if not isinstance(action, dict):
            return "action must be an object"
        kind = self._expected_action(state, seat)
        if action.get("action") != kind:
            return f"action must be '{kind}' now"
        if kind == "say":
            message = action.get("message")
            if not isinstance(message, str) or not message.strip():
                return "'message' must be non-empty text"
            return None
        target = action.get("target")
        legal = self._legal_targets(state, seat)
        if not is_int(target) or target not in legal:
            return f"'target' must be one of {legal}"
        return None

    def fill_action(self, state: WWState, seat: int, action: Action) -> Action:
        # Each turn allows exactly one kind of action, so a missing `action` is unambiguous.
        # One that's present and wrong is left for validate_action to reject.
        if isinstance(action, dict) and "action" not in action:
            return action | {"action": self._expected_action(state, seat)}
        return action

    def normalize_action(self, state: WWState, seat: int, action: Action) -> Action:
        message = action.get("message")
        if isinstance(message, str) and len(message) > MAX_MESSAGE_CHARS:
            return action | {"message": message[:MAX_MESSAGE_CHARS]}
        return action

    def apply(self, state: WWState, seat: int, action: Action) -> WWState:
        kind, rest = action["action"], state.queue[1:]
        if kind == "kill":
            vote = Vote(state.day, seat, action["target"])
            state = replace(state, night_votes=state.night_votes + (vote,))
        elif kind == "inspect":
            target = action["target"]
            found = Inspection(state.day, target, state.roles[target] == WOLF)
            state = replace(state, inspections=state.inspections + (found,))
        elif kind == "say":
            text = self.normalize_action(state, seat, action)["message"]
            said = Message(state.day, state.discussion_round, seat, text)
            state = replace(state, messages=state.messages + (said,))
        else:
            vote = Vote(state.day, seat, action["target"])
            state = replace(state, day_votes=state.day_votes + (vote,))
        return _advance(replace(state, queue=rest))

    def forfeit(self, state: WWState, seat: int) -> WWState:
        return _advance(replace(state, queue=state.queue[1:], forfeited=state.forfeited + (seat,)))

    def is_terminal(self, state: WWState) -> bool:
        return state.winner is not None

    def final_placements(self, state: WWState, forfeits: Sequence[int]) -> list[int]:
        return self.placements(state)

    def scores(self, state: WWState) -> list[float]:
        if state.winner is None:
            raise ValueError("scores() called on a non-terminal state")
        wolves_won = state.winner == WOLVES_WIN
        return [1.0 if (r == WOLF) == wolves_won else 0.0 for r in state.roles]


class WerewolfDayFirst(Werewolf):
    name = "werewolf_dayfirst"

    def rules_text(self) -> str:
        return DAY_FIRST_RULES

    def initial_state(self, num_players: int, seed: int, options: JSON | None = None) -> WWState:
        return _start_discussion(super().initial_state(num_players, seed, options), 1)


def _vote_json(v: Vote) -> JSON:
    return {"day": v.day, "seat": v.seat, "target": v.target}


def _death_json(d: Death) -> JSON:
    return {"day": d.day, "seat": d.seat, "role": d.role, "cause": d.cause}


def _players(seats: list[int]) -> str:
    return ", ".join(str(s) for s in seats)


def _instruction(view: JSON) -> str:
    if view["phase"] == "night":
        if view["role"] == WOLF:
            return f"Night {view['day']}: choose a player to kill from {view['legal_targets']}."
        return f"Night {view['day']}: choose a player to inspect from {view['legal_targets']}."
    if view["phase"] == "discussion":
        return (
            f"Day {view['day']}, discussion round {view['discussion_round']} of "
            f"{DISCUSSION_ROUNDS}: say something to the table."
        )
    if view["phase"] == "vote":
        return f"Day {view['day']}: vote to eliminate a player from {view['legal_targets']}."
    return f"The game is over: the {view['winner']} won."


# --- role claims ----------------------------------------------------------------------
# Literal phrases only, so paraphrases ("I can see who's guilty") are missed.
_ROLE = (
    r"(?:(?:just|only)\s+)?(?:(?:a|an|the)\s+)?(?:(?:simple|plain)\s+)?"
    r"(werewolf|wolf|seer|villager)(?![\w-])"  # not "I'm wolf-hunting"
)
_SELF_CLAIM = re.compile(
    rf"\b(?:(?:i\s+am|i['’]m)\s+{_ROLE}|as\s+{_ROLE}\s*,?\s*i\b)", re.IGNORECASE
)
_OTHER_CLAIM = re.compile(
    rf"\b(?:player|seat)\s+(\d+)\s+(?:is\s+|here\s*[,:\-–—]?\s*(?:as\s+)?){_ROLE}",
    re.IGNORECASE,
)

ClaimKind = Literal["true", "false", "leak"]


@dataclass(frozen=True)
class RoleClaim:
    seat: int  # who the claim is about
    role: str
    kind: ClaimKind


def classify_claims(message: str, speaker: int, roles: list[str]) -> list[RoleClaim]:
    """Explicit role claims in one message: true claims, false claims (bluffs), or leaks
    (a wolf stating its own role or naming its partner). Other seats' true claims about
    wolves are accusations that landed, and count as true. "Player 6 here, villager" is
    a claim about seat 6 whoever says it. The same claim twice in a message counts once."""
    found = [(speaker, m[1] or m[2]) for m in _SELF_CLAIM.finditer(message)]
    found += [(int(m[1]), m[2]) for m in _OTHER_CLAIM.finditer(message)]
    claims = []
    for seat, word in dict.fromkeys((seat, word.lower()) for seat, word in found):
        if seat >= len(roles):
            continue
        role = WOLF if word == "werewolf" else word
        if role == WOLF and roles[speaker] == WOLF and roles[seat] == WOLF:
            kind: ClaimKind = "leak"
        else:
            kind = "true" if roles[seat] == role else "false"
        claims.append(RoleClaim(seat, role, kind))
    return claims
