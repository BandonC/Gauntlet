"""Non-LLM players. Use these for almost all engine/worker testing: zero token cost."""

from __future__ import annotations

import json
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
