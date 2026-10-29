"""Wraps any Provider with (1) a shared rate limiter around every attempt and
(2) retries for transient failures: exponential backoff with full jitter, honouring
retry-after (which also pauses every other caller sharing the limiter)."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from arena.providers.base import Completion, CompletionRequest, Provider, ProviderError
from arena.providers.ratelimit import RateLimiter


class RetryingProvider:
    def __init__(
        self,
        inner: Provider,
        *,
        max_attempts: int = 4,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ):
        self.name = inner.name
        self._inner = inner
        self._max_attempts = max_attempts
        self._base = base_delay
        self._max = max_delay
        self._limiter = limiter
        self._sleep = sleep
        self._rng = rng or random.Random()

    async def complete(self, req: CompletionRequest) -> Completion:
        for attempt in range(1, self._max_attempts + 1):
            try:
                if self._limiter:
                    async with self._limiter.slot():
                        return await self._inner.complete(req)
                return await self._inner.complete(req)
            except ProviderError as e:
                if not e.retryable or attempt == self._max_attempts:
                    raise
                delay = self._rng.uniform(0, min(self._max, self._base * 2 ** (attempt - 1)))
                if e.retry_after is not None:
                    delay = max(delay, e.retry_after)
                    if self._limiter:
                        self._limiter.penalize(e.retry_after)
                await self._sleep(delay)
        raise AssertionError("unreachable")
