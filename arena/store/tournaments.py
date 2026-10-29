"""Creating tournaments and reading results from Postgres."""

from __future__ import annotations

import random

import asyncpg

from arena.ratings.elo import EloTable
from arena.roster import PlayerConfig, config_hash
from arena.tournaments import round_robin

UPSERT_PLAYER_SQL = """
INSERT INTO players (id, provider, model, prompt_key, config, config_hash)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (id) DO NOTHING
RETURNING config_hash
"""


async def upsert_players(conn: asyncpg.Connection, players: list[PlayerConfig]) -> None:
    for p in players:
        h = config_hash(p)
        inserted = await conn.fetchval(
            UPSERT_PLAYER_SQL,
            p.id,
            p.provider,
            p.model,
            p.prompt,
            {"temperature": p.temperature, "max_tokens": p.max_tokens, "seed": p.seed},
            h,
        )
        if inserted is None:
            existing = await conn.fetchval("SELECT config_hash FROM players WHERE id = $1", p.id)
            if existing != h:
                raise ValueError(
                    f"player {p.id!r} already exists with a different config "
                    "(model/prompt/settings changed). Give the new config a new id."
                )


async def create_round_robin(
    pool: asyncpg.Pool,
    game: str,
    players: list[PlayerConfig],
    games_per_pair: int,
    budget_usd: float | None,
    seed: int | None = None,
) -> tuple[int, int]:
    """Returns (tournament_id, number_of_matches)."""
    rng = random.Random(seed)
    seatings = round_robin([p.id for p in players], games_per_pair)
    async with pool.acquire() as conn, conn.transaction():
        await upsert_players(conn, players)
        tid = await conn.fetchval(
            "INSERT INTO tournaments (game, format, budget_usd) "
            "VALUES ($1, 'round_robin', $2) RETURNING id",
            game,
            budget_usd,
        )
        for seats in seatings:
            mid = await conn.fetchval(
                "INSERT INTO matches (tournament_id, game, seed) VALUES ($1, $2, $3) RETURNING id",
                tid,
                game,
                rng.randrange(2**31),
            )
            await conn.executemany(
                "INSERT INTO match_seats (match_id, seat, player_id) VALUES ($1, $2, $3)",
                [(mid, i, pid) for i, pid in enumerate(seats)],
            )
    return tid, len(seatings)


async def leaderboard(pool: asyncpg.Pool, game: str) -> list[dict]:
    """v1: Elo recomputed from completed 2-player matches in completion order.
    TODO(phase 3): materialise into ratings/rating_history on match completion."""
    rows = await pool.fetch(
        """
        SELECT m.id, m.scores, array_agg(s.player_id ORDER BY s.seat) AS seats
        FROM matches m JOIN match_seats s ON s.match_id = m.id
        WHERE m.game = $1 AND m.status = 'completed'
        GROUP BY m.id ORDER BY m.completed_at, m.id
        """,
        game,
    )
    table = EloTable()
    wins: dict[str, float] = {}
    for r in rows:
        a, b = r["seats"]
        score_a = r["scores"][0]
        table.record(a, b, score_a)
        wins[a] = wins.get(a, 0) + score_a
        wins[b] = wins.get(b, 0) + r["scores"][1]
    return [
        {"player_id": p, "rating": round(rating, 1), "games": n, "points": wins.get(p, 0)}
        for p, rating, n in table.leaderboard()
    ]
