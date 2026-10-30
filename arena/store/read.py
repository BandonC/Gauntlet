"""Read queries shared by the JSON API, the HTML pages and `arena export`, so every
surface shows the same data."""

from __future__ import annotations

import asyncpg

from arena.engine.match_runner import MatchSpec, iter_states
from arena.games.registry import get_game
from arena.store.turns import PostgresTurnStore

TOURNAMENTS_SQL = """
SELECT t.id, t.game, t.format, t.status, t.rounds, t.current_round, t.last_error,
       t.budget_usd::float, t.spent_usd::float,
       count(m.*) AS total,
       count(m.*) FILTER (WHERE m.status = 'completed') AS completed,
       count(m.*) FILTER (WHERE m.status = 'failed') AS failed
FROM tournaments t LEFT JOIN matches m ON m.tournament_id = t.id
WHERE $1::bigint IS NULL OR t.id = $1
GROUP BY t.id ORDER BY t.id DESC
"""

MATCHES_SQL = """
SELECT m.id, m.tournament_id, m.round, m.game, m.status, m.scores, m.placements,
       m.run_attempts,
       array_agg(s.player_id ORDER BY s.seat) AS seats,
       (SELECT coalesce(sum(cost_usd), 0)::float
        FROM turns WHERE match_id = m.id) AS cost_usd
FROM matches m JOIN match_seats s ON s.match_id = m.id
WHERE $1::bigint IS NULL OR m.tournament_id = $1
GROUP BY m.id, m.round ORDER BY m.id DESC LIMIT $2
"""


async def list_tournaments(pool: asyncpg.Pool, tournament_id: int | None = None) -> list[dict]:
    """Progress and spend per tournament, newest first (or just one)."""
    return [dict(r) for r in await pool.fetch(TOURNAMENTS_SQL, tournament_id)]


async def list_matches(
    pool: asyncpg.Pool, tournament_id: int | None = None, limit: int = 50
) -> list[dict]:
    return [dict(r) for r in await pool.fetch(MATCHES_SQL, tournament_id, min(limit, 500))]


async def match_ids(pool: asyncpg.Pool) -> list[int]:
    """Every match id, oldest first (the static export writes a page for each)."""
    return [r["id"] for r in await pool.fetch("SELECT id FROM matches ORDER BY id")]


async def match_detail(pool: asyncpg.Pool, match_id: int) -> dict | None:
    """Seats, status, turns, and a spectator frame per turn. None if no such match."""
    m = await pool.fetchrow("SELECT * FROM matches WHERE id = $1", match_id)
    if m is None:
        return None
    seats = [
        r["player_id"]
        for r in await pool.fetch(
            "SELECT player_id FROM match_seats WHERE match_id = $1 ORDER BY seat", match_id
        )
    ]
    turns = await PostgresTurnStore(pool, worker_id="api").load_turns(str(match_id))

    # Rebuild a spectator frame for every turn by replaying the log through the engine.
    game = get_game(m["game"])
    spec = MatchSpec(str(match_id), m["game"], m["seed"], seats, m["options"])
    frames = [game.spectator_view(s) for s in iter_states(game, spec, turns)]

    return {
        "id": m["id"],
        "tournament_id": m["tournament_id"],
        "round": m["round"],
        "game": m["game"],
        "status": m["status"],
        "scores": m["scores"],
        "placements": m["placements"],
        "last_error": m["last_error"],
        "seats": seats,
        "turns": [
            {
                "turn_no": t.turn_no,
                "seat": t.seat,
                "action": t.action,
                "forfeited": t.forfeited,
                "truncated": t.truncated,
                "attempts": t.attempts,
                "error": t.error,
                "cost_usd": t.cost_usd,
                "latency_ms": t.latency_ms,
                "tokens": t.usage.__dict__,
            }
            for t in turns
        ],
        "frames": frames,  # frames[i] = state before turn i; frames[-1] = final
    }
