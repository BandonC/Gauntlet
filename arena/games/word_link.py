"""Word Link: two teams, one shared board of words.

Seats: 0 and 1 are team A's clue-giver and guesser, 2 and 3 team B's.

Rules:
- 16 seeded words: 6 for each team, 3 neutral, 1 trap. Clue-givers see which is which;
  guessers see only the words and what's been revealed.
- Team A goes first. A clue-giver gives one word and a number. The clue can't be, contain or
  sit inside a word on the board, which also rules out plurals of it.
- The guesser picks words one at a time, up to the number plus one, and may stop after the
  first. Their own word: guess again. Neutral or the other team's word: the turn ends, and
  the other team's word counts for them. The trap: their team loses at once.
- First team with all 6 of its words revealed wins, even if the other team revealed the
  last one. After 24 clues the game ends: most words revealed wins, level is a draw.
- Three invalid attempts skip the turn: a skipped clue passes to the other team, a skipped
  guess ends the turn. A forfeit removes nobody, so placements ignore it. The clue limit is
  what keeps a game finite when clues keep being skipped.

Ratings use `teams`, so both seats of a side share a Weng-Lin team.
"""

from __future__ import annotations

import random
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

from arena.games.base import Game, placements_from_scores
from arena.games.word_link_words import WORDS
from arena.types import JSON, Action, ActionSpec

BOARD_SIZE = 16
TEAM_WORDS = 6
NEUTRAL_WORDS = 3
MAX_CLUES = 24
MAX_NUMBER = TEAM_WORDS
A, B, NEUTRAL, TRAP = "a", "b", "neutral", "trap"
TEAM_NAMES = (A, B)
CLUE, GUESS = "clue", "guess"
_ONE_WORD = re.compile(r"^[a-z]+$")

RULES = f"""\
Word Link. Two teams of two, team A and team B, each with a clue-giver and a guesser, share a
board of {BOARD_SIZE} words: {TEAM_WORDS} belong to each team, {NEUTRAL_WORDS} are neutral and 1 is
the trap. Clue-givers can see which is which. Guessers see only the words and what has been
revealed.

Teams take turns. The clue-giver gives one word and a number: the word should point at their
team's words, and the number says how many. The clue must be a single word, and it can't be,
contain or sit inside any word on the board.

The guesser then picks one word at a time, up to the number plus one, and may stop after
the first guess.
- Your team's word: it is revealed and you can guess again.
- A neutral word or the other team's word: it is revealed and your turn ends. The other
  team's word counts for them.
- The trap: your team loses at once.

The first team with all {TEAM_WORDS} of its words revealed wins, even if the other team revealed the
last one. After {MAX_CLUES} clues the team with more words revealed wins, and level is a draw."""


@dataclass(frozen=True)
class Clue:
    team: int
    word: str
    number: int

    def to_json(self) -> JSON:
        return {"team": TEAM_NAMES[self.team], "word": self.word, "number": self.number}


@dataclass(frozen=True)
class Reveal:
    team: int  # the team that guessed
    word: str
    kind: str

    def to_json(self) -> JSON:
        return {"team": TEAM_NAMES[self.team], "word": self.word, "kind": self.kind}


@dataclass(frozen=True)
class WLState:
    board: tuple[str, ...]
    key: tuple[str, ...]
    team: int = 0  # whose turn
    phase: str = CLUE
    clue: Clue | None = None
    guesses_made: int = 0
    clues: tuple[Clue, ...] = ()
    reveals: tuple[Reveal, ...] = ()
    skipped: int = 0  # clue turns lost to forfeits
    winner: int | None = None  # team index
    over: bool = False
    forfeits: tuple[int, ...] = ()


