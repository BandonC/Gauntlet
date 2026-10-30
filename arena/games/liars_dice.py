"""Liar's Dice: 2-6 players, 5 dice each, hidden dice. First real test of player_view
hiding.

Rules (no wild ones, no spot-on):
- Each round every player with dice rolls them in secret. Round r's dice come from
  Random(f"{seed}:{r}"), so a replay re-rolls exactly the same dice; no RNG state is
  stored.
- Players take turns in seat order (skipping players with no dice). On your turn you
  either bid "at least Q dice showing face F across ALL players' dice", or challenge
  the previous bid. A bid must raise the previous one: higher quantity, or the same
  quantity and a higher face. Quantity can't exceed the dice in play.
- On a challenge all dice are revealed. If the bid holds (count >= Q) the challenger
  loses a die, otherwise the bidder does. The loser opens the next round; if they were
  eliminated, the next player with dice in seat order opens it.
- Last player with dice wins (score 1; everyone else 0). Placements rank the rest in
  reverse elimination order: the first player out finishes last.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace

from arena.games.base import Game
from arena.types import JSON, Action, ActionSpec

DICE_PER_PLAYER = 5
FACES = 6

RULES = f"""\
Liar's Dice. Every player starts with {DICE_PER_PLAYER} dice. Each round all players roll in
secret: you see only your own dice. Ones are NOT wild.

Players act in turn. On your turn either:
- bid: claim that at least QUANTITY dice showing FACE (1-{FACES}) exist among ALL players'
  dice combined. Your bid must be higher than the previous bid this round: a higher
  quantity, or the same quantity with a higher face. The first bid of a round can be
  anything. Quantity can't exceed the total number of dice in play.
- challenge: call the previous bid a lie (not allowed as the first action of a round).

