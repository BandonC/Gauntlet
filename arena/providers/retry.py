"""Wraps any Provider with (1) a shared rate limiter around every attempt and
(2) retries for transient failures: exponential backoff with full jitter, honouring
retry-after (which also pauses every other caller sharing the limiter)."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from arena.providers.base import Completion, CompletionRequest, Provider, ProviderError
from arena.providers.ratelimit import RateLimiter
from arena.telemetry import PROVIDER_ERRORS, PROVIDER_RETRIES, tracer


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
                        return await self._call(req, attempt)
                return await self._call(req, attempt)
            except ProviderError as e:
                PROVIDER_ERRORS.labels(self.name, str(e.retryable).lower()).inc()
                if not e.retryable or attempt == self._max_attempts:
                    raise
                PROVIDER_RETRIES.labels(self.name).inc()
                delay = self._rng.uniform(0, min(self._max, self._base * 2 ** (attempt - 1)))
                if e.retry_after is not None:
                    delay = max(delay, e.retry_after)
                    if self._limiter:
                        self._limiter.penalize(e.retry_after)
                await self._sleep(delay)
        raise AssertionError("unreachable")

    async def _call(self, req: CompletionRequest, attempt: int) -> Completion:
        """One API call in one span, inside the rate limiter so it times only the vendor."""
        attributes = {
            "arena.provider": self.name,
            "arena.model": req.model,
            "arena.call_attempt": attempt,
        }
        with tracer.start_as_current_span("provider.call", attributes=attributes) as span:
            completion = await self._inner.complete(req)
            span.set_attribute("arena.input_tokens", completion.usage.input_tokens)
            span.set_attribute("arena.output_tokens", completion.usage.output_tokens)
            return completion
