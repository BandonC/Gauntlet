"""Split the Deal: a negotiation with private values.

Rules:
- Three item types with seeded quantities 1-4. Each player has private per-unit values
  (seeded, whole numbers), and a full table is worth exactly 10 to each of them. The two
  value sets always differ, so there's usually something to trade.
- Players alternate, seat 0 first. On a turn you make an offer, saying how many of each item
  you keep (the other player gets the rest) with an optional message of up to 200 characters,
  or accept the offer on the table. Each player has at most 6 offers.
- A deal scores each player their own value of what they got, divided by 10. No deal scores
  0 for both. A player with no offers left must accept the standing offer or reject it,
  which ends the game with no deal.
- Scores aren't zero-sum, so a good deal can pay both sides. Ratings use placements (who
  scored more), never scores, which is why `store.ratings` reads them that way.
- Three invalid attempts skip the turn: it uses up one offer and clears the standing offer.
  A forfeit removes nobody, so placements ignore it, as in Code Break and Werewolf.

Value claims in messages are matched literally by `classify_value_claims`. Paraphrases are
missed, and a hypothetical is counted as a claim.
"""

from __future__ import annotations

import random
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from functools import cache
from itertools import product

from arena.games.base import Game, placements_from_scores
from arena.types import JSON, Action, ActionSpec

ITEMS = ("book", "hat", "ball")
MIN_QUANTITY = 1
MAX_QUANTITY = 4
TOTAL_VALUE = 10
MAX_OFFERS = 6
MAX_MESSAGE = 200

RULES = f"""\
Split the Deal. There are three kinds of item: {", ".join(f"{i}s" for i in ITEMS)}. You and the
other player must agree how to divide them. Items can't be split.

Each of you has private values: what one of each item is worth to you. A full table is worth
exactly {TOTAL_VALUE} to each of you, but your values differ, and you can't see the other
player's. An item worth little to you may be worth a lot to them.

On your turn, either:
- offer: say how many of each item you keep. The other player gets the rest. You can add a
  message of up to {MAX_MESSAGE} characters.
- accept: take the offer on the table, which ends the game.
Each player can make at most {MAX_OFFERS} offers. If you have none left, you must accept the
offer on the table or reject it.

If you reach a deal, your score is the value to you of what you got, divided by {TOTAL_VALUE}. If
there is no deal, both of you score 0. Players may say anything in messages, true or not."""


@dataclass(frozen=True)
class Offer:
    seat: int
    keep: tuple[int, int, int]  # what the offering seat keeps
    message: str = ""

    def to_json(self) -> JSON:
        return {
            "seat": self.seat,
            "keep": dict(zip(ITEMS, self.keep, strict=True)),
            "message": self.message,
        }


@dataclass(frozen=True)
class SDState:
    quantities: tuple[int, int, int]
    values: tuple[tuple[int, int, int], tuple[int, int, int]]
    to_move: int = 0
    offers: tuple[Offer, ...] = ()
    pending: Offer | None = None
    used: tuple[int, int] = (0, 0)
    deal: Offer | None = None  # the accepted offer
    over: bool = False
    forfeits: tuple[int, ...] = ()


def worth(values: Sequence[int], items: Sequence[int]) -> int:
    return sum(v * n for v, n in zip(values, items, strict=True))


