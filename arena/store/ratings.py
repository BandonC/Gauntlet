"""Live ratings, materialised per match, plus final ratings computed per tournament.

Weng-Lin (wl_* columns) updates on every match at any table size and ranks the
leaderboard; Elo (column `rating`, the v1 rating) and Glicko-2 (glicko_* columns) are
heads-up-only extras beside it. Written in the same transaction
as queue.complete, so a match is rated exactly once and only if its completion commits.

Concurrent completions that share a player serialise on that player's `ratings` row
(`FOR UPDATE`). Rows are created and locked in player_id order so two transactions
never wait on each other in opposite orders.

`rebuild` replays completed matches ordered by (completed_at, id). queue.complete
re-stamps completed_at with clock_timestamp() while holding these row locks, so that
order is exactly the order the live updates were applied in.
"""

from __future__ import annotations

import asyncpg

from arena.games.registry import get_game
from arena.ratings import bradley_terry, elo, glicko2, weng_lin
from arena.ratings.glicko2 import Glicko
from arena.ratings.weng_lin import WengLin

# A cancelled tournament keeps its data, but its results count for nothing. Use with the
# match aliased `m`, in anything that ranks or totals results.
NOT_CANCELLED_SQL = """NOT EXISTS (
    SELECT 1 FROM tournaments c WHERE c.id = m.tournament_id AND c.status = 'cancelled')"""

ENSURE_RATING_SQL = """
INSERT INTO ratings (player_id, game, rating) VALUES ($1, $2, $3::double precision)
ON CONFLICT (player_id, game) DO NOTHING
"""

LOCK_RATINGS_SQL = """
SELECT player_id, rating, glicko_rating, glicko_rd, glicko_vol, wl_mu, wl_sigma FROM ratings
WHERE game = $1 AND player_id = ANY($2::text[])
ORDER BY player_id
FOR UPDATE
"""

UPDATE_RATING_SQL = """
UPDATE ratings
SET rating = $3, glicko_rating = $4, glicko_rd = $5, glicko_vol = $6,
    wl_mu = $7, wl_sigma = $8, games_played = games_played + 1, updated_at = now()
WHERE player_id = $1 AND game = $2
"""

# UNIQUE (player_id, match_id) makes a second rating of the same match raise, which
# rolls back the whole completion transaction.
INSERT_HISTORY_SQL = """
INSERT INTO rating_history (
    player_id, game, match_id, rating_before, rating_after,
    glicko_rating_before, glicko_rating_after, glicko_rd_before, glicko_rd_after,
    wl_mu_before, wl_mu_after, wl_sigma_before, wl_sigma_after
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
"""

# Ranked by the ordinal mu - 3*sigma, a conservative estimate: a player needs games, not
# just a lucky win, to reach the top.
LEADERBOARD_SQL = f"""
SELECT r.player_id, r.rating AS elo, r.glicko_rating, r.glicko_rd, r.wl_mu, r.wl_sigma,
       r.games_played,
       COALESCE((
           SELECT sum((m.scores ->> s.seat::int)::float)
           FROM match_seats s JOIN matches m ON m.id = s.match_id
           WHERE s.player_id = r.player_id AND m.game = r.game AND m.status = 'completed'
             AND {NOT_CANCELLED_SQL}
       ), 0) AS points
FROM ratings r
WHERE r.game = $1
ORDER BY r.wl_mu - 3 * r.wl_sigma DESC, r.player_id
"""

REPLAY_SQL = f"""
SELECT m.id, m.game, m.scores, m.placements,
       array_agg(s.player_id ORDER BY s.seat) AS seats
FROM matches m JOIN match_seats s ON s.match_id = m.id
WHERE m.status = 'completed' AND ($1::text IS NULL OR m.game = $1)
  AND {NOT_CANCELLED_SQL}
GROUP BY m.id
ORDER BY m.completed_at, m.id
"""


FINAL_RESULTS_SQL = """
SELECT m.placements, array_agg(s.player_id ORDER BY s.seat) AS seats
FROM matches m JOIN match_seats s ON s.match_id = m.id
WHERE m.tournament_id = $1 AND m.status = 'completed'
GROUP BY m.id
HAVING count(*) = 2
"""

# The newest finished tournament of a game that was entirely heads-up.
LATEST_FINAL_SQL = """
SELECT max(t.id) FROM tournaments t
WHERE t.game = $1 AND t.status = 'completed'
  AND EXISTS (SELECT 1 FROM matches m WHERE m.tournament_id = t.id)
  AND NOT EXISTS (
      SELECT 1 FROM matches m
      WHERE m.tournament_id = t.id
        AND (SELECT count(*) FROM match_seats s WHERE s.match_id = m.id) <> 2
  )
"""


def seat_zero_outcome(placements: list[int]) -> float:
    """1 if seat 0 finished ahead, 0 if behind, 0.5 level. From placements, never scores:
    Split the Deal scores aren't zero-sum, so scores[0] doesn't say who won."""
    a, b = placements
    return 1.0 if a < b else 0.0 if a > b else 0.5


