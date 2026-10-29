"""Postgres token bucket: requests-per-minute limiting shared by all worker processes.

Taking a token is ONE atomic UPDATE (no explicit transaction): concurrent callers
serialise on the vendor's row, and the row-level re-check under READ COMMITTED means a
token is only granted if the refilled bucket still holds one. On a miss the caller
sleeps until the bucket should hold a whole token and tries again.

Only the requests/minute part lives here. max_concurrency and retry-after pauses stay
in the in-process RateLimiter, which delegates token-taking to this bucket.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import asyncpg

from arena.providers.ratelimit import default_burst
from arena.roster import ProviderLimits

TAKE_SQL = """
UPDATE rate_limits
SET tokens = least(capacity, tokens + extract(epoch from now() - updated_at) * refill_per_sec) - 1,
    updated_at = now()
WHERE vendor = $1
  AND least(capacity, tokens + extract(epoch from now() - updated_at) * refill_per_sec) >= 1
RETURNING tokens
"""

PEEK_SQL = """
SELECT least(capacity, tokens + extract(epoch from now() - updated_at) * refill_per_sec)
           AS tokens,
       refill_per_sec
FROM rate_limits WHERE vendor = $1
"""

# Re-seeding keeps the current fill level (clamped to the new capacity) so restarting
# a worker doesn't hand out a fresh burst.
SEED_SQL = """
INSERT INTO rate_limits (vendor, tokens, capacity, refill_per_sec)
VALUES ($1, $2, $2, $3)
ON CONFLICT (vendor) DO UPDATE
SET capacity = EXCLUDED.capacity,
    refill_per_sec = EXCLUDED.refill_per_sec,
    tokens = least(rate_limits.tokens, EXCLUDED.capacity)
"""


class PostgresTokenBucket:
    def __init__(
        self,
        pool: asyncpg.Pool,
        vendor: str,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._pool = pool
        self.vendor = vendor
        self._sleep = sleep

    async def take(self) -> None:
        while True:
            if await self._pool.fetchval(TAKE_SQL, self.vendor) is not None:
                return
            row = await self._pool.fetchrow(PEEK_SQL, self.vendor)
            if row is None:
                raise RuntimeError(f"no rate_limits row for {self.vendor!r}; seed it first")
            await self._sleep(max(0.0, (1 - row["tokens"]) / row["refill_per_sec"]))


async def seed_rate_limits(pool: asyncpg.Pool, limits: dict[str, ProviderLimits]) -> list[str]:
    """Upsert a bucket per vendor that has requests_per_minute. Returns those vendors."""
    seeded = []
    for vendor, lim in sorted(limits.items()):
        if not lim.requests_per_minute:
            continue
        rpm = lim.requests_per_minute
        await pool.execute(SEED_SQL, vendor, default_burst(rpm), rpm / 60.0)
        seeded.append(vendor)
    return seeded


def postgres_buckets(pool: asyncpg.Pool, vendors: list[str]) -> dict[str, PostgresTokenBucket]:
    return {v: PostgresTokenBucket(pool, v) for v in vendors}
