"""Creating tournaments and reading results from Postgres."""

from __future__ import annotations

import random

import asyncpg

from arena.roster import PlayerConfig, config_hash
from arena.store import ratings
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
    """Reads the materialised ratings table (written by queue.complete)."""
    return await ratings.leaderboard(pool, game)
