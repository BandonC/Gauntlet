"""Non-LLM players. Use these for almost all engine/worker testing: zero token cost."""

from __future__ import annotations

import json
import math
import random
import zlib
from dataclasses import dataclass
from functools import cache
from itertools import combinations_with_replacement, product
from pathlib import Path
from typing import Any

from arena.balance import DEFAULT_P, vote_target
from arena.games.code_break import (
    ALL_CODES,
    consistent,
    guess_action,
    history_of,
    minimax_guess,
)
from arena.games.split_deal import MAX_OFFERS, TOTAL_VALUE, worth
from arena.games.word_link import MAX_NUMBER, NEUTRAL, TRAP
from arena.games.word_link_words import TOPICS
from arena.players.base import Decision, Player, TurnContext
from arena.types import Action

LIARS_DICE_V1_THRESHOLD = 0.5
# Tuned by `arena.dice_sim`: 0.05 beats v1 by as much, but 0.10 beats every other threshold.
LIARS_DICE_V2_THRESHOLD = 0.1


class RandomPlayer:
    """Picks uniformly from legal_actions_hint. Stateless: the choice is a pure function
    of (seed, view, attempt), so a resumed match replays identically on a fresh worker."""

    def __init__(self, player_id: str = "random", seed: int = 0):
        self.player_id = player_id
        self.seed = seed

    async def act(self, ctx: TurnContext) -> Decision:
        if not ctx.legal_actions_hint:
            raise ValueError(f"{ctx.game_name} has no legal_actions_hint; RandomPlayer can't play")
        key = f"{self.seed}|{json.dumps(ctx.view, sort_keys=True)}|{ctx.attempt}"
        return Decision(action=random.Random(key).choice(ctx.legal_actions_hint))


class ScriptedPlayer:
    """Plays a fixed list of actions in order (including deliberately illegal ones).
    Records every context it saw, so tests can assert on retry feedback."""

    def __init__(self, actions: list[Action | None], player_id: str = "scripted"):
        self.player_id = player_id
        self._actions = list(actions)
        self.seen: list[TurnContext] = []

    async def act(self, ctx: TurnContext) -> Decision:
        self.seen.append(ctx)
        if not self._actions:
            raise AssertionError(f"{self.player_id} ran out of scripted actions")
        return Decision(action=self._actions.pop(0))


class LiarsDiceBot:
    """Liar's Dice heuristic. Stateless: decides from the player_view alone.

    Challenges when the previous bid is less likely than not to hold, given its own
    dice (each unseen die shows a face with probability 1/6; ones aren't wild).
    Otherwise makes the minimal legal raise on each face and bids the one most likely
    to be true (ties: lower quantity, then higher face). The minimal raise on a face is
    that face's most likely legal raise, so this already covers every legal raise.

    `liars-dice-bot-v2` is this same rule with a lower threshold. v1 keeps its 0.5, so tournaments
    played against it stay comparable.
    """

    def __init__(
        self, player_id: str = "liars-dice-bot", challenge_below: float = LIARS_DICE_V1_THRESHOLD
    ):
        self.player_id = player_id
        self.challenge_below = challenge_below

    async def act(self, ctx: TurnContext) -> Decision:
        v = ctx.view
        own, total = v["your_dice"], v["total_dice"]
        unseen = total - len(own)

        def p_true(q: int, f: int) -> float:
            return _at_least(q - own.count(f), unseen, 1 / 6)

        bids = v["bids"]
        if bids:
            last = bids[-1]
            if p_true(last["quantity"], last["face"]) < self.challenge_below:
                return Decision(action={"action": "challenge"})
        options = []
        for f in range(1, 7):
            if not bids:
                q = 1
            elif f > bids[-1]["face"]:
                q = bids[-1]["quantity"]
            else:
                q = bids[-1]["quantity"] + 1
            if q <= total:
                options.append((-p_true(q, f), q, -f))
        if not options:  # nothing left to raise to
            return Decision(action={"action": "challenge"})
        _, q, neg_f = min(options)
        return Decision(action={"action": "bid", "quantity": q, "face": -neg_f})


