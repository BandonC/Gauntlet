"""Server-rendered HTML pages (Jinja2). Each page builder takes a pool and a `Urls` and
returns an HTML string, so FastAPI serves them and `arena export` can render the same
templates to static files with different link targets.

Data comes from the same functions as the JSON API (store/read.py, the leaderboard).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import asyncpg
from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from arena.games.registry import list_games
from arena.store import read
from arena.store.ratings import leaderboard

SERVER_ROUTES = {
    "index": "/",
    "tournaments": "/tournaments",
    "tournament": "/tournaments/{id}",
    "matches": "/matches",
    "match": "/matches/{id}",
}


@dataclass(frozen=True)
class Urls:
    """Link targets for the templates: `url("match", id=3)`. The server uses
    SERVER_ROUTES; a static export passes file paths instead."""

    routes: dict[str, str] = field(default_factory=lambda: dict(SERVER_ROUTES))

    def __call__(self, name: str, **params: object) -> str:
        return self.routes[name].format(**params)


def _usd(value: float | None, digits: int = 4) -> str:
    return "" if value is None else f"${value:.{digits}f}"


def _action(action: object) -> str:
    return json.dumps(action, separators=(", ", ": ")) if action is not None else ""


env = Environment(
    loader=PackageLoader("arena", "templates"),
    autoescape=select_autoescape(["html"]),
    undefined=StrictUndefined,  # a typo in a template fails loudly in tests
    trim_blocks=True,
    lstrip_blocks=True,
)
env.filters["usd"] = _usd
env.filters["action"] = _action


def render(template: str, urls: Urls, **context: object) -> str:
    return env.get_template(template).render(url=urls, **context)


async def index_page(pool: asyncpg.Pool, urls: Urls) -> str:
    boards = {g: await leaderboard(pool, g) for g in list_games()}
    return render("index.html", urls, boards=boards)


async def tournaments_page(pool: asyncpg.Pool, urls: Urls) -> str:
    return render("tournaments.html", urls, tournaments=await read.list_tournaments(pool))


async def tournament_page(pool: asyncpg.Pool, urls: Urls, tournament_id: int) -> str | None:
    found = await read.list_tournaments(pool, tournament_id)
    if not found:
        return None
    matches = await read.list_matches(pool, tournament_id, limit=500)
    return render("tournament.html", urls, t=found[0], matches=matches)


async def matches_page(pool: asyncpg.Pool, urls: Urls) -> str:
    return render("matches.html", urls, matches=await read.list_matches(pool, limit=50))


async def match_page(pool: asyncpg.Pool, urls: Urls, match_id: int) -> str | None:
    m = await read.match_detail(pool, match_id)
    return None if m is None else render("match.html", urls, m=m)