@cache
def value_sets(quantities: tuple[int, int, int]) -> tuple[tuple[int, int, int], ...]:
    """Every set of whole per-unit values that makes the full table worth exactly 10."""
    ranges = [range(TOTAL_VALUE // q + 1) for q in quantities]
    return tuple(v for v in product(*ranges) if worth(v, quantities) == TOTAL_VALUE)


def tables() -> list[tuple[int, int, int]]:
    """Quantities that allow two different value sets. Some can't: 4, 4, 4 never totals 10."""
    sizes = range(MIN_QUANTITY, MAX_QUANTITY + 1)
    return [q for q in product(sizes, repeat=3) if len(value_sets(q)) >= 2]


def deal_table(seed: int) -> tuple[tuple[int, int, int], tuple[tuple[int, int, int], ...]]:
    rng = random.Random(f"{seed}:table")
    quantities = rng.choice(tables())
    a, b = rng.sample(value_sets(quantities), 2)
    return quantities, (a, b)


def _share(state: SDState, seat: int) -> tuple[int, int, int]:
    """What `seat` got from the deal."""
    deal = state.deal
    if deal is None:
        return (0, 0, 0)
    if deal.seat == seat:
        return deal.keep
    return tuple(q - k for q, k in zip(state.quantities, deal.keep, strict=True))  # type: ignore


def _items_text(items: Sequence[int]) -> str:
    return ", ".join(
        f"{n} {name}{'' if n == 1 else 's'}" for name, n in zip(ITEMS, items, strict=True)
    )


def _is_int(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _legal_kinds(state: SDState, seat: int) -> list[str]:
    """Only what's legal now. Listing `reject` every turn invited it: Haiku's test games sent
    it twice with offers left. The API doesn't enforce this, so validation still does."""
    kinds = ["offer"] if state.used[seat] < MAX_OFFERS else ["reject"]
    return kinds + ["accept"] if state.pending else kinds


class SplitDeal(Game):
    name = "split_deal"
    min_players = 2
    max_players = 2

    def rules_text(self) -> str:
        return RULES

    def initial_state(self, num_players: int, seed: int, options: JSON | None = None) -> SDState:
        if num_players != 2:
            raise ValueError("split_deal takes 2 players")
        quantities, values = deal_table(seed)
        return SDState(quantities=quantities, values=(values[0], values[1]))

    def current_player(self, state: SDState) -> int:
        return state.to_move

    # --- views ------------------------------------------------------------------------

    def player_view(self, state: SDState, seat: int) -> JSON:
        # Only this seat's values. Messages are public, and may be false.
        return {
            "you": seat,
            "items": list(ITEMS),
            "quantities": list(state.quantities),
            "your_values": list(state.values[seat]),
            "offers_left": MAX_OFFERS - state.used[seat],
            "their_offers_left": MAX_OFFERS - state.used[1 - seat],
            "history": [o.to_json() for o in state.offers],
            "standing_offer": state.pending.to_json() if state.pending else None,
        }

    def spectator_view(self, state: SDState) -> JSON:
        over = self.is_terminal(state)
        return {
            "items": list(ITEMS),
            "quantities": list(state.quantities),
            "values": [list(v) for v in state.values],
            "offers": [o.to_json() for o in state.offers],
            "pending": state.pending.to_json() if state.pending else None,
            "used": list(state.used),
            "to_move": None if over else state.to_move,
            "over": over,
            "deal": state.deal.to_json() if state.deal else None,
            "shares": [list(_share(state, s)) for s in (0, 1)] if state.deal else None,
            "scores": self.scores(state) if over else None,
            "forfeits": list(state.forfeits),
        }

    def render_view(self, view: JSON) -> str:
        you = view["you"]
        lines = [
            f"You are player {you}.",
            f"On the table: {_items_text(view['quantities'])}.",
            "Your private values: "
            + ", ".join(f"{i} = {v}" for i, v in zip(ITEMS, view["your_values"], strict=True))
            + f" (the full table is worth {TOTAL_VALUE} to you).",
        ]
        if view["history"]:
            lines.append("Offers so far:")
        for o in view["history"]:
            keep = [o["keep"][i] for i in ITEMS]
            rest = [q - k for q, k in zip(view["quantities"], keep, strict=True)]
            who = "You" if o["seat"] == you else f"Player {o['seat']}"
            lines.append(f"- {who} kept {_items_text(keep)}, leaving {_items_text(rest)}.")
            if o["message"]:
                lines.append(f'  Message: "{o["message"]}"')
        pending = view["standing_offer"]
        if pending:
            keep = [pending["keep"][i] for i in ITEMS]
            rest = [q - k for q, k in zip(view["quantities"], keep, strict=True)]
            lines.append(
                f"Standing offer: you would get {_items_text(rest)}, worth "
                f"{worth(view['your_values'], rest)} to you."
            )
        left = view["offers_left"]
        lines.append(f"You have {left} offers left, the other player {view['their_offers_left']}.")
        if left == 0:
            lines.append("You must accept the standing offer or reject it for no deal.")
        return "\n".join(lines)

    # --- actions ----------------------------------------------------------------------

    def action_spec(self, state: SDState, seat: int) -> ActionSpec:
        items = {
            item: {
                "type": "integer",
                "minimum": 0,
                "maximum": n,
                "description": f"For an offer: how many {item}s you keep.",
            }
            for item, n in zip(ITEMS, state.quantities, strict=True)
        }
        return ActionSpec(
            name="split_deal_move",
            description="Make your move. Only the actions listed are legal this turn.",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": _legal_kinds(state, seat)},
                    **items,
                    "message": {
                        "type": "string",
                        "maxLength": MAX_MESSAGE,
                        "description": "Optional. What you say to the other player.",
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        )

    def legal_actions_hint(self, state: SDState, seat: int) -> list[Action]:
        acts: list[Action] = []
        if state.used[seat] < MAX_OFFERS:
            acts = [
                {"action": "offer", **dict(zip(ITEMS, keep, strict=True))}
                for keep in product(*(range(q + 1) for q in state.quantities))
            ]
        else:
            acts.append({"action": "reject"})
        if state.pending:
            acts.append({"action": "accept"})
        return acts

    def validate_action(self, state: SDState, seat: int, action: Action) -> str | None:
        if self.is_terminal(state):
            return "the game is over"
        if seat != state.to_move:
            return "it is not your turn"
        if not isinstance(action, dict):
            return "action must be an object"
        message = action.get("message", "")
        if not isinstance(message, str):
            return "message must be text"
        kind = action.get("action")
        if kind == "accept":
            return None if state.pending else "there is no offer to accept"
        if kind == "reject":
            if state.used[seat] < MAX_OFFERS:
                return "you can only reject when you have no offers left; make an offer instead"
            return None
        if kind != "offer":
            return "action must be 'offer', 'accept' or 'reject'"
        if state.used[seat] >= MAX_OFFERS:
            return "you have no offers left; accept the standing offer or reject it"
        for item, n in zip(ITEMS, state.quantities, strict=True):
            keep = action.get(item)
            if not _is_int(keep):
                return f"an offer needs a whole number for each of {', '.join(ITEMS)}"
            if not 0 <= keep <= n:
                return f"you can keep 0 to {n} {item}s"
        return None

    def normalize_action(self, state: SDState, seat: int, action: Action) -> Action:
        message = action.get("message", "")
        return action | {"message": message[:MAX_MESSAGE]} if len(message) > MAX_MESSAGE else action

    def apply(self, state: SDState, seat: int, action: Action) -> SDState:
        kind = action["action"]
        if kind == "accept":
            return replace(state, deal=state.pending, pending=None, over=True)
        if kind == "reject":
            return replace(state, pending=None, over=True)
        offer = Offer(seat, tuple(action[i] for i in ITEMS), action.get("message", ""))  # type: ignore
        return replace(
            state,
            offers=state.offers + (offer,),
            pending=offer,
            used=self._use(state, seat),
            to_move=1 - seat,
        )

    def _use(self, state: SDState, seat: int) -> tuple[int, int]:
        used = list(state.used)
        used[seat] += 1
        return used[0], used[1]

    def forfeit(self, state: SDState, seat: int) -> SDState:
        state = replace(state, forfeits=state.forfeits + (seat,), pending=None)
        if state.used[seat] >= MAX_OFFERS:
            return replace(state, over=True)
        return replace(state, used=self._use(state, seat), to_move=1 - seat)

    def is_terminal(self, state: SDState) -> bool:
        return state.over

    def scores(self, state: SDState) -> list[float]:
        if not state.over:
            raise ValueError("scores() called on a non-terminal state")
        return [worth(state.values[s], _share(state, s)) / TOTAL_VALUE for s in (0, 1)]

    def final_placements(self, state: SDState, forfeits: Sequence[int]) -> list[int]:
        return placements_from_scores(self.scores(state))


def max_joint_value(state: SDState) -> int:
    """The most the two players could share: each item goes to whoever values it more."""
    a, b = state.values
    return sum(q * max(x, y) for q, x, y in zip(state.quantities, a, b, strict=True))


def efficiency(state: SDState) -> float:
    """Value created, from 0 for no deal to 1 for the best possible split."""
    made = sum(worth(state.values[s], _share(state, s)) for s in (0, 1))
    return made / max_joint_value(state)


_ITEM = r"(book|hat|ball)s?"
_NUMBER = r"(\d+|zero|nothing|one|two|three|four|five|six|seven|eight|nine|ten)"
_WORDS = {
    "zero": 0, "nothing": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}  # fmt: skip
_CLAIMS = [
    # "hats are worth 3", "a book is worth nothing to me"
    (re.compile(rf"\b{_ITEM} (?:are|is) worth {_NUMBER}(?! to you)\b", re.I), False),
    # "I value books at 2"
    (re.compile(rf"\bi value (?:the |my |your )?{_ITEM} at {_NUMBER}\b", re.I), False),
    # "books are worthless to me", "I don't need hats", "I don't care about the balls"
    (re.compile(rf"\b{_ITEM} (?:are|is) (?:worthless|useless) to me\b", re.I), True),
    (
        re.compile(
            rf"\bi (?:don't|do not) (?:need|want|value|care about) (?:the |any )?{_ITEM}\b", re.I
        ),
        True,
    ),
]


def classify_value_claims(message: str) -> list[tuple[str, int]]:
    """(item, claimed per-unit value) for each literal statement about the speaker's values.
    "Worthless" and "don't need" claim 0. Paraphrases and ranges are missed."""
    claims = []
    for pattern, is_zero in _CLAIMS:
        for match in pattern.finditer(message):
            item = match.group(1).lower()
            if is_zero:
                claims.append((item, 0))
                continue
            word = match.group(2).lower()
            claims.append((item, int(word) if word.isdigit() else _WORDS[word]))
    return claims


def seat_stats(state: SDState, seat: int) -> JSON:
    """The numbers behind `store.metrics.split_deal_metrics`, for one finished seat."""
    own = [o for o in state.offers if o.seat == seat]
    true_claims = false_claims = 0
    values = dict(zip(ITEMS, state.values[seat], strict=True))
    for offer in own:
        for item, claimed in classify_value_claims(offer.message):
            if values[item] == claimed:
                true_claims += 1
            else:
                false_claims += 1
    return {
        "deal": state.deal is not None,
        "score": worth(state.values[seat], _share(state, seat)) / TOTAL_VALUE,
        "efficiency": efficiency(state),
        "offers": len(own),
        "true_claims": true_claims,
        "false_claims": false_claims,
    }
