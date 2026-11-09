"""Code Break: a deduction race on one secret.

Rules:
- A seeded secret of 4 pegs from 6 colours, repeats allowed, the same for both players.
- Players alternate guesses, seat 0 first. Each sees only their own guesses and feedback,
  plus how many guesses the other has made and whether they've solved it.
- Feedback is two counts: exact (right colour, right place) and colour (right colour, wrong
  place). Each peg counts at most once.
- Fewer guesses to solve wins; the same number is a draw; so is neither solving within 10.
  Once a player solves on guess k, the other plays on only to guess k, the last chance to
  tie. Seat 0 going first therefore costs seat 1 nothing.
- A guess names each peg in its own field. The API doesn't enforce the enum, so a model
  can still send two letters in one peg, and validation rejects that. The older form, one
  `code` string, stays valid so tournament 12's log still replays.
- Three invalid attempts skip the guess, which still counts towards the limit. A forfeit
  removes nobody, so placements ignore it, as in Werewolf.

The minimax solver is here because the metrics need it too: it's a pure function of the
secret, and `solver_guesses` is what a model's guess count is measured against.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from itertools import product

from arena.games.base import Game, placements_from_scores
from arena.types import JSON, Action, ActionSpec

COLOURS = "RGBYOP"
PEGS = 4
MAX_GUESSES = 10
FIRST_GUESS = "RRGG"  # Knuth's opening
ALL_CODES = tuple("".join(p) for p in product(COLOURS, repeat=PEGS))
SOLVED = (PEGS, 0)
PEG_FIELDS = tuple(f"peg{i}" for i in range(1, PEGS + 1))
_COLOUR_COUNTS = {code: tuple(code.count(c) for c in COLOURS) for code in ALL_CODES}

RULES = f"""\
Code Break. There is a secret code of {PEGS} pegs. Each peg is one of {len(COLOURS)} colours: \
{", ".join(COLOURS)}. Colours can repeat. You and your opponent are racing to find the same
secret, and neither of you can see the other's guesses or feedback.

Take turns guessing. After each guess you get two numbers:
- exact: pegs with the right colour in the right place.
- colour: pegs with a right colour in the wrong place.
Each peg of the secret is counted at most once, so exact + colour is never more than {PEGS}.
A guess is solved when exact is {PEGS}.

Whoever solves it in fewer guesses wins. The same number of guesses is a draw, and so is
neither of you solving it in {MAX_GUESSES}. Once one player solves it, the other can only tie, by
solving it on the same guess number."""


@dataclass(frozen=True)
class Guess:
    code: str | None  # None: skipped by a forfeit
    exact: int = 0
    colour: int = 0

    @property
    def solved(self) -> bool:
        return (self.exact, self.colour) == SOLVED

    def to_json(self) -> JSON:
        return {"code": self.code, "exact": self.exact, "colour": self.colour}


@dataclass(frozen=True)
class CBState:
    secret: str
    guesses: tuple[tuple[Guess, ...], tuple[Guess, ...]] = ((), ())
    to_move: int = 0
    forfeits: tuple[int, ...] = ()


def code_of(action: object) -> str | None:
    """The 4-letter code in either action form, or None if it isn't a valid guess."""
    if not isinstance(action, dict):
        return None
    if set(action) == {"code"}:
        pegs = action["code"]
    elif set(action) == set(PEG_FIELDS):
        pegs = [action[field] for field in PEG_FIELDS]
        if not all(isinstance(p, str) for p in pegs):
            return None
        pegs = "".join(pegs)
    else:
        return None
    if not isinstance(pegs, str) or len(pegs) != PEGS or set(pegs) - set(COLOURS):
        return None
    return pegs


def guess_action(code: str) -> Action:
    return dict(zip(PEG_FIELDS, code, strict=True))


def make_secret(seed: int) -> str:
    return random.Random(f"{seed}:secret").choice(ALL_CODES)


