"""Postgres-backed job queue with leases. No Redis: `FOR UPDATE SKIP LOCKED` lets many
workers claim different rows concurrently without blocking each other.

Lifecycle:  queued --claim--> running --complete--> completed
                                 |  \\--release_for_retry--> queued (after backoff)
                                 |   \\--(too many attempts)--> failed
                                 \\--lease expires (worker died)--> claimable again

A worker must heartbeat() well inside the lease window. If heartbeat returns False,
the lease was lost: stop immediately and write nothing more (turn writes are also
fenced by the lease; see turns.py).
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

CLAIM_SQL = """
UPDATE matches m
SET status = 'running',
    lease_owner = $1,
    lease_expires_at = now() + make_interval(secs => $2),
    run_attempts = run_attempts + 1,
    started_at = COALESCE(started_at, now())
WHERE m.id = (
    SELECT id FROM matches
    WHERE available_at <= now()
      AND (status = 'queued' OR (status = 'running' AND lease_expires_at < now()))
      -- don't start matches in paused or over-budget tournaments
      AND NOT EXISTS (
          SELECT 1 FROM tournaments t
          WHERE t.id = matches.tournament_id
            AND (t.status = 'paused'
                 OR (t.budget_usd IS NOT NULL AND t.spent_usd >= t.budget_usd))
      )
    ORDER BY available_at, id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING m.id, m.tournament_id, m.game, m.seed, m.run_attempts
"""

HEARTBEAT_SQL = """
UPDATE matches
SET lease_expires_at = now() + make_interval(secs => $3)
WHERE id = $1 AND lease_owner = $2 AND status = 'running'
RETURNING id
"""

COMPLETE_SQL = """
UPDATE matches
SET status = 'completed', scores = $3, completed_at = now(),
    lease_owner = NULL, lease_expires_at = NULL, last_error = NULL
WHERE id = $1 AND lease_owner = $2 AND status = 'running'
RETURNING id
"""

RELEASE_SQL = """
UPDATE matches
SET status = CASE WHEN run_attempts >= $4 THEN 'failed' ELSE 'queued' END,
    available_at = now() + make_interval(secs => $5),
    lease_owner = NULL, lease_expires_at = NULL, last_error = $3
WHERE id = $1 AND lease_owner = $2
RETURNING status
"""

SEATS_SQL = "SELECT seat, player_id FROM match_seats WHERE match_id = $1 ORDER BY seat"


@dataclass(frozen=True)
class ClaimedMatch:
    id: int
    tournament_id: int | None
    game: str
    seed: int
    run_attempts: int
    seats: list[str]


async def claim(pool: asyncpg.Pool, worker_id: str, lease_seconds: float) -> ClaimedMatch | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(CLAIM_SQL, worker_id, lease_seconds)
        if row is None:
            return None
        seats = [r["player_id"] for r in await conn.fetch(SEATS_SQL, row["id"])]
    return ClaimedMatch(
        id=row["id"],
        tournament_id=row["tournament_id"],
        game=row["game"],
        seed=row["seed"],
        run_attempts=row["run_attempts"],
        seats=seats,
    )


async def heartbeat(
    pool: asyncpg.Pool, match_id: int, worker_id: str, lease_seconds: float
) -> bool:
    return await pool.fetchval(HEARTBEAT_SQL, match_id, worker_id, lease_seconds) is not None


async def complete(pool: asyncpg.Pool, match_id: int, worker_id: str, scores: list[float]) -> bool:
    # TODO(phase 3): update ratings + rating_history in the same transaction.
    return await pool.fetchval(COMPLETE_SQL, match_id, worker_id, scores) is not None


async def release_for_retry(
    pool: asyncpg.Pool,
    match_id: int,
    worker_id: str,
    error: str,
    *,
    max_attempts: int,
    backoff_seconds: float,
) -> str | None:
    """Give the match back after an infrastructure failure. Returns the new status."""
    return await pool.fetchval(
        RELEASE_SQL, match_id, worker_id, error[:2000], max_attempts, backoff_seconds
    )
