"""Static site export: `arena export --out site/`.

Renders the same page builders as `arena serve` (web.py) to HTML files, plus the JSON
API's data as files, so the result can be browsed from disk or GitHub Pages with no
server. Pages sit flat in the output directory, so every relative link resolves from
every page; JSON mirrors the API paths under `api/`.

Liar's Dice replays show every seat's dice (spectator frames), exactly as the server does.

A partial export (`--tournaments 6,7 --matches 116,142`) writes only those pages. Leaderboards
still cover everything, and whatever was left out shows as plain text instead of a dead link.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import asyncpg

from arena import web
from arena.games.registry import list_games
from arena.store import metrics, ratings, read
from arena.store.ratings import leaderboard

# Flat on purpose: a page in a subdirectory would need "../" in front of every link.
FILE_ROUTES = {
    "index": "index.html",
    "tournaments": "tournaments.html",
    "tournament": "tournament-{id}.html",
    "matches": "matches.html",
    "match": "match-{id}.html",
    "match_events": "",  # no server to stream from: exported pages never go live
}
FILE_URLS = web.Urls(dict(FILE_ROUTES))


@dataclass(frozen=True)
class PartialUrls(web.Urls):
    """File links for a partial export: a page left out gets an empty link."""

    tournaments: frozenset[int] = frozenset()
    matches: frozenset[int] = frozenset()

    def __call__(self, name: str, **params: object) -> str:
        kept = {"tournament": self.tournaments, "match": self.matches}.get(name)
        if kept is not None and params["id"] not in kept:
            return ""
        return super().__call__(name, **params)


@dataclass
class ExportSummary:
    pages: int = 0
    json_files: int = 0


def _json_default(value: object) -> object:
    # asyncpg returns sum()/count() over bigint as Decimal; the API encodes them as numbers.
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


async def export_site(
    pool: asyncpg.Pool,
    out: Path,
    tournaments: Collection[int] | None = None,
    matches: Collection[int] | None = None,
) -> ExportSummary:
    """Write every page and JSON file into `out` (created if missing; existing files with
    the same names are overwritten, nothing else is deleted). Passing `tournaments` or
    `matches` limits that kind of page to those ids; None means all of them."""
    summary = ExportSummary()
    all_tournaments = await read.list_tournaments(pool)
    all_matches = await read.match_ids(pool)
    t_ids = {t["id"] for t in all_tournaments} if tournaments is None else set(tournaments)
    m_ids = set(all_matches) if matches is None else set(matches)
    unknown = (t_ids - {t["id"] for t in all_tournaments}) | (m_ids - set(all_matches))
    if unknown:
        raise ValueError(f"nothing to export for ids {sorted(unknown)}")
    partial = tournaments is not None or matches is not None
    urls = FILE_URLS
    if partial:
        urls = PartialUrls(dict(FILE_ROUTES), frozenset(t_ids), frozenset(m_ids))
    only_t, only_m = (t_ids, m_ids) if partial else (None, None)

    def page(rel: str, html: str | None) -> None:
        assert html is not None, rel  # ids come from the database, so the page exists
        _write(out / rel, html)
        summary.pages += 1

    def data(rel: str, obj: object) -> None:
        _write(out / "api" / rel, json.dumps(obj, default=_json_default, indent=1) + "\n")
        summary.json_files += 1

    _write(out / ".nojekyll", "")  # GitHub Pages: serve files as-is

    page(FILE_URLS("index"), await web.index_page(pool, urls))
    page(FILE_URLS("tournaments"), await web.tournaments_page(pool, urls, only_t))
    page(FILE_URLS("matches"), await web.matches_page(pool, urls, only_m))

    kept_tournaments = [t for t in all_tournaments if t["id"] in t_ids]
    for t in kept_tournaments:
        html = await web.tournament_page(pool, urls, t["id"])
        page(FILE_URLS("tournament", id=t["id"]), html)
        data(f"tournaments/{t['id']}/final.json", await ratings.final_ratings(pool, t["id"]))
    for match_id in sorted(m_ids):
        detail = await read.match_detail(pool, match_id)
        page(FILE_URLS("match", id=match_id), web.match_html(urls, detail))
        data(f"matches/{match_id}.json", detail)

    games = list_games()
    data("games.json", games)
    data("tournaments.json", kept_tournaments)
    listed = await read.list_matches(pool, limit=500)
    data("matches.json", [m for m in listed if m["id"] in m_ids])
    for g in games:
        data(f"leaderboard/{g}.json", await leaderboard(pool, g))
        data(f"metrics/{g}.json", await metrics.player_metrics(pool, g))
    data("werewolf/roles.json", await metrics.werewolf_metrics(pool))
    return summary


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
