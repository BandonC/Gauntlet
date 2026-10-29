"""Per-provider rate limiting: a token bucket for requests/minute plus a concurrency cap.

Share ONE RateLimiter per provider account across all players in a process, so 50
parallel matches don't each think they have the whole quota. Across processes, pass a
`token_source` (store/ratelimit.py: a Postgres bucket shared by every worker): it then
replaces the in-process requests/minute bucket, while the concurrency cap and
retry-after pauses stay here.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Protocol


class TokenSource(Protocol):
    async def take(self) -> None:
        """Return once one request token has been granted."""
        ...


def default_burst(requests_per_minute: float) -> float:
    return float(max(1, int(requests_per_minute / 10)))


class RateLimiter:
    def __init__(
        self,
        requests_per_minute: float | None = None,
        max_concurrency: int | None = None,
        *,
        burst: int | None = None,
        token_source: TokenSource | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._token_source = token_source
        self._rate = (
            requests_per_minute / 60.0 if requests_per_minute and not token_source else None
        )
        self._capacity = float(burst or default_burst(requests_per_minute or 60))
        self._tokens = self._capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(max_concurrency) if max_concurrency else None

    @classmethod
    def unlimited(cls) -> RateLimiter:
        return cls()

    def penalize(self, seconds: float) -> None:
        """Pause every caller, e.g. after a 429 with a retry-after header."""
        self._blocked_until = max(self._blocked_until, self._clock() + seconds)

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        if self._sem:
            await self._sem.acquire()
        try:
            await self._take_token()
            yield
        finally:
            if self._sem:
                self._sem.release()

    async def _take_token(self) -> None:
        async with self._lock:  # FIFO-ish: waiters queue on the lock
            while True:
                now = self._clock()
                if now < self._blocked_until:
                    await self._sleep(self._blocked_until - now)
                    continue
                if self._token_source is not None:
                    await self._token_source.take()
                    return
                if self._rate is None:
                    return
                self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await self._sleep((1 - self._tokens) / self._rate)
