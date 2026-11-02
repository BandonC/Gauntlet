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

from arena.store import ratings
from arena.store import tournaments as t_store
from arena.store.spend import TOTAL_SPEND_SQL

CLAIM_SQL = f"""
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
                 OR (t.budget_usd IS NOT NULL AND {TOTAL_SPEND_SQL} >= t.budget_usd))
      )
    ORDER BY available_at, id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING m.id, m.tournament_id, m.game, m.seed, m.run_attempts, m.options,
    (SELECT t.spend_mode FROM tournaments t WHERE t.id = m.tournament_id) AS spend_mode,
    (SELECT t.budget_usd FROM tournaments t WHERE t.id = m.tournament_id) AS budget_usd
"""

HEARTBEAT_SQL = """
UPDATE matches
SET lease_expires_at = now() + make_interval(secs => $3)
WHERE id = $1 AND lease_owner = $2 AND status = 'running'
RETURNING id
"""

COMPLETE_SQL = """
UPDATE matches
SET status = 'completed', scores = $3, placements = $4, completed_at = now(),
    lease_owner = NULL, lease_expires_at = NULL, last_error = NULL
WHERE id = $1 AND lease_owner = $2 AND status = 'running'
RETURNING game, tournament_id
"""

RELEASE_SQL = """
UPDATE matches
SET status = CASE WHEN run_attempts >= $4 THEN 'failed' ELSE 'queued' END,
    available_at = now() + make_interval(secs => $5),
    lease_owner = NULL, lease_expires_at = NULL, last_error = $3
WHERE id = $1 AND lease_owner = $2
RETURNING status, tournament_id
"""

# Graceful shutdown: hand the match straight back. The run attempt is refunded because
# nothing failed, so restarts and deploys never push a match towards 'failed'.
SHUTDOWN_RELEASE_SQL = """
UPDATE matches
SET status = 'queued', available_at = now(), run_attempts = GREATEST(run_attempts - 1, 0),
    lease_owner = NULL, lease_expires_at = NULL
WHERE id = $1 AND lease_owner = $2 AND status = 'running'
RETURNING id
"""

RESTAMP_SQL = "UPDATE matches SET completed_at = clock_timestamp() WHERE id = $1"

SEATS_SQL = "SELECT seat, player_id FROM match_seats WHERE match_id = $1 ORDER BY seat"


@dataclass(frozen=True)
class ClaimedMatch:
    id: int
    tournament_id: int | None
    game: str
    seed: int
    run_attempts: int
    seats: list[str]
    options: dict | None = None
    spend_mode: str | None = None  # None for a match outside any tournament
    has_budget: bool = False


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
        options=row["options"],
        spend_mode=row["spend_mode"],
        has_budget=row["budget_usd"] is not None,
    )


async def heartbeat(
    pool: asyncpg.Pool, match_id: int, worker_id: str, lease_seconds: float
) -> bool:
    return await pool.fetchval(HEARTBEAT_SQL, match_id, worker_id, lease_seconds) is not None


async def complete(
    pool: asyncpg.Pool, match_id: int, worker_id: str, scores: list[float], placements: list[int]
) -> bool:
    """Mark the match completed and rate it, atomically. False if the lease was lost
    (nothing is written)."""
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(COMPLETE_SQL, match_id, worker_id, scores, placements)
        if row is None:
            return False
        seats = [r["player_id"] for r in await conn.fetch(SEATS_SQL, match_id)]
        if await ratings.apply_match_result(conn, match_id, row["game"], seats, scores, placements):
            # Re-stamp while holding the players' rating locks: (completed_at, id) is
            # then exactly the order ratings were applied in, which rebuild replays.
            await conn.execute(RESTAMP_SQL, match_id)
        await t_store.advance_tournament(conn, row["tournament_id"])
    return True


async def release_for_retry(
    pool: asyncpg.Pool,
    match_id: int,
    worker_id: str,
    error: str,
    *,
    max_attempts: int,
    backoff_seconds: float,
) -> str | None:
    """Give the match back after an infrastructure failure. Returns the new status.
    A match that fails for good pauses its tournament until `arena tournament resume`."""
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            RELEASE_SQL, match_id, worker_id, error[:2000], max_attempts, backoff_seconds
        )
        if row is None:
            return None
        if row["status"] == "failed":
            await t_store.pause_tournament(
                conn, row["tournament_id"], f"match {match_id} failed: {error}"
            )
    return row["status"]


async def release_on_shutdown(pool: asyncpg.Pool, match_id: int, worker_id: str) -> bool:
    """Give back a match interrupted by shutdown, claimable immediately. False if the
    lease was already lost."""
    return await pool.fetchval(SHUTDOWN_RELEASE_SQL, match_id, worker_id) is not None
