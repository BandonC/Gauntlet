"""Read-only HTTP API plus a minimal UI page. Run: arena serve

Endpoints:
  GET /api/health
  GET /api/games
  GET /api/tournaments                 progress and spend per tournament
  GET /api/leaderboard/{game}
  GET /api/matches?tournament_id=&limit=
  GET /api/matches/{id}                seats, status, turns, and a spectator frame per turn
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from arena.config import settings
from arena.engine.match_runner import MatchSpec, iter_states
from arena.games.registry import get_game, list_games
from arena.store import tournaments as t_store
from arena.store.db import create_pool
from arena.store.turns import PostgresTurnStore

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await create_pool(settings().database_url)
    yield
    await app.state.pool.close()


app = FastAPI(title="LLM Arena", lifespan=lifespan)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
async def health() -> dict:
    await app.state.pool.fetchval("SELECT 1")
    return {"ok": True}


@app.get("/api/games")
async def games() -> list[str]:
    return list_games()


@app.get("/api/tournaments")
async def tournaments() -> list[dict]:
    rows = await app.state.pool.fetch(
        """
        SELECT t.id, t.game, t.format, t.status, t.budget_usd::float, t.spent_usd::float,
               count(m.*) AS total,
               count(m.*) FILTER (WHERE m.status = 'completed') AS completed,
               count(m.*) FILTER (WHERE m.status = 'failed') AS failed
        FROM tournaments t LEFT JOIN matches m ON m.tournament_id = t.id
        GROUP BY t.id ORDER BY t.id DESC
        """
    )
    return [dict(r) for r in rows]


@app.get("/api/leaderboard/{game}")
async def leaderboard(game: str) -> list[dict]:
    return await t_store.leaderboard(app.state.pool, game)


@app.get("/api/matches")
async def matches(tournament_id: int | None = None, limit: int = 50) -> list[dict]:
    rows = await app.state.pool.fetch(
        """
        SELECT m.id, m.tournament_id, m.game, m.status, m.scores, m.run_attempts,
               array_agg(s.player_id ORDER BY s.seat) AS seats,
               (SELECT coalesce(sum(cost_usd), 0)::float
                FROM turns WHERE match_id = m.id) AS cost_usd
        FROM matches m JOIN match_seats s ON s.match_id = m.id
        WHERE $1::bigint IS NULL OR m.tournament_id = $1
        GROUP BY m.id ORDER BY m.id DESC LIMIT $2
        """,
        tournament_id,
        min(limit, 500),
    )
    return [dict(r) for r in rows]


@app.get("/api/matches/{match_id}")
async def match_detail(match_id: int) -> dict:
    pool = app.state.pool
    m = await pool.fetchrow("SELECT * FROM matches WHERE id = $1", match_id)
    if m is None:
        raise HTTPException(404, "match not found")
    seats = [
        r["player_id"]
        for r in await pool.fetch(
            "SELECT player_id FROM match_seats WHERE match_id = $1 ORDER BY seat", match_id
        )
    ]
    turns = await PostgresTurnStore(pool, worker_id="api").load_turns(str(match_id))

    # Rebuild a spectator frame for every turn by replaying the log through the engine.
    game = get_game(m["game"])
    spec = MatchSpec(str(match_id), m["game"], m["seed"], seats)
    frames = [game.spectator_view(s) for s in iter_states(game, spec, turns)]

    return {
        "id": m["id"],
        "game": m["game"],
        "status": m["status"],
        "scores": m["scores"],
        "last_error": m["last_error"],
        "seats": seats,
        "turns": [
            {
                "turn_no": t.turn_no,
                "seat": t.seat,
                "action": t.action,
                "forfeited": t.forfeited,
                "attempts": t.attempts,
                "error": t.error,
                "cost_usd": t.cost_usd,
                "latency_ms": t.latency_ms,
                "tokens": t.usage.__dict__,
            }
            for t in turns
        ],
        "frames": frames,  # frames[i] = board before turn i; frames[-1] = final
    }