def feedback(guess: str, secret: str) -> tuple[int, int]:
    """(exact, colour). Takes either order: the count is symmetric."""
    exact = sum(a == b for a, b in zip(guess, secret, strict=True))
    shared = sum(map(min, _COLOUR_COUNTS[guess], _COLOUR_COUNTS[secret]))
    return exact, shared - exact


def consistent(code: str, history: Sequence[tuple[str, tuple[int, int]]]) -> bool:
    """Could `code` be the secret, given what these guesses were told?"""
    return all(feedback(guess, code) == told for guess, told in history)


def solved_in(guesses: Sequence[Guess]) -> int | None:
    return next((i for i, g in enumerate(guesses, 1) if g.solved), None)


def _limit(state: CBState, seat: int) -> int:
    other = solved_in(state.guesses[1 - seat])
    return other if other is not None else MAX_GUESSES


def _finished(state: CBState, seat: int) -> bool:
    mine = state.guesses[seat]
    return solved_in(mine) is not None or len(mine) >= _limit(state, seat)


def _advance(state: CBState, seat: int) -> CBState:
    other = 1 - seat
    return replace(state, to_move=seat if _finished(state, other) else other)


class CodeBreak(Game):
    name = "code_break"
    min_players = 2
    max_players = 2

    def rules_text(self) -> str:
        return RULES

    def initial_state(self, num_players: int, seed: int, options: JSON | None = None) -> CBState:
        if num_players != 2:
            raise ValueError("code_break takes 2 players")
        return CBState(secret=make_secret(seed))

    def current_player(self, state: CBState) -> int:
        return state.to_move

    # --- views ------------------------------------------------------------------------

    def player_view(self, state: CBState, seat: int) -> JSON:
        # Never the secret, and never the other seat's guesses: feedback would give away
        # what they've learned. How far along they are is public.
        other = state.guesses[1 - seat]
        return {
            "you": seat,
            "guess_number": len(state.guesses[seat]) + 1,
            "guess_limit": _limit(state, seat),
            "your_guesses": [g.to_json() for g in state.guesses[seat]],
            "opponent_guesses": len(other),
            "opponent_solved": solved_in(other) is not None,
        }

    def spectator_view(self, state: CBState) -> JSON:
        over = self.is_terminal(state)
        return {
            "secret": state.secret,
            "guesses": [[g.to_json() for g in side] for side in state.guesses],
            "to_move": None if over else state.to_move,
            "scores": self.scores(state) if over else None,
            "forfeits": list(state.forfeits),
        }

    def render_view(self, view: JSON) -> str:
        lines = [f"You are player {view['you']}."]
        guesses = view["your_guesses"]
        if guesses:
            lines.append("Your guesses so far:")
            for i, g in enumerate(guesses, 1):
                told = (
                    "skipped"
                    if g["code"] is None
                    else f"{g['code']}: exact {g['exact']}, colour {g['colour']}"
                )
                lines.append(f"{i}. {told}")
        else:
            lines.append("You have made no guesses yet.")
        lines.append(f"Your opponent has made {view['opponent_guesses']} guesses.")
        if view["opponent_solved"]:
            lines.append(
                f"Your opponent has solved it. Solve it on guess {view['guess_limit']} to draw."
            )
        lines.append(f"This is your guess {view['guess_number']} of {view['guess_limit']}.")
        return "\n".join(lines)

    # --- actions ----------------------------------------------------------------------

    def action_spec(self, state: CBState, seat: int) -> ActionSpec:
        return ActionSpec(
            name="code_break_guess",
            description=f"Guess the secret: one colour for each of the {PEGS} pegs.",
            input_schema={
                "type": "object",
                "properties": {
                    field: {
                        "type": "string",
                        "enum": list(COLOURS),
                        "description": f"The colour of peg {i}.",
                    }
                    for i, field in enumerate(PEG_FIELDS, 1)
                },
                "required": list(PEG_FIELDS),
                "additionalProperties": False,
            },
        )

    def legal_actions_hint(self, state: CBState, seat: int) -> list[Action] | None:
        return None  # 1,296 of them, and the bots work from the view instead

    def validate_action(self, state: CBState, seat: int, action: Action) -> str | None:
        if self.is_terminal(state):
            return "the game is over"
        if seat != state.to_move:
            return "it is not your turn"
        if not isinstance(action, dict):
            return "action must be an object"
        if code_of(action) is None:
            return f"give {', '.join(PEG_FIELDS)}, each one of {', '.join(COLOURS)}"
        return None

    def apply(self, state: CBState, seat: int, action: Action) -> CBState:
        code = code_of(action)
        assert code is not None  # validate_action already required it
        guess = Guess(code, *feedback(code, state.secret))
        return self._record(state, seat, guess)

    def forfeit(self, state: CBState, seat: int) -> CBState:
        state = replace(state, forfeits=state.forfeits + (seat,))
        return self._record(state, seat, Guess(None))

    def _record(self, state: CBState, seat: int, guess: Guess) -> CBState:
        sides = list(state.guesses)
        sides[seat] = sides[seat] + (guess,)
        return _advance(replace(state, guesses=(sides[0], sides[1])), seat)

    def is_terminal(self, state: CBState) -> bool:
        return _finished(state, 0) and _finished(state, 1)

    def scores(self, state: CBState) -> list[float]:
        if not self.is_terminal(state):
            raise ValueError("scores() called on a non-terminal state")
        a, b = (solved_in(side) for side in state.guesses)
        if a == b:
            return [0.5, 0.5]
        if b is None or (a is not None and a < b):
            return [1.0, 0.0]
        return [0.0, 1.0]

    def final_placements(self, state: CBState, forfeits: Sequence[int]) -> list[int]:
        return placements_from_scores(self.scores(state))


