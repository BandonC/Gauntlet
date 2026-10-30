"""Static site export: `arena export --out site/`.

Renders the same page builders as `arena serve` (web.py) to HTML files, plus the JSON
API's data as files, so the result can be browsed from disk or GitHub Pages with no
server. Pages sit flat in the output directory, so every relative link resolves from
every page; JSON mirrors the API paths under `api/`.

Liar's Dice replays show every seat's dice (spectator frames), exactly as the server does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import asyncpg

from arena import web
from arena.games.registry import list_games
from arena.store import metrics, read
from arena.store.ratings import leaderboard

# Flat on purpose: a page in a subdirectory would need "../" in front of every link.
FILE_ROUTES = {
    "index": "index.html",
    "tournaments": "tournaments.html",
    "tournament": "tournament-{id}.html",
    "matches": "matches.html",
    "match": "match-{id}.html",
}
FILE_URLS = web.Urls(dict(FILE_ROUTES))


@dataclass
class ExportSummary:
    pages: int = 0
    json_files: int = 0


def _json_default(value: object) -> object:
    # asyncpg returns sum()/count() over bigint as Decimal; the API encodes them as numbers.
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


async def export_site(pool: asyncpg.Pool, out: Path) -> ExportSummary:
    """Write every page and JSON file into `out` (created if missing; existing files with
    the same names are overwritten, nothing else is deleted)."""
    summary = ExportSummary()

    def page(rel: str, html: str | None) -> None:
        assert html is not None, rel  # ids come from the database, so the page exists
        _write(out / rel, html)
        summary.pages += 1

    def data(rel: str, obj: object) -> None:
        _write(out / "api" / rel, json.dumps(obj, default=_json_default, indent=1) + "\n")
        summary.json_files += 1

    _write(out / ".nojekyll", "")  # GitHub Pages: serve files as-is

    page(FILE_URLS("index"), await web.index_page(pool, FILE_URLS))
    page(FILE_URLS("tournaments"), await web.tournaments_page(pool, FILE_URLS))
    page(FILE_URLS("matches"), await web.matches_page(pool, FILE_URLS))

    tournaments = await read.list_tournaments(pool)
    for t in tournaments:
        html = await web.tournament_page(pool, FILE_URLS, t["id"])
        page(FILE_URLS("tournament", id=t["id"]), html)
    for match_id in await read.match_ids(pool):
        detail = await read.match_detail(pool, match_id)
        page(FILE_URLS("match", id=match_id), web.match_html(FILE_URLS, detail))
        data(f"matches/{match_id}.json", detail)

    games = list_games()
    data("games.json", games)
    data("tournaments.json", tournaments)
    data("matches.json", await read.list_matches(pool, limit=500))
    for g in games:
        data(f"leaderboard/{g}.json", await leaderboard(pool, g))
        data(f"metrics/{g}.json", await metrics.player_metrics(pool, g))
    return summary


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
