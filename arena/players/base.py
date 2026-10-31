"""Player contract. LLM players, random bots and scripted test players all implement it,
so the engine, worker and tests never care which kind is in a seat."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from arena.types import JSON, Action, ActionSpec, Usage


@dataclass(frozen=True)
class TurnContext:
    game_name: str
    seat: int
    rules: str
    view: JSON
    view_text: str
    action_spec: ActionSpec
    legal_actions_hint: list[Action] | None
    attempt: int = 1
    previous_error: str | None = None  # why the last attempt this turn was rejected


@dataclass(frozen=True)
class Decision:
    action: Action | None  # None = the player produced no usable action
    raw: str | None = None
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    stop_reason: str | None = None


class Player(Protocol):
    player_id: str

    async def act(self, ctx: TurnContext) -> Decision:
        """Return an action. May raise ProviderError for infrastructure failures;
        those fail the match run (and get retried) rather than forfeiting."""
        ...