def history_of(guesses: Sequence[JSON]) -> tuple[tuple[str, tuple[int, int]], ...]:
    """What a seat's view says it has learned. Skipped guesses taught nothing."""
    return tuple((g["code"], (g["exact"], g["colour"])) for g in guesses if g["code"] is not None)


@lru_cache(maxsize=4096)
def minimax_guess(history: tuple[tuple[str, tuple[int, int]], ...]) -> str:
    """Knuth's minimax: the guess whose worst feedback leaves the fewest candidates, among
    all 1,296 codes, preferring a code that could itself be the secret, then the earliest in
    ALL_CODES. That last rule matters: sorting ties alphabetically needs 6 on some secrets, and
    this order is what solves every one in at most 5. Cached because the tree is small and
    each node compares many codes."""
    if not history:
        return FIRST_GUESS
    candidates = [c for c in ALL_CODES if consistent(c, history)]
    if not candidates:
        raise ValueError("no code fits this feedback")
    if len(candidates) <= 2:
        return candidates[0]
    possible = set(candidates)

    def worst_case(code: str) -> int:
        return max(Counter(feedback(code, c) for c in candidates).values())

    return min(ALL_CODES, key=lambda code: (worst_case(code), code not in possible))


@lru_cache(maxsize=2048)
def solver_guesses(secret: str) -> int:
    """How many guesses the solver takes on this secret."""
    history: list[tuple[str, tuple[int, int]]] = []
    while True:
        guess = minimax_guess(tuple(history))
        told = feedback(guess, secret)
        history.append((guess, told))
        if told == SOLVED:
            return len(history)


def seat_stats(state: CBState, seat: int) -> JSON:
    """The numbers behind `store.metrics.code_break_metrics`, for one finished seat."""
    guesses = state.guesses[seat]
    played = [(g.code, (g.exact, g.colour)) for g in guesses if g.code is not None]
    inconsistent = sum(not consistent(code, played[:i]) for i, (code, _) in enumerate(played))
    solved = solved_in(guesses)
    return {
        "guesses": len(guesses),
        "solved": solved is not None,
        "inconsistent": inconsistent,
        "solver_guesses": solver_guesses(state.secret),
        "above_solver": None if solved is None else solved - solver_guesses(state.secret),
    }