CFR_POLICY_PATH = Path(__file__).resolve().parent / "data" / "liars_dice_small_cfr.bin"


@dataclass(frozen=True)
class CFRPolicy:
    """The trained `liars_dice_small` policy (see `arena.solvers.liars_dice_cfr`), read with the
    stdlib only: per round state, uint8 probabilities indexed by (node, mover's roll, action)."""

    faces: int
    dice: int
    rounds: dict[tuple[int, int, int], tuple[int, int, int]]  # state -> (offset, k, a)
    data: bytes

    def probabilities(self, state: tuple[int, int, int], node: int, roll: int) -> list[int]:
        offset, k, a = self.rounds[state]
        start = offset + (node * k + roll) * a
        return list(self.data[start : start + a])


@cache
def load_cfr_policy(path: Path = CFR_POLICY_PATH) -> CFRPolicy:
    raw = zlib.decompress(path.read_bytes())
    size = int.from_bytes(raw[:4], "big")
    header = json.loads(raw[4 : 4 + size])
    body = raw[4 + size :]
    rounds = {tuple(r["state"]): (r["offset"], r["k"], r["a"]) for r in header["rounds"]}
    return CFRPolicy(header["faces"], header["dice"], rounds, body)  # type: ignore[arg-type]


class LiarsDiceCFRBot:
    """`liars-dice-cfr`: plays the CFR+ average strategy for `liars_dice_small`. Stateless: the
    round state, the bid sequence and its own roll all come from the view; the opener is whoever
    bid first, or this seat if nobody has. Samples with an RNG keyed on (seed, view, attempt), so
    a resumed match decides the same way. All-zero stored rows (probabilities rounded away) fall
    back to uniform over legal moves, as the trainer's evaluation does."""

    def __init__(
        self, player_id: str = "liars-dice-cfr", seed: int = 0, path: Path = CFR_POLICY_PATH
    ):
        self.player_id = player_id
        self.seed = seed
        self.path = path

    async def act(self, ctx: TurnContext) -> Decision:
        policy = load_cfr_policy(self.path)
        v = ctx.view
        me, counts, bids = v["you"], v["dice_counts"], v["bids"]
        faces = policy.faces
        opener = bids[0]["seat"] if bids else me
        state = (counts[0], counts[1], opener)
        node = 0
        for b in bids:
            node |= 1 << ((b["quantity"] - 1) * faces + b["face"] - 1)
        roll = list(combinations_with_replacement(range(1, faces + 1), counts[me]))
        r = roll.index(tuple(sorted(v["your_dice"])))
        weights = policy.probabilities(state, node, r)
        nb = len(weights) - 1
        last = max(((b["quantity"] - 1) * faces + b["face"] - 1 for b in bids), default=-1)
        legal = [a for a in range(nb) if a > last] + ([nb] if bids else [])
        w = [weights[a] for a in legal]
        if not any(w):
            w = [1] * len(legal)
        rng = random.Random(f"{self.seed}|{json.dumps(v, sort_keys=True)}|{ctx.attempt}")
        a = rng.choices(legal, weights=w)[0]
        if a == nb:
            return Decision(action={"action": "challenge"})
        return Decision(action={"action": "bid", "quantity": a // faces + 1, "face": a % faces + 1})


class WerewolfBot:
    """Scripted Werewolf player. Stateless: every choice is a pure function of (seed, view,
    attempt). Wolves back their partner's kill, the seer inspects new seats and names any
    wolf it finds, and everyone else claims to be a villager and votes at random."""

    def __init__(self, player_id: str = "werewolf-bot", seed: int = 0):
        self.player_id = player_id
        self.seed = seed

    async def act(self, ctx: TurnContext) -> Decision:
        v = ctx.view
        rng = random.Random(f"{self.seed}|{json.dumps(v, sort_keys=True)}|{ctx.attempt}")
        targets = v["legal_targets"]
        found = [r["seat"] for r in v["seer_results"] if r["wolf"] and r["seat"] in v["alive"]]
        if v["phase"] == "discussion":
            others = [s for s in v["alive"] if s != v["you"]]
            text = f"I am a villager. I suspect player {rng.choice(others)}."
            if found:
                text = f"Player {found[0]} is a wolf. Vote with me."
            return Decision(action={"action": "say", "message": text})
        if v["phase"] == "vote":
            target = found[0] if found else rng.choice(targets)
            return Decision(action={"action": "vote", "target": target})
        if v["role"] == "wolf":
            backed = [w["target"] for w in v["wolf_votes"] if w["day"] == v["day"]]
            target = next((t for t in backed if t in targets), rng.choice(targets))
            return Decision(action={"action": "kill", "target": target})
        seen = {r["seat"] for r in v["seer_results"]}
        fresh = [t for t in targets if t not in seen]
        return Decision(action={"action": "inspect", "target": rng.choice(fresh or targets)})


class AccuracyVoteBot(WerewolfBot):
    """WerewolfBot whose day vote hits a wolf with probability p (`balance.vote_target`).
    For balance simulations only: it is told every role, so it is left out of `build_bot`
    and can't reach a tournament. A table shares one `seed` so `focus` can line votes up."""

    def __init__(
        self,
        player_id: str,
        seed: int,
        roles: list[str],
        p: float = DEFAULT_P,
        focus: float = 0.0,
    ):
        super().__init__(player_id, seed)
        self.roles = roles
        self.p = p
        self.focus = focus

    async def act(self, ctx: TurnContext) -> Decision:
        v = ctx.view
        if v["phase"] != "vote":
            return await super().act(ctx)
        target = vote_target(
            self.roles, v["alive"], v["you"], v["day"], self.seed, self.p, self.focus
        )
        return Decision(action={"action": "vote", "target": target})


class CodeBreakRandomBot:
    """Guesses any code that fits everything it's been told. Stateless, seeded by its history,
    so a resumed match plays on identically."""

    def __init__(self, player_id: str = "code-break-random", seed: int = 0):
        self.player_id = player_id
        self.seed = seed

    async def act(self, ctx: TurnContext) -> Decision:
        history = history_of(ctx.view["your_guesses"])
        candidates = [c for c in ALL_CODES if consistent(c, history)]
        rng = random.Random(f"{self.seed}|{history}|{ctx.attempt}")
        return Decision(action=guess_action(rng.choice(candidates)))


class CodeBreakSolver:
    """Knuth's minimax: at most 5 guesses on every secret. Stateless."""

    def __init__(self, player_id: str = "code-break-solver"):
        self.player_id = player_id

    async def act(self, ctx: TurnContext) -> Decision:
        return Decision(action=guess_action(minimax_guess(history_of(ctx.view["your_guesses"]))))


class SplitDealBot:
    """Concedes linearly: its target falls from the whole table to half of it over its offers.
    It accepts anything that meets today's target, and always offers the least it can keep at
    that target, so the other player gets the most. With no offers left, anything beats no
    deal. Stateless: the target is worked out from offers left."""

    FLOOR = TOTAL_VALUE / 2

    def __init__(self, player_id: str = "split-deal-bot"):
        self.player_id = player_id

    async def act(self, ctx: TurnContext) -> Decision:
        v = ctx.view
        values, quantities, items = v["your_values"], v["quantities"], v["items"]
        pending = v["standing_offer"]
        target = self._target(v["offers_left"])
        if pending:
            mine = [q - pending["keep"][i] for q, i in zip(quantities, items, strict=True)]
            value = worth(values, mine)
            if value >= target or (v["offers_left"] == 0 and value > 0):
                return Decision(action={"action": "accept"})
        if v["offers_left"] == 0:
            return Decision(action={"action": "reject"})
        options = product(*(range(q + 1) for q in quantities))
        fits = [k for k in options if worth(values, k) >= target]
        keep = min(fits, key=lambda k: (worth(values, k), sum(k), k))
        return Decision(action={"action": "offer", **dict(zip(items, keep, strict=True))})

    def _target(self, offers_left: int) -> float:
        made = MAX_OFFERS - min(offers_left, MAX_OFFERS)
        return TOTAL_VALUE - (TOTAL_VALUE - self.FLOOR) * made / (MAX_OFFERS - 1)


class WordLinkClueBot:
    """Gives the topic from the association table that covers the most of its team's words,
    marked down for the other team's words on the board and heavily for the trap. Stateless."""

    def __init__(self, player_id: str = "word-link-clue-bot"):
        self.player_id = player_id

    async def act(self, ctx: TurnContext) -> Decision:
        v = ctx.view
        key = {w: k for w, k in v["key"].items() if w in _hidden(v)}
        mine, trap = v["team"], TRAP

        def merit(topic: str) -> tuple[int, str]:
            kinds = [key[w] for w in TOPICS[topic] if w in key]
            own = sum(k == mine for k in kinds)
            other = sum(k not in (mine, NEUTRAL, trap) for k in kinds)
            return (own - 2 * other - 5 * (trap in kinds) - (NEUTRAL in kinds), topic)

        topic = max(TOPICS, key=merit)
        own = sum(key.get(w) == mine for w in TOPICS[topic])
        return Decision(action={"word": topic, "number": min(max(own, 1), MAX_NUMBER)})


class WordLinkGuessBot:
    """Guesses the unrevealed words in the clue's topic, then stops. It shares the clue bot's
    table, so together they are a closer partnership than two strangers could be."""

    def __init__(self, player_id: str = "word-link-guess-bot", seed: int = 0):
        self.player_id = player_id
        self.seed = seed

    async def act(self, ctx: TurnContext) -> Decision:
        v = ctx.view
        hidden = _hidden(v)
        fits = [w for w in hidden if w in TOPICS.get(v["clue"]["word"], ())]
        if fits and v["guesses_made"] < v["clue"]["number"]:
            return Decision(action={"action": "guess", "word": fits[0]})
        if v["guesses_made"]:
            return Decision(action={"action": "stop"})
        rng = random.Random(f"{self.seed}|{v['clue']}|{sorted(hidden)}")
        return Decision(action={"action": "guess", "word": rng.choice(sorted(hidden))})


class WordLinkRandomGuesser:
    """Any unrevealed word, and a coin toss on stopping after the first. Stateless."""

    def __init__(self, player_id: str = "word-link-random", seed: int = 0):
        self.player_id = player_id
        self.seed = seed

    async def act(self, ctx: TurnContext) -> Decision:
        v = ctx.view
        hidden = sorted(_hidden(v))
        rng = random.Random(f"{self.seed}|{v['clue']}|{hidden}|{ctx.attempt}")
        if v["guesses_made"] and rng.random() < 0.5:
            return Decision(action={"action": "stop"})
        return Decision(action={"action": "guess", "word": rng.choice(hidden)})


def _hidden(view: dict[str, Any]) -> set[str]:
    return {cell["word"] for cell in view["board"] if not cell["revealed"]}


def build_bot(kind: str, player_id: str, seed: int) -> Player:
    """Roster `provider = "bot"`: `model` names which game's bot."""
    if kind == "werewolf":
        return WerewolfBot(player_id, seed)
    if kind == "liars_dice":
        return LiarsDiceBot(player_id)
    if kind == "liars_dice_v2":
        return LiarsDiceBot(player_id, LIARS_DICE_V2_THRESHOLD)
    if kind == "liars_dice_cfr":
        return LiarsDiceCFRBot(player_id, seed)
    if kind == "split_deal":
        return SplitDealBot(player_id)
    if kind == "word_link_clue":
        return WordLinkClueBot(player_id)
    if kind == "word_link_guess":
        return WordLinkGuessBot(player_id, seed)
    if kind == "word_link_random":
        return WordLinkRandomGuesser(player_id, seed)
    if kind == "code_break":
        return CodeBreakSolver(player_id)
    if kind == "code_break_random":
        return CodeBreakRandomBot(player_id, seed)
    raise ValueError(
        f"unknown bot {kind!r}; known: code_break, code_break_random, liars_dice, "
        "liars_dice_cfr, liars_dice_v2, split_deal, werewolf, word_link_clue, word_link_guess, "
        "word_link_random"
    )


def _at_least(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1))
