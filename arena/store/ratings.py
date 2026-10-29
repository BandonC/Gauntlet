"""Materialised Elo ratings. Written in the same transaction as queue.complete, so a
match is rated exactly once and only if its completion commits.

Concurrent completions that share a player serialise on that player's `ratings` row
(`FOR UPDATE`). Rows are created and locked in player_id order so two transactions
never wait on each other in opposite orders.
"""

from __future__ import annotations

import asyncpg

from arena.ratings.elo import DEFAULT_RATING, update

ENSURE_RATING_SQL = """
INSERT INTO ratings (player_id, game, rating) VALUES ($1, $2, $3::double precision)
ON CONFLICT (player_id, game) DO NOTHING
"""

LOCK_RATINGS_SQL = """
SELECT player_id, rating FROM ratings
WHERE game = $1 AND player_id = ANY($2::text[])
ORDER BY player_id
FOR UPDATE
"""

UPDATE_RATING_SQL = """
UPDATE ratings
SET rating = $3, games_played = games_played + 1, updated_at = now()
WHERE player_id = $1 AND game = $2
"""

# UNIQUE (player_id, match_id) makes a second rating of the same match raise, which
# rolls back the whole completion transaction.
INSERT_HISTORY_SQL = """
INSERT INTO rating_history (player_id, game, match_id, rating_before, rating_after)
VALUES ($1, $2, $3, $4, $5)
"""

LEADERBOARD_SQL = """
SELECT r.player_id, r.rating, r.games_played,
       COALESCE((
           SELECT sum((m.scores ->> s.seat::int)::float)
           FROM match_seats s JOIN matches m ON m.id = s.match_id
           WHERE s.player_id = r.player_id AND m.game = r.game AND m.status = 'completed'
       ), 0) AS points
FROM ratings r
WHERE r.game = $1
ORDER BY r.rating DESC, r.player_id
"""


async def apply_match_result(
    conn: asyncpg.Connection, match_id: int, game: str, seats: list[str], scores: list[float]
) -> None:
    """Update ratings + rating_history for a completed match. Call inside a transaction.

    Elo is two-player only: matches with any other seat count are left unrated until
    a multi-player rating system exists.
    """
    if len(seats) != 2 or seats[0] == seats[1]:
        return
    a, b = seats
    for pid in sorted(seats):
        await conn.execute(ENSURE_RATING_SQL, pid, game, DEFAULT_RATING)
    rows = await conn.fetch(LOCK_RATINGS_SQL, game, sorted(seats))
    before = {r["player_id"]: r["rating"] for r in rows}
    after_a, after_b = update(before[a], before[b], scores[0])
    for pid, new in ((a, after_a), (b, after_b)):
        await conn.execute(UPDATE_RATING_SQL, pid, game, new)
        await conn.execute(INSERT_HISTORY_SQL, pid, game, match_id, before[pid], new)


async def leaderboard(pool: asyncpg.Pool, game: str) -> list[dict]:
    rows = await pool.fetch(LEADERBOARD_SQL, game)
    return [
        {
            "player_id": r["player_id"],
            "rating": round(r["rating"], 1),
            "games": r["games_played"],
            "points": r["points"],
        }
        for r in rows
    ]
