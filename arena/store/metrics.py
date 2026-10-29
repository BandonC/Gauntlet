"""Per-player operational metrics, computed from the turn log.

Definitions (all per player, for one game, optionally one tournament):
- illegal_move_rate: invalid attempts / all attempts. A turn that succeeded on attempt k
  had k-1 invalid attempts; a forfeited turn's attempts were all invalid.
- forfeits: forfeited turns.
- latency_p50_ms / latency_p95_ms: per turn, including retries within the turn.
- cost_per_game_usd: spend in completed matches / completed matches. Turns of matches
  still running count towards everything else.
- cache_hit_rate: cache reads / (uncached input + cache reads + cache writes).
Rates are None when their denominator is zero.
"""

from __future__ import annotations

import asyncpg

METRICS_SQL = """
WITH t AS (
    SELECT s.player_id, t.match_id, m.status AS match_status, t.attempts, t.forfeited,
           t.latency_ms, t.cost_usd, t.input_tokens, t.cache_read_tokens,
           t.cache_write_tokens
    FROM turns t
    JOIN matches m ON m.id = t.match_id
    JOIN match_seats s ON s.match_id = t.match_id AND s.seat = t.seat
    WHERE m.game = $1 AND ($2::bigint IS NULL OR m.tournament_id = $2)
)
SELECT player_id,
       count(*) AS turns,
       sum(attempts) AS attempts,
       sum(attempts) - count(*) FILTER (WHERE NOT forfeited) AS illegal_attempts,
       count(*) FILTER (WHERE forfeited) AS forfeits,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS latency_p50_ms,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS latency_p95_ms,
       count(DISTINCT match_id) FILTER (WHERE match_status = 'completed') AS completed_games,
       coalesce(sum(cost_usd) FILTER (WHERE match_status = 'completed'), 0)::float
           AS completed_cost_usd,
       sum(input_tokens) AS input_tokens,
       sum(cache_read_tokens) AS cache_read_tokens,
       sum(cache_write_tokens) AS cache_write_tokens
FROM t
GROUP BY player_id
ORDER BY player_id
"""


def _ratio(num: float, den: float, digits: int = 4) -> float | None:
    return round(num / den, digits) if den else None


async def player_metrics(
    pool: asyncpg.Pool, game: str, tournament_id: int | None = None
) -> list[dict]:
    rows = await pool.fetch(METRICS_SQL, game, tournament_id)
    out = []
    for r in rows:
        prompt_tokens = r["input_tokens"] + r["cache_read_tokens"] + r["cache_write_tokens"]
        out.append(
            {
                "player_id": r["player_id"],
                "turns": r["turns"],
                "attempts": r["attempts"],
                "illegal_attempts": r["illegal_attempts"],
                "illegal_move_rate": _ratio(r["illegal_attempts"], r["attempts"]),
                "forfeits": r["forfeits"],
                "latency_p50_ms": round(r["latency_p50_ms"], 1),
                "latency_p95_ms": round(r["latency_p95_ms"], 1),
                "completed_games": r["completed_games"],
                "cost_per_game_usd": _ratio(r["completed_cost_usd"], r["completed_games"], 6),
                "cache_hit_rate": _ratio(r["cache_read_tokens"], prompt_tokens),
            }
        )
    return out
