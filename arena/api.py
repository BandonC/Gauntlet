"""Read-only HTTP API plus server-rendered pages (Jinja2, see web.py). Run: arena serve

Pages:
  GET /                      leaderboards for every game
  GET /tournaments           GET /tournaments/{id}
  GET /matches               GET /matches/{id}

JSON endpoints:
  GET /api/health
  GET /api/games
  GET /api/tournaments                 progress and spend per tournament
  GET /api/tournaments/{id}/final      order-independent final ratings, or null
  GET /api/leaderboard/{game}
  GET /api/metrics/{game}?tournament_id=   illegal-move rate, forfeits, latency, cost, cache
  GET /api/matches?tournament_id=&limit=
  GET /api/matches/{id}                seats, status, turns, and a spectator frame per turn
  GET /api/matches/{id}/events?after=  Server-Sent Events: each new turn, then done (live.py)
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from arena import live, web
from arena.config import settings
from arena.games.registry import list_games
from arena.store import metrics as m_store
from arena.store import ratings, read
from arena.store import tournaments as t_store
from arena.store.db import create_pool

URLS = web.Urls()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await create_pool(settings().database_url)
    yield
    await app.state.pool.close()


app = FastAPI(title="LLM Arena", lifespan=lifespan)


def _page(html: str | None, what: str) -> HTMLResponse:
    if html is None:
        raise HTTPException(404, f"{what} not found")
    return HTMLResponse(html)


@app.get("/", response_class=HTMLResponse)
async def index_page() -> HTMLResponse:
    return _page(await web.index_page(app.state.pool, URLS), "page")


@app.get("/tournaments", response_class=HTMLResponse)
async def tournaments_page() -> HTMLResponse:
    return _page(await web.tournaments_page(app.state.pool, URLS), "page")


@app.get("/tournaments/{tournament_id}", response_class=HTMLResponse)
async def tournament_page(tournament_id: int) -> HTMLResponse:
    return _page(await web.tournament_page(app.state.pool, URLS, tournament_id), "tournament")


@app.get("/matches", response_class=HTMLResponse)
async def matches_page() -> HTMLResponse:
    return _page(await web.matches_page(app.state.pool, URLS), "page")


@app.get("/matches/{match_id}", response_class=HTMLResponse)
async def match_page(match_id: int) -> HTMLResponse:
    return _page(await web.match_page(app.state.pool, URLS, match_id), "match")


@app.get("/api/health")
async def health() -> dict:
    await app.state.pool.fetchval("SELECT 1")
    return {"ok": True}


@app.get("/api/games")
async def games() -> list[str]:
    return list_games()


@app.get("/api/tournaments")
async def tournaments() -> list[dict]:
    return await read.list_tournaments(app.state.pool)


@app.get("/api/tournaments/{tournament_id}/final")
async def tournament_final(tournament_id: int) -> dict | None:
    return await ratings.final_ratings(app.state.pool, tournament_id)


@app.get("/api/leaderboard/{game}")
async def leaderboard(game: str) -> list[dict]:
    return await t_store.leaderboard(app.state.pool, game)


@app.get("/api/metrics/{game}")
async def metrics(game: str, tournament_id: int | None = None) -> list[dict]:
    return await m_store.player_metrics(app.state.pool, game, tournament_id)


@app.get("/api/werewolf/roles")
async def werewolf_roles(tournament_id: int | None = None) -> list[dict]:
    return await m_store.werewolf_metrics(app.state.pool, tournament_id)


@app.get("/api/matches")
async def matches(tournament_id: int | None = None, limit: int = 50) -> list[dict]:
    return await read.list_matches(app.state.pool, tournament_id, limit)


@app.get("/api/matches/{match_id}")
async def match_detail(match_id: int) -> dict:
    m = await read.match_detail(app.state.pool, match_id)
    if m is None:
        raise HTTPException(404, "match not found")
    return m


@app.get("/api/matches/{match_id}/events")
async def match_events(match_id: int, request: Request, after: int = 0) -> StreamingResponse:
    if await read.match_progress(app.state.pool, match_id) is None:
        raise HTTPException(404, "match not found")
    start = live.resume_point(after, request.headers.get("last-event-id"))
    return StreamingResponse(
        live.match_events(app.state.pool, match_id, start),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