On a challenge all dice are revealed. If at least QUANTITY dice show FACE, the bid was
true and the challenger loses one die; otherwise the bidder loses one die. The loser
starts the next round (or the next player with dice, if the loser has none left).
A player with no dice is out. The last player with dice wins."""


@dataclass(frozen=True)
class Bid:
    seat: int
    quantity: int
    face: int


@dataclass(frozen=True)
class LDState:
    seed: int
    dice_counts: tuple[int, ...]
    round: int  # 1-based
    dice: tuple[tuple[int, ...], ...]  # this round's roll, per seat (sorted)
    to_move: int
    bids: tuple[Bid, ...] = ()
    last_reveal: JSON | None = None  # public record of the previous challenge
    winner: int | None = None
    forfeited: tuple[int, ...] = ()
    eliminated: tuple[int, ...] = ()  # seats in the order they ran out of dice


def roll(seed: int, round_no: int, dice_counts: tuple[int, ...]) -> tuple[tuple[int, ...], ...]:
    rng = random.Random(f"{seed}:{round_no}")
    return tuple(tuple(sorted(rng.randint(1, FACES) for _ in range(n))) for n in dice_counts)


def _next_with_dice(counts: tuple[int, ...], after: int) -> int:
    n = len(counts)
    for step in range(1, n + 1):
        seat = (after + step) % n
        if counts[seat] > 0:
            return seat
    raise ValueError("nobody has dice")


def _bid_json(b: Bid) -> JSON:
    return {"seat": b.seat, "quantity": b.quantity, "face": b.face}


class LiarsDice(Game):
    name = "liars_dice"
    min_players = 2
    max_players = 6

    def rules_text(self) -> str:
        return RULES

    def initial_state(self, num_players: int, seed: int) -> LDState:
        if not self.min_players <= num_players <= self.max_players:
            raise ValueError("liars_dice takes 2-6 players")
        counts = (DICE_PER_PLAYER,) * num_players
        return LDState(
            seed=seed, dice_counts=counts, round=1, dice=roll(seed, 1, counts), to_move=0
        )

    def current_player(self, state: LDState) -> int:
        return state.to_move

    # --- views ------------------------------------------------------------------------

    def player_view(self, state: LDState, seat: int) -> JSON:
        # Only this seat's own dice. Everyone else's dice are visible only once revealed
        # by a challenge (last_reveal), which is public information.
        return {
            "you": seat,
            "round": state.round,
            "to_move": state.to_move,
            "your_dice": list(state.dice[seat]),
            "dice_counts": list(state.dice_counts),
            "total_dice": sum(state.dice_counts),
            "bids": [_bid_json(b) for b in state.bids],
            "last_reveal": state.last_reveal,
            "winner": state.winner,
        }

    def spectator_view(self, state: LDState) -> JSON:
        return {
            "round": state.round,
            "to_move": state.to_move,
            "dice": [list(d) for d in state.dice],
            "dice_counts": list(state.dice_counts),
            "total_dice": sum(state.dice_counts),
            "bids": [_bid_json(b) for b in state.bids],
            "last_reveal": state.last_reveal,
            "winner": state.winner,
            "forfeited": list(state.forfeited),
        }

    def render_view(self, view: JSON) -> str:
        lines = [
            f"You are player {view['you']}. Round {view['round']}.",
            f"Your dice: {' '.join(str(d) for d in view['your_dice'])}",
            "Dice per player: "
            + ", ".join(f"player {i}: {n}" for i, n in enumerate(view["dice_counts"]))
            + f" (total {view['total_dice']})",
        ]
        rev = view["last_reveal"]
        if rev:
            dice = "; ".join(
                f"player {i}: {' '.join(map(str, d)) or '-'}" for i, d in enumerate(rev["dice"])
            )
            lines.append(
                f"Last round: player {rev['challenger']} challenged player {rev['bidder']}'s "
                f"bid of {rev['quantity']} x {rev['face']}. Revealed: {dice}. "
                f"Actual count: {rev['actual']}. Player {rev['loser']} lost a die."
            )
        if view["bids"]:
            lines.append(
                "Bids this round: "
                + ", ".join(
                    f"player {b['seat']}: {b['quantity']} x {b['face']}" for b in view["bids"]
                )
            )
            last = view["bids"][-1]
            lines.append(
                f"Raise {last['quantity']} x {last['face']} (more dice, or same quantity "
                "and a higher face), or challenge it."
            )
        else:
            lines.append("You open this round: make the first bid.")
        return "\n".join(lines)

    # --- actions ----------------------------------------------------------------------

    def action_spec(self, state: LDState, seat: int) -> ActionSpec:
        return ActionSpec(
            name="liars_dice_move",
            description="Bid (quantity and face) or challenge the previous bid.",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["bid", "challenge"]},
                    "quantity": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "For a bid: how many dice show the face (all players).",
                    },
                    "face": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": FACES,
                        "description": "For a bid: the face value, 1-6.",
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        )

    def legal_actions_hint(self, state: LDState, seat: int) -> list[Action]:
        total = sum(state.dice_counts)
        acts: list[Action] = [
            {"action": "bid", "quantity": q, "face": f}
            for q in range(1, total + 1)
            for f in range(1, FACES + 1)
            if _raises(state, q, f)
        ]
        if state.bids:
            acts.append({"action": "challenge"})
        return acts

    def validate_action(self, state: LDState, seat: int, action: Action) -> str | None:
        if self.is_terminal(state):
            return "the game is over"
        if seat != state.to_move:
            return "it is not your turn"
        if not isinstance(action, dict):
            return "action must be an object"
        kind = action.get("action")
        if kind == "challenge":
            return None if state.bids else "there is no bid to challenge; make the first bid"
        if kind != "bid":
            return "action must be 'bid' or 'challenge'"
        q, f = action.get("quantity"), action.get("face")
        if not _is_int(q) or not _is_int(f):
            return "a bid needs integer 'quantity' and 'face'"
        total = sum(state.dice_counts)
        if not 1 <= f <= FACES:
            return f"face must be 1-{FACES}"
        if not 1 <= q <= total:
            return f"quantity must be 1-{total} (dice in play)"
        if not _raises(state, q, f):
            last = state.bids[-1]
            return (
                f"bid {q} x {f} does not raise {last.quantity} x {last.face}: "
                "bid more dice, or the same quantity with a higher face"
            )
        return None

    def apply(self, state: LDState, seat: int, action: Action) -> LDState:
        if action["action"] == "bid":
            bid = Bid(seat, action["quantity"], action["face"])
            return replace(
                state,
                bids=state.bids + (bid,),
                to_move=_next_with_dice(state.dice_counts, seat),
            )
        last = state.bids[-1]
        actual = sum(d == last.face for hand in state.dice for d in hand)
        loser = seat if actual >= last.quantity else last.seat
        reveal = {
            "round": state.round,
            "dice": [list(d) for d in state.dice],
            "bidder": last.seat,
            "quantity": last.quantity,
            "face": last.face,
            "challenger": seat,
            "actual": actual,
            "loser": loser,
        }
        return self._lose_die(replace(state, last_reveal=reveal), loser)

    def forfeit(self, state: LDState, seat: int) -> LDState:
        # The forfeiting seat is out. The game goes on if two or more still have dice.
        counts = tuple(0 if i == seat else n for i, n in enumerate(state.dice_counts))
        state = replace(state, forfeited=state.forfeited + (seat,))
        return self._new_round(state, counts, opener_hint=seat)

    def _lose_die(self, state: LDState, loser: int) -> LDState:
        counts = tuple(n - 1 if i == loser else n for i, n in enumerate(state.dice_counts))
        return self._new_round(state, counts, opener_hint=loser)

    def _new_round(self, state: LDState, counts: tuple[int, ...], opener_hint: int) -> LDState:
        out = tuple(i for i, n in enumerate(counts) if n == 0 and state.dice_counts[i] > 0)
        state = replace(state, eliminated=state.eliminated + out)
        alive = [i for i, n in enumerate(counts) if n > 0]
        if len(alive) == 1:
            return replace(state, dice_counts=counts, bids=(), winner=alive[0])
        # The loser opens; if they're out, the next player with dice in seat order does.
        opener = opener_hint if counts[opener_hint] > 0 else _next_with_dice(counts, opener_hint)
        r = state.round + 1
        return replace(
            state,
            dice_counts=counts,
            round=r,
            dice=roll(state.seed, r, counts),
            to_move=opener,
            bids=(),
        )

    def is_terminal(self, state: LDState) -> bool:
        return state.winner is not None

    def scores(self, state: LDState) -> list[float]:
        if state.winner is None:
            raise ValueError("scores() called on a non-terminal state")
        return [1.0 if s == state.winner else 0.0 for s in range(len(state.dice_counts))]

    def placements(self, state: LDState) -> list[int]:
        if state.winner is None:
            raise ValueError("placements() called on a non-terminal state")
        ranks = [0] * len(state.dice_counts)
        for rank, seat in enumerate((state.winner, *reversed(state.eliminated)), 1):
            ranks[seat] = rank
        return ranks


def _is_int(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _raises(state: LDState, quantity: int, face: int) -> bool:
    if not state.bids:
        return True
    last = state.bids[-1]
    return quantity > last.quantity or (quantity == last.quantity and face > last.face)
