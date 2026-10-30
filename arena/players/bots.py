"""Non-LLM players. Use these for almost all engine/worker testing: zero token cost."""

from __future__ import annotations

import json
import math
import random

from arena.players.base import Decision, TurnContext
from arena.types import Action


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
    to be true (ties: lower quantity, then higher face).
    """

    def __init__(self, player_id: str = "liars-dice-bot", challenge_below: float = 0.5):
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


def _at_least(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1))
