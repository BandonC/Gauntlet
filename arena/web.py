"""Server-rendered HTML pages (Jinja2). Each page builder takes a pool and a `Urls` and
returns an HTML string, so FastAPI serves them and `arena export` can render the same
templates to static files with different link targets.

Data comes from the same functions as the JSON API (store/read.py, the leaderboard).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg
from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape
from markupsafe import Markup

from arena.games.registry import list_games
from arena.store import metrics, ratings, read
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


GAME_NAMES = {
    "connect4": "Connect 4",
    "liars_dice": "Liar's Dice",
    "werewolf": "Werewolf",
    "werewolf_dayfirst": "Werewolf (day-first)",
}
BLIND_GAMES = ("werewolf", "werewolf_dayfirst")
GAME_BLURBS = {
    "connect4": "Four in a row. Simple rules, and the first move decides most games.",
    "liars_dice": "Hidden dice and bold bids. A short probability bot sets the bar.",
    "werewolf": "Seven seats, two wolves, a night kill to open. Bluffing in plain text.",
    "werewolf_dayfirst": "The same game opening by day, with everyone still alive.",
}


def _is_baseline(row: dict) -> bool:
    return re.fullmatch(r"random\d*", row["player_id"]) is not None


def contenders(rows: list[dict]) -> list[dict]:
    """The real field. If only baselines have played, they're all there is to show."""
    return [r for r in rows if not _is_baseline(r)] or rows


def baselines(rows: list[dict]) -> list[dict]:
    field = contenders(rows)
    return [r for r in rows if r not in field]


def game_blurb(game: str) -> str:
    return GAME_BLURBS.get(game, "")


def game_name(game: str) -> str:
    return GAME_NAMES.get(game, game)


def match_result(m: dict) -> str | None:
    """Who won, in words: a match's scores alone mean little to a reader."""
    scores = m.get("scores")
    if m.get("status") != "completed" or not scores or not m.get("seats"):
        return None
    if m["game"] in BLIND_GAMES:
        return "Wolves" if sum(scores) == 2 else "Village"
    best = max(scores)
    winners = [m["seats"][seat] for seat, score in enumerate(scores) if score == best]
    return f"{winners[0]} won" if len(winners) == 1 else "Draw"


def result_class(m: dict) -> str:
    """The side's colour class in Werewolf. Other games name a player, so they get none."""
    word = match_result(m)
    return word.lower() if word and m["game"] in BLIND_GAMES else ""


def result_cells(matches: list[dict]) -> list[dict]:
    """One cell per finished Werewolf match, wolves first and oldest first within a side."""
    cells = [
        {"id": m["id"], "word": match_result(m), "side": result_class(m)}
        for m in matches
        if result_class(m)
    ]
    return sorted(cells, key=lambda c: (c["side"] != "wolves", c["id"]))


def result_counts(matches: list[dict]) -> list[tuple[str, int]]:
    return Counter(c["word"] for c in result_cells(matches)).most_common()


def compact(n: int) -> str:
    """7700217 reads as 7.7M: a headline number doesn't need its last digits."""
    for limit, suffix in ((1_000_000, "M"), (1_000, "K")):
        if n >= limit:
            return f"{n / limit:.1f}{suffix}"
    return str(n)


def result_tally(matches: list[dict]) -> str | None:
    """Wolves 23, Village 17. Only Werewolf: elsewhere the leaderboard says this per player."""
    return ", ".join(f"{side} {n}" for side, n in result_counts(matches)) or None


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
    "werewolf_dayfirst": _werewolf_move,
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
env.filters["game_name"] = game_name
env.filters["match_result"] = match_result
env.filters["result_tally"] = result_tally
env.filters["result_class"] = result_class
env.filters["result_cells"] = result_cells
env.filters["result_counts"] = result_counts
env.filters["compact"] = compact
env.filters["game_blurb"] = game_blurb
env.filters["contenders"] = contenders
env.filters["baselines"] = baselines
# Inlined into the match page (not {% include %}d: JS braces would clash with Jinja), so
# the page works as a single file in a static export. Tests load it in Node directly.
REPLAY_JS = Path(__file__).parent / "templates" / "replay.js"
env.globals["replay_js"] = Markup(REPLAY_JS.read_text(encoding="utf-8"))
SITE_CSS = Path(__file__).parent / "templates" / "site.css"
env.globals["site_css"] = Markup(SITE_CSS.read_text(encoding="utf-8"))


def render(template: str, urls: Urls, **context: object) -> str:
    return env.get_template(template).render(url=urls, **context)


async def index_page(pool: asyncpg.Pool, urls: Urls) -> str:
    boards = {g: await leaderboard(pool, g) for g in list_games()}
    role_stats = await metrics.werewolf_metrics(pool)
    finished = [
        urls("match", id=m["id"])
        for m in await read.list_matches(pool, limit=500)
        if m["game"] in BLIND_GAMES and m["status"] == "completed"
    ]
    blind_url = next((f"{href}#blind" for href in finished if href), None)
    return render(
        "index.html",
        urls,
        boards=boards,
        role_stats=role_stats,
        blind_url=blind_url,
        totals=await read.totals(pool),
        stories=await _rule_change_stories(pool),
    )


async def _rule_change_stories(pool: asyncpg.Pool) -> list[dict]:
    """Each Werewolf ruleset's biggest tournament, to set their results side by side."""
    stories = []
    tournaments = await read.list_tournaments(pool)
    for game in BLIND_GAMES:
        played = [t for t in tournaments if t["game"] == game and t["completed"] > 1]
        if played:
            biggest = max(played, key=lambda t: t["completed"])
            matches = await read.list_matches(pool, biggest["id"], limit=500)
            stories.append({"game": game, "matches": matches})
    return stories


async def tournaments_page(
    pool: asyncpg.Pool, urls: Urls, only: Collection[int] | None = None
) -> str:
    listed = await read.list_tournaments(pool)
    return render(
        "tournaments.html",
        urls,
        tournaments=[t for t in listed if only is None or t["id"] in only],
    )


async def tournament_page(pool: asyncpg.Pool, urls: Urls, tournament_id: int) -> str | None:
    found = await read.list_tournaments(pool, tournament_id)
    if not found:
        return None
    matches = await read.list_matches(pool, tournament_id, limit=500)
    final = await ratings.final_ratings(pool, tournament_id)
    return render("tournament.html", urls, t=found[0], matches=matches, final=final)


async def matches_page(pool: asyncpg.Pool, urls: Urls, only: Collection[int] | None = None) -> str:
    listed = await read.list_matches(pool, limit=50 if only is None else 500)
    return render(
        "matches.html", urls, matches=[m for m in listed if only is None or m["id"] in only]
    )


async def match_page(pool: asyncpg.Pool, urls: Urls, match_id: int) -> str | None:
    m = await read.match_detail(pool, match_id)
    return None if m is None else match_html(urls, m)


def match_html(urls: Urls, m: dict) -> str:
    """The match page from an already-loaded `read.match_detail` (export reuses it for
    the JSON file instead of replaying the log twice)."""
    return render("match.html", urls, m=m)
