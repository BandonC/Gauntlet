"""Provider contract: one interface over every LLM API.

A provider makes exactly one API call per `complete()` and forces the model to answer
with the given tool. Retries live in `RetryingProvider`, rate limits in `RateLimiter`,
so vendor adapters stay thin.

Error policy (important for fairness, see CLAUDE.md):
- Infrastructure failures (429, 5xx, timeouts) raise ProviderError. Once retries are
  exhausted they propagate up and fail the *match run*, which the worker re-queues.
  They never count against the player.
- A model that answers but doesn't call the tool is NOT an error here: `tool_input` is
  None and the match runner treats it as an invalid attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from arena.types import Action, ActionSpec, Usage


@dataclass(frozen=True)
class CompletionRequest:
    model: str
    system: str
    user: str
    tool: ActionSpec
    max_tokens: int = 256
    temperature: float | None = 0.0  # None = don't send (some models reject it)
    cache_system: bool = True


@dataclass(frozen=True)
class Completion:
    tool_input: Action | None
    raw_text: str
    usage: Usage


class ProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class Provider(Protocol):
    name: str

    async def complete(self, req: CompletionRequest) -> Completion: ...