def deal_board(seed: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
    rng = random.Random(f"{seed}:board")
    board = tuple(rng.sample(WORDS, BOARD_SIZE))
    key = [A] * TEAM_WORDS + [B] * TEAM_WORDS + [NEUTRAL] * NEUTRAL_WORDS + [TRAP]
    rng.shuffle(key)
    return board, tuple(key)


def _kind(state: WLState, word: str) -> str:
    return state.key[state.board.index(word)]


def _revealed(state: WLState) -> set[str]:
    return {r.word for r in state.reveals}


def _left(state: WLState, team: int) -> int:
    name = TEAM_NAMES[team]
    return sum(k == name for k in state.key) - sum(r.kind == name for r in state.reveals)


def clue_problem(word: object, board: Sequence[str]) -> str | None:
    """Why this clue breaks the rules, or None. `word` may be anything a model sends."""
    if not isinstance(word, str):
        return "a clue must be a word"
    clue = word.strip().lower()
    if not _ONE_WORD.fullmatch(clue):
        return "a clue must be one word, letters only"
    for on_board in board:
        if clue == on_board or clue in on_board or on_board in clue:
            return f"the clue {clue!r} is too close to {on_board!r}, a word on the board"
    return None


def _guess_limit(state: WLState) -> int:
    return state.clue.number + 1 if state.clue else 0


class WordLink(Game):
    name = "word_link"
    min_players = 4
    max_players = 4

    def rules_text(self) -> str:
        return RULES

    def teams(self, num_players: int) -> tuple[tuple[int, ...], ...]:
        return ((0, 1), (2, 3))

    def initial_state(self, num_players: int, seed: int, options: JSON | None = None) -> WLState:
        if num_players != 4:
            raise ValueError("word_link takes 4 players")
        board, key = deal_board(seed)
        return WLState(board=board, key=key)

    def current_player(self, state: WLState) -> int:
        return state.team * 2 + (state.phase == GUESS)

    # --- views ------------------------------------------------------------------------

    def _public(self, state: WLState) -> JSON:
        kinds = {r.word: r.kind for r in state.reveals}
        return {
            "board": [{"word": w, "revealed": kinds.get(w)} for w in state.board],
            "turn": TEAM_NAMES[state.team],
            "phase": state.phase,
            "clue": state.clue.to_json() if state.clue else None,
            "guesses_made": state.guesses_made,
            "guess_limit": _guess_limit(state),
            "clues": [c.to_json() for c in state.clues],
            "reveals": [r.to_json() for r in state.reveals],
            "left": {TEAM_NAMES[t]: _left(state, t) for t in (0, 1)},
            "clues_left": MAX_CLUES - len(state.clues) - state.skipped,
        }

    def player_view(self, state: WLState, seat: int) -> JSON:
        view = self._public(state) | {
            "you": seat,
            "team": TEAM_NAMES[seat // 2],
            "role": GUESS if seat % 2 else CLUE,
        }
        if seat % 2 == 0:  # only clue-givers see the key
            view["key"] = dict(zip(state.board, state.key, strict=True))
        return view

    def spectator_view(self, state: WLState) -> JSON:
        return self._public(state) | {
            "key": dict(zip(state.board, state.key, strict=True)),
            "over": state.over,
            "winner": TEAM_NAMES[state.winner] if state.winner is not None else None,
            "scores": self.scores(state) if state.over else None,
            "forfeits": list(state.forfeits),
        }

    def render_view(self, view: JSON) -> str:
        team = view["team"].upper()
        lines = [f"You are the {view['role']}-giver of team {team}."]
        if view["role"] == GUESS:
            lines = [f"You are the guesser of team {team}."]
        lines.append(
            f"Words left: team A {view['left'][A]}, team B {view['left'][B]}. "
            f"Clues left in the game: {view['clues_left']}."
        )
        hidden = [c["word"] for c in view["board"] if not c["revealed"]]
        if view["role"] == CLUE:
            key = view["key"]
            mine = view["team"]
            other = B if mine == A else A
            for label, kind in (
                ("Your team's words", mine),
                ("The other team's words", other),
                ("Neutral words", NEUTRAL),
                ("The trap", TRAP),
            ):
                lines.append(f"{label}: " + ", ".join(w for w in hidden if key[w] == kind))
        else:
            lines.append("Unrevealed words: " + ", ".join(hidden))
        if view["reveals"]:
            lines.append("Revealed so far:")
            lines += [
                f"- {r['word']}: {r['kind']} (guessed by team {r['team'].upper()})"
                for r in view["reveals"]
            ]
        if view["clues"]:
            lines.append("Clues so far:")
            lines += [
                f"- team {c['team'].upper()}: {c['word']} {c['number']}" for c in view["clues"]
            ]
        clue = view["clue"]
        if view["role"] == GUESS and clue:
            lines.append(
                f"Your clue: {clue['word']} {clue['number']}. This is guess "
                f"{view['guesses_made'] + 1} of at most {view['guess_limit']}."
            )
        return "\n".join(lines)

    # --- actions ----------------------------------------------------------------------

    def action_spec(self, state: WLState, seat: int) -> ActionSpec:
        if state.phase == CLUE:
            return ActionSpec(
                name="word_link_clue",
                description="Give your guesser one word and a number.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "word": {"type": "string", "description": "One word, letters only."},
                        "number": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": MAX_NUMBER,
                            "description": "How many of your team's words it points at.",
                        },
                    },
                    "required": ["word", "number"],
                    "additionalProperties": False,
                },
            )
        kinds = ["guess", "stop"] if state.guesses_made else ["guess"]
        words = [w for w in state.board if w not in _revealed(state)]
        return ActionSpec(
            name="word_link_guess",
            description="Guess one unrevealed word. Only the actions listed are legal now.",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": kinds},
                    "word": {"type": "string", "enum": words},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        )

    def legal_actions_hint(self, state: WLState, seat: int) -> list[Action] | None:
        return None  # a clue is free text, so the bots work from the view

    def fill_action(self, state: WLState, seat: int, action: Action) -> Action:
        if state.phase == GUESS and isinstance(action, dict):
            if "action" not in action and "word" in action:
                return action | {"action": "guess"}
        return action

    def validate_action(self, state: WLState, seat: int, action: Action) -> str | None:
        if self.is_terminal(state):
            return "the game is over"
        if seat != self.current_player(state):
            return "it is not your turn"
        if not isinstance(action, dict):
            return "action must be an object"
        if state.phase == CLUE:
            problem = clue_problem(action.get("word"), state.board)
            if problem:
                return problem
            number = action.get("number")
            if not isinstance(number, int) or isinstance(number, bool):
                return "a clue needs a whole number"
            if not 1 <= number <= MAX_NUMBER:
                return f"the number must be 1 to {MAX_NUMBER}"
            return None
        kind = action.get("action")
        if kind == "stop":
            return None if state.guesses_made else "you must make at least one guess"
        if kind != "guess":
            return "action must be 'guess' or 'stop'"
        word = action.get("word")
        if not isinstance(word, str):
            return "a guess needs a word from the board"
        word = word.strip().lower()
        if word not in state.board:
            return f"{word!r} is not on the board"
        if word in _revealed(state):
            return f"{word!r} has already been revealed"
        return None

    def apply(self, state: WLState, seat: int, action: Action) -> WLState:
        if state.phase == CLUE:
            clue = Clue(state.team, action["word"].strip().lower(), action["number"])
            return replace(
                state, phase=GUESS, clue=clue, clues=state.clues + (clue,), guesses_made=0
            )
        if action["action"] == "stop":
            return self._next_team(state)
        word = action["word"].strip().lower()
        kind = _kind(state, word)
        state = replace(
            state,
            reveals=state.reveals + (Reveal(state.team, word, kind),),
            guesses_made=state.guesses_made + 1,
        )
        if kind == TRAP:
            return replace(state, winner=1 - state.team, over=True)
        for team in (state.team, 1 - state.team):
            if _left(state, team) == 0:
                return replace(state, winner=team, over=True)
        if kind != TEAM_NAMES[state.team] or state.guesses_made >= _guess_limit(state):
            return self._next_team(state)
        return state

    def forfeit(self, state: WLState, seat: int) -> WLState:
        state = replace(state, forfeits=state.forfeits + (seat,))
        if state.phase == CLUE:
            state = replace(state, skipped=state.skipped + 1)
        return self._next_team(state)

    def _next_team(self, state: WLState) -> WLState:
        state = replace(state, team=1 - state.team, phase=CLUE, clue=None, guesses_made=0)
        if len(state.clues) + state.skipped < MAX_CLUES:
            return state
        a, b = _left(state, 0), _left(state, 1)
        winner = None if a == b else (0 if a < b else 1)
        return replace(state, winner=winner, over=True)

    def is_terminal(self, state: WLState) -> bool:
        return state.over

    def scores(self, state: WLState) -> list[float]:
        if not state.over:
            raise ValueError("scores() called on a non-terminal state")
        if state.winner is None:
            return [0.5] * 4
        return [1.0 if seat // 2 == state.winner else 0.0 for seat in range(4)]

    def final_placements(self, state: WLState, forfeits: Sequence[int]) -> list[int]:
        return placements_from_scores(self.scores(state))


def team_stats(state: WLState, team: int) -> JSON:
    """The numbers behind `store.metrics.word_link_metrics`, for one finished team."""
    name = TEAM_NAMES[team]
    mine = [r for r in state.reveals if r.team == team]
    return {
        "clues": sum(c.team == team for c in state.clues),
        "own_words": sum(r.kind == name for r in mine),
        "guesses": len(mine),
        "trap_hits": sum(r.kind == TRAP for r in mine),
        "won": state.winner == team,
    }
