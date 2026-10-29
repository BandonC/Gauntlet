"""Materialised ratings: Elo (the v1 rating, column `rating`) and Glicko-2 beside it
(glicko_* columns, post-v1). Written in the same transaction as queue.complete, so a
match is rated exactly once and only if its completion commits.

Concurrent completions that share a player serialise on that player's `ratings` row
(`FOR UPDATE`). Rows are created and locked in player_id order so two transactions
never wait on each other in opposite orders.

`rebuild` replays completed matches ordered by (completed_at, id). queue.complete
re-stamps completed_at with clock_timestamp() while holding these row locks, so that
order is exactly the order the live updates were applied in.
"""

from __future__ import annotations

import asyncpg

from arena.ratings import elo, glicko2
from arena.ratings.glicko2 import Glicko

ENSURE_RATING_SQL = """
INSERT INTO ratings (player_id, game, rating) VALUES ($1, $2, $3::double precision)
ON CONFLICT (player_id, game) DO NOTHING
"""

LOCK_RATINGS_SQL = """
SELECT player_id, rating, glicko_rating, glicko_rd, glicko_vol FROM ratings
WHERE game = $1 AND player_id = ANY($2::text[])
ORDER BY player_id
FOR UPDATE
"""

UPDATE_RATING_SQL = """
UPDATE ratings
SET rating = $3, glicko_rating = $4, glicko_rd = $5, glicko_vol = $6,
    games_played = games_played + 1, updated_at = now()
WHERE player_id = $1 AND game = $2
"""

# UNIQUE (player_id, match_id) makes a second rating of the same match raise, which
# rolls back the whole completion transaction.
INSERT_HISTORY_SQL = """
INSERT INTO rating_history (
    player_id, game, match_id, rating_before, rating_after,
    glicko_rating_before, glicko_rating_after, glicko_rd_before, glicko_rd_after
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
"""

# Ranked by the conservative estimate rating - 2*RD: a player needs games, not just a
# lucky win, to reach the top.
LEADERBOARD_SQL = """
SELECT r.player_id, r.rating AS elo, r.glicko_rating, r.glicko_rd, r.games_played,
       COALESCE((
           SELECT sum((m.scores ->> s.seat::int)::float)
           FROM match_seats s JOIN matches m ON m.id = s.match_id
           WHERE s.player_id = r.player_id AND m.game = r.game AND m.status = 'completed'
       ), 0) AS points
FROM ratings r
WHERE r.game = $1
ORDER BY r.glicko_rating - 2 * r.glicko_rd DESC, r.player_id
"""

REPLAY_SQL = """
SELECT m.id, m.game, m.scores, array_agg(s.player_id ORDER BY s.seat) AS seats
FROM matches m JOIN match_seats s ON s.match_id = m.id
WHERE m.status = 'completed' AND ($1::text IS NULL OR m.game = $1)
GROUP BY m.id
ORDER BY m.completed_at, m.id
"""


async def apply_match_result(
    conn: asyncpg.Connection, match_id: int, game: str, seats: list[str], scores: list[float]
) -> bool:
    """Update ratings + rating_history for a completed match. Call inside a transaction.
    Returns True if the match was rated (the player rows are then locked until commit).

    Both systems are two-player only: matches with any other seat count are left
    unrated until a multi-player rating system exists.
    """
    if len(seats) != 2 or seats[0] == seats[1]:
        return False
    a, b = seats
    for pid in sorted(seats):
        await conn.execute(ENSURE_RATING_SQL, pid, game, elo.DEFAULT_RATING)
    rows = {r["player_id"]: r for r in await conn.fetch(LOCK_RATINGS_SQL, game, sorted(seats))}
    before = {
        pid: (r["rating"], Glicko(r["glicko_rating"], r["glicko_rd"], r["glicko_vol"]))
        for pid, r in rows.items()
    }
    elo_a, elo_b = elo.update(before[a][0], before[b][0], scores[0])
    gl_a, gl_b = glicko2.update_pair(before[a][1], before[b][1], scores[0])
    for pid, new_elo, new_gl in ((a, elo_a, gl_a), (b, elo_b, gl_b)):
        old_elo, old_gl = before[pid]
        await conn.execute(
            UPDATE_RATING_SQL, pid, game, new_elo, new_gl.rating, new_gl.rd, new_gl.vol
        )
        await conn.execute(
            INSERT_HISTORY_SQL,
            pid,
            game,
            match_id,
            old_elo,
            new_elo,
            old_gl.rating,
            new_gl.rating,
            old_gl.rd,
            new_gl.rd,
        )
    return True


async def rebuild(pool: asyncpg.Pool, game: str | None = None) -> int:
    """Recompute ratings + rating_history from completed matches, ordered by
    (completed_at, id). Blocks completions while it runs. Returns matches replayed."""
    async with pool.acquire() as conn, conn.transaction():
        # EXCLUSIVE conflicts with the row locks and inserts completions need.
        await conn.execute("LOCK TABLE ratings IN EXCLUSIVE MODE")
        await conn.execute("DELETE FROM rating_history WHERE $1::text IS NULL OR game = $1", game)
        await conn.execute("DELETE FROM ratings WHERE $1::text IS NULL OR game = $1", game)
        matches = await conn.fetch(REPLAY_SQL, game)
        for m in matches:
            await apply_match_result(conn, m["id"], m["game"], list(m["seats"]), m["scores"])
    return len(matches)


async def leaderboard(pool: asyncpg.Pool, game: str) -> list[dict]:
    rows = await pool.fetch(LEADERBOARD_SQL, game)
    return [
        {
            "player_id": r["player_id"],
            "rating": round(r["glicko_rating"], 1),
            "rd": round(r["glicko_rd"], 1),
            "low": round(r["glicko_rating"] - 2 * r["glicko_rd"], 1),
            "high": round(r["glicko_rating"] + 2 * r["glicko_rd"], 1),
            "elo": round(r["elo"], 1),
            "games": r["games_played"],
            "points": r["points"],
        }
        for r in rows
    ]