def _rate_seats(
    game: str, seats: list[str], placements: list[int], old: dict[str, WengLin]
) -> dict[str, WengLin]:
    teams = get_game(game).teams(len(seats))
    if teams is None:
        rated = weng_lin.rate([old[pid] for pid in seats], placements)
        return dict(zip(seats, rated, strict=True))
    groups = [[old[seats[seat]] for seat in team] for team in teams]
    new_teams = weng_lin.rate_teams(groups, [placements[team[0]] for team in teams])
    return {
        seats[seat]: rating
        for team, ratings_of_team in zip(teams, new_teams, strict=True)
        for seat, rating in zip(team, ratings_of_team, strict=True)
    }


async def apply_match_result(
    conn: asyncpg.Connection,
    match_id: int,
    game: str,
    seats: list[str],
    scores: list[float],
    placements: list[int],
) -> bool:
    """Update ratings + rating_history for a completed match. Call inside a transaction.
    Returns True if the match was rated (the player rows are then locked until commit).

    Weng-Lin updates at every table size. Elo and Glicko-2 are heads-up only: at other
    table sizes their columns stay put, so those history rows show before == after.
    A player seated twice is never rated (rating_history allows one row per match).
    """
    if len(seats) < 2 or len(set(seats)) != len(seats):
        return False
    for pid in sorted(seats):
        await conn.execute(ENSURE_RATING_SQL, pid, game, elo.DEFAULT_RATING)
    rows = {r["player_id"]: r for r in await conn.fetch(LOCK_RATINGS_SQL, game, sorted(seats))}
    old_elo = {pid: r["rating"] for pid, r in rows.items()}
    old_gl = {
        pid: Glicko(r["glicko_rating"], r["glicko_rd"], r["glicko_vol"]) for pid, r in rows.items()
    }
    old_wl = {pid: WengLin(r["wl_mu"], r["wl_sigma"]) for pid, r in rows.items()}
    new_elo, new_gl = dict(old_elo), dict(old_gl)
    if len(seats) == 2:
        a, b = seats
        outcome = seat_zero_outcome(placements)
        new_elo[a], new_elo[b] = elo.update(old_elo[a], old_elo[b], outcome)
        new_gl[a], new_gl[b] = glicko2.update_pair(old_gl[a], old_gl[b], outcome)
    new_wl = _rate_seats(game, seats, placements, old_wl)
    for pid in sorted(seats):
        gl, wl = new_gl[pid], new_wl[pid]
        await conn.execute(
            UPDATE_RATING_SQL, pid, game, new_elo[pid], gl.rating, gl.rd, gl.vol, wl.mu, wl.sigma
        )
        await conn.execute(
            INSERT_HISTORY_SQL,
            pid,
            game,
            match_id,
            old_elo[pid],
            new_elo[pid],
            old_gl[pid].rating,
            gl.rating,
            old_gl[pid].rd,
            gl.rd,
            old_wl[pid].mu,
            wl.mu,
            old_wl[pid].sigma,
            wl.sigma,
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
            await apply_match_result(
                conn, m["id"], m["game"], list(m["seats"]), m["scores"], m["placements"]
            )
    return len(matches)


async def final_ratings(pool: asyncpg.Pool, tournament_id: int) -> dict[str, dict] | None:
    """Bradley-Terry ratings of one finished tournament, best first. None while it's
    unfinished, if it wasn't heads-up, or if no finite estimate exists."""
    status = await pool.fetchval("SELECT status FROM tournaments WHERE id = $1", tournament_id)
    if status != "completed":
        return None
    rows = await pool.fetch(FINAL_RESULTS_SQL, tournament_id)
    fitted = bradley_terry.fit(
        (r["seats"][0], r["seats"][1], seat_zero_outcome(r["placements"]))
        for r in rows
        if len(set(r["seats"])) == 2
    )
    if fitted is None:
        return None
    best_first = sorted(fitted.items(), key=lambda kv: (-kv[1].rating, kv[0]))
    return {
        pid: {"rating": round(r.rating, 1), "low": round(r.low, 1), "high": round(r.high, 1)}
        for pid, r in best_first
    }


async def leaderboard(pool: asyncpg.Pool, game: str) -> list[dict]:
    """Live ratings for every player, plus each one's final rating from the game's
    latest heads-up tournament (None if it didn't play there, or none exists). Finals
    only compare within one tournament, so they come from that single fit."""
    rows = await pool.fetch(LEADERBOARD_SQL, game)
    final_id = await pool.fetchval(LATEST_FINAL_SQL, game)
    finals = await final_ratings(pool, final_id) if final_id else None
    return [
        {
            "player_id": r["player_id"],
            "rating": round(r["glicko_rating"], 1),
            "rd": round(r["glicko_rd"], 1),
            "low": round(r["glicko_rating"] - 2 * r["glicko_rd"], 1),
            "high": round(r["glicko_rating"] + 2 * r["glicko_rd"], 1),
            "ordinal": round(r["wl_mu"] - weng_lin.ORDINAL_Z * r["wl_sigma"], 2),
            "mu": round(r["wl_mu"], 2),
            "sigma": round(r["wl_sigma"], 2),
            "elo": round(r["elo"], 1),
            "games": r["games_played"],
            "points": r["points"],
            "final": (finals or {}).get(r["player_id"]),
            "final_tournament_id": final_id if finals else None,
        }
        for r in rows
    ]
