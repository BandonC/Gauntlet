"""Server-rendered HTML pages (Jinja2). Each page builder takes a pool and a `Urls` and
returns an HTML string, so FastAPI serves them and `arena export` can render the same
templates to static files with different link targets.

Data comes from the same functions as the JSON API (store/read.py, the leaderboard).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg
from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape
from markupsafe import Markup

from arena.games.registry import list_games
from arena.store import metrics, read
from arena.store.ratings import leaderboard

SERVER_ROUTES = {
    "index": "/",
    "tournaments": "/tournaments",
    "tournament": "/tournaments/{id}",
    "matches": "/matches",
    "match": "/matches/{id}",
    "match_events": "/api/matches/{id}/events",
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


def _connect4_move(a: dict) -> str | None:
    return f"column {a['column']}" if set(a) == {"column"} else None


def _liars_dice_move(a: dict) -> str | None:
    if a.get("action") == "challenge":
        return "challenge"
    if a.get("action") == "bid" and {"quantity", "face"} <= set(a):
        return f"bid {a['quantity']} × {a['face']}"
    return None


def _werewolf_move(a: dict) -> str | None:
    if a.get("action") == "say" and "message" in a:
        return f"says: {a['message']}"
    if a.get("action") in ("kill", "inspect", "vote") and "target" in a:
        return f"{a['action']} seat {a['target']}"
    return None


# Short labels for the turn table and the replay info line. Keep in step with the
# game's action schema; anything unexpected falls back to the raw (sorted) JSON.
MOVE_LABELS = {
    "connect4": _connect4_move,
    "liars_dice": _liars_dice_move,
    "werewolf": _werewolf_move,
}


def move_label(game: str, action: object, forfeited: bool = False) -> str:
    if forfeited or action is None:
        return "forfeit"
    fmt = MOVE_LABELS.get(game)
    label = fmt(action) if fmt and isinstance(action, dict) else None
    return label or json.dumps(action, sort_keys=True, separators=(", ", ": "))


def with_labels(turns: list[dict], game: str) -> list[dict]:
    return [t | {"label": move_label(game, t["action"], t["forfeited"])} for t in turns]


env = Environment(
    loader=PackageLoader("arena", "templates"),
    autoescape=select_autoescape(["html"]),
    undefined=StrictUndefined,  # a typo in a template fails loudly in tests
    trim_blocks=True,
    lstrip_blocks=True,
)
env.filters["usd"] = _usd
env.filters["with_labels"] = with_labels
# Inlined into the match page (not {% include %}d: JS braces would clash with Jinja), so
# the page works as a single file in a static export. Tests load it in Node directly.
REPLAY_JS = Path(__file__).parent / "templates" / "replay.js"
env.globals["replay_js"] = Markup(REPLAY_JS.read_text(encoding="utf-8"))


def render(template: str, urls: Urls, **context: object) -> str:
    return env.get_template(template).render(url=urls, **context)


async def index_page(pool: asyncpg.Pool, urls: Urls) -> str:
    boards = {g: await leaderboard(pool, g) for g in list_games()}
    role_stats = await metrics.werewolf_metrics(pool)
    return render("index.html", urls, boards=boards, role_stats=role_stats)


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
    return None if m is None else match_html(urls, m)


def match_html(urls: Urls, m: dict) -> str:
    """The match page from an already-loaded `read.match_detail` (export reuses it for
    the JSON file instead of replaying the log twice)."""
    return render("match.html", urls, m=m)
