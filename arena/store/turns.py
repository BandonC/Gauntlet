"""Postgres TurnStore with lease fencing.

The INSERT only happens if this worker still holds an unexpired lease. `FOR SHARE` on
the match row means a concurrent re-claim (which needs a row lock) waits for this
insert to commit, so a worker whose lease just expired can't sneak in a late write.
The (match_id, turn_no) primary key is the second line of defence.

Turn cost is added to the tournament's spent_usd in the same transaction, so the
budget cap reflects every persisted turn across all workers.
"""

from __future__ import annotations

import asyncpg

from arena.engine.match_runner import LeaseLost, TurnRecord
from arena.types import Usage

INSERT_TURN_SQL = """
INSERT INTO turns (
    match_id, turn_no, seat, action, forfeited, attempts, raw_response, error,
    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
    cost_usd, latency_ms, worker_id, truncated
)
-- Explicit casts: params in an INSERT ... SELECT list don't infer the column types.
SELECT $1::bigint, $2::int, $3::smallint, $4::jsonb, $5::boolean, $6::smallint,
       $7::text, $8::text, $9::int, $10::int, $11::int, $12::int,
       $13::numeric, $14::int, $15::text, $16::boolean
WHERE EXISTS (
    SELECT 1 FROM matches
    WHERE id = $1 AND lease_owner = $15 AND lease_expires_at > now()
    FOR SHARE
)
ON CONFLICT (match_id, turn_no) DO NOTHING
RETURNING turn_no
"""

ADD_SPEND_SQL = """
UPDATE tournaments SET spent_usd = spent_usd + $2
WHERE id = (SELECT tournament_id FROM matches WHERE id = $1)
"""

LOAD_SQL = "SELECT * FROM turns WHERE match_id = $1 ORDER BY turn_no"


class PostgresTurnStore:
    def __init__(self, pool: asyncpg.Pool, worker_id: str):
        self._pool = pool
        self._worker_id = worker_id

    async def load_turns(self, match_id: str) -> list[TurnRecord]:
        rows = await self._pool.fetch(LOAD_SQL, int(match_id))
        return [
            TurnRecord(
                turn_no=r["turn_no"],
                seat=r["seat"],
                action=r["action"],
                forfeited=r["forfeited"],
                attempts=r["attempts"],
                raw=r["raw_response"],
                error=r["error"],
                usage=Usage(
                    r["input_tokens"],
                    r["output_tokens"],
                    r["cache_read_tokens"],
                    r["cache_write_tokens"],
                ),
                cost_usd=float(r["cost_usd"]),
                latency_ms=r["latency_ms"],
                truncated=r["truncated"],
            )
            for r in rows
        ]

    async def append_turn(self, match_id: str, t: TurnRecord) -> None:
        mid = int(match_id)
        async with self._pool.acquire() as conn, conn.transaction():
            inserted = await conn.fetchval(
                INSERT_TURN_SQL,
                mid,
                t.turn_no,
                t.seat,
                t.action,
                t.forfeited,
                t.attempts,
                t.raw,
                t.error,
                t.usage.input_tokens,
                t.usage.output_tokens,
                t.usage.cache_read_tokens,
                t.usage.cache_write_tokens,
                t.cost_usd,
                t.latency_ms,
                self._worker_id,
                t.truncated,
            )
            if inserted is None:
                raise LeaseLost(f"match {mid} turn {t.turn_no}: lease lost or turn exists")
            await conn.execute(ADD_SPEND_SQL, mid, t.cost_usd)
