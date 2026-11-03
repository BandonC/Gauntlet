"""Server-rendered HTML pages (Jinja2). Each page builder takes a pool and a `Urls` and
returns an HTML string, so FastAPI serves them and `arena export` can render the same
templates to static files with different link targets.

Data comes from the same functions as the JSON API (store/read.py, the leaderboard).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from math import sqrt
from pathlib import Path

import asyncpg
from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape
from markupsafe import Markup

from arena.games.code_break import code_of
from arena.games.registry import get_game, list_games
from arena.games.split_deal import ITEMS
from arena.store import metrics, ratings, read
from arena.store.ratings import leaderboard

SERVER_ROUTES = {
    "index": "/",
    "tournaments": "/tournaments",
    "tournament": "/tournaments/{id}",
    "matches": "/matches",
    "match": "/matches/{id}",
    "match_events": "/api/matches/{id}/events",
    "player": "/players/{id}",
    "compare": "/compare",
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


def pct(value: float | None, digits: int = 1) -> str:
    return "–" if value is None else f"{100 * value:.{digits}f}%"


def ms(value: float | None) -> str:
    """A seat that never got to act has no latency: night 1's victim, on the original rules."""
    return "–" if value is None else f"{value:.0f} ms"


def player_slug(player_id: str) -> str:
    """A player id as one URL and file name segment: ids hold slashes, and a case-insensitive
    disk must not merge two players. Reversible, so no two ids share a slug."""
    safe = "abcdefghijklmnopqrstuvwxyz0123456789-."
    return "".join(c if c in safe else f"_{ord(c):x}_" for c in player_id)


def _connect4_move(a: dict) -> str | None:
    return f"column {a['column']}" if set(a) == {"column"} else None


def _liars_dice_move(a: dict) -> str | None:
    if a.get("action") == "challenge":
        return "challenge"
    if a.get("action") == "bid" and {"quantity", "face"} <= set(a):
        return f"bid {a['quantity']} × {a['face']}"
    return None


def _code_break_move(a: dict) -> str | None:
    code = code_of(a)
    return f"guess {code}" if code else None


def _split_deal_move(a: dict) -> str | None:
    if a.get("action") in ("accept", "reject"):
        return a["action"]
    if a.get("action") == "offer" and set(ITEMS) <= set(a):
        return "keep " + ", ".join(f"{a[item]} {item}" for item in ITEMS)
    return None


def _word_link_move(a: dict) -> str | None:
    if {"word", "number"} <= set(a):
        return f"clue {a['word']} {a['number']}"
    if a.get("action") == "stop":
        return "stop"
    return f"guess {a['word']}" if a.get("action") == "guess" and "word" in a else None


GAME_NAMES = {
    "code_break": "Code Break",
    "connect4": "Connect 4",
    "liars_dice": "Liar's Dice",
    "liars_dice_small": "Liar's Dice (small)",
    "split_deal": "Split the Deal",
    "werewolf": "Werewolf",
    "werewolf_dayfirst": "Werewolf (day-first)",
    "word_link": "Word Link",
}
BLIND_GAMES = ("werewolf", "werewolf_dayfirst")
GAME_BLURBS = {
    "code_break": "Two players race to crack one secret code. A solver never needs over 5 guesses.",
    "connect4": "Four in a row. Simple rules, and the first move decides most games.",
    "liars_dice": "Hidden dice and bold bids. A short probability bot sets the bar.",
    "liars_dice_small": "Two dice each, faces 1-4: small enough to solve exactly.",
    "split_deal": "Two players divide three kinds of item, each hiding what they're worth.",
    "werewolf": "Seven seats, two wolves, a night kill to open. Bluffing in plain text.",
    "werewolf_dayfirst": "The same game opening by day, with everyone still alive.",
    "word_link": "Two teams chase their words from one-word clues, around a trap.",
}


def _is_baseline(row: dict) -> bool:
    return re.fullmatch(r"random\d*", row["player_id"]) is not None


def contenders(rows: list[dict]) -> list[dict]:
    """The real field. If only baselines have played, they're all there is to show."""
    return [r for r in rows if not _is_baseline(r)] or rows


def clear_leader(rows: list[dict]) -> str | None:
    """Who leads, only if the numbers can tell the top two apart. Finals decide when both have
    one, since they compare only within one tournament; otherwise the live ordinal gap must
    exceed both sigmas. Always the top two: skipping a rival with no final could invent a lead."""
    field = contenders(rows)
    if len(field) < 2:
        return None
    top, second = field[:2]
    if top["final"] and second["final"]:
        clear = top["final"]["low"] > second["final"]["high"]
    else:
        clear = top["ordinal"] - second["ordinal"] > max(top["sigma"], second["sigma"])
    return top["player_id"] if clear else None


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
    if m["game"] == "split_deal" and best == 0:
        return "No deal"
    winners = [m["seats"][seat] for seat, score in enumerate(scores) if score == best]
    if m["game"] == "word_link" and len(winners) == 2:
        return f"{winners[0]} and {winners[1]} won"
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


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% interval for a share: unlike a normal approximation, sound at small counts."""
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    spread = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - spread), min(1.0, centre + spread)


def village_share(matches: list[dict]) -> dict | None:
    """How often the village won, with the uncertainty a headline number should carry."""
    cells = result_cells(matches)
    if not cells:
        return None
    won = sum(c["side"] == "village" for c in cells)
    low, high = wilson(won, len(cells))
    return {"won": won, "games": len(cells), "share": won / len(cells), "low": low, "high": high}


def seat_outcome(game: str, scores: list[float], seat: int) -> str:
    """Won, lost or drew, for one seat. Werewolf's winners share a score, and that's a win."""
    best = max(scores)
    if scores[seat] < best:
        return "lost"
    return "won" if game in BLIND_GAMES or scores.count(best) == 1 else "drew"


def summarise(rows: Iterable[dict]) -> dict:
    """Totals over `read.seat_results` rows. Rates are None when nothing was measured, and
    illegal moves are defined as in store/metrics.py."""
    rows = list(rows)
    games = len(rows)
    turns = sum(r["turns"] for r in rows)
    attempts = sum(r["attempts"] for r in rows)
    prompt = sum(r["input_tokens"] + r["cache_read_tokens"] + r["cache_write_tokens"] for r in rows)
    outcomes = Counter(seat_outcome(r["game"], r["scores"], r["seat"]) for r in rows)
    cost = sum(r["cost_usd"] for r in rows)
    return {
        "games": games,
        "won": outcomes["won"],
        "drew": outcomes["drew"],
        "lost": outcomes["lost"],
        "win_rate": outcomes["won"] / games if games else None,
        "cost_usd": cost,
        "cost_per_game": cost / games if games else None,
        "illegal_rate": (
            (attempts - sum(r["accepted"] for r in rows)) / attempts if attempts else None
        ),
        "cache_hit": sum(r["cache_read_tokens"] for r in rows) / prompt if prompt else None,
        "latency_ms": sum(r["latency_ms"] for r in rows) / turns if turns else None,
    }


def model_of(player_id: str) -> str:
    return player_id.partition("/")[0]


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
    "code_break": _code_break_move,
    "connect4": _connect4_move,
    "liars_dice": _liars_dice_move,
    "liars_dice_small": _liars_dice_move,
    "split_deal": _split_deal_move,
    "werewolf": _werewolf_move,
    "werewolf_dayfirst": _werewolf_move,
    "word_link": _word_link_move,
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
env.filters["pct"] = pct
env.filters["ms"] = ms
env.filters["player_slug"] = player_slug
env.filters["village_share"] = village_share
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
env.filters["clear_leader"] = clear_leader
env.filters["baselines"] = baselines
# Inlined into the match page (not {% include %}d: JS braces would clash with Jinja), so
# the page works as a single file in a static export. Tests load it in Node directly.
REPLAY_JS = Path(__file__).parent / "templates" / "replay.js"
env.globals["replay_js"] = Markup(REPLAY_JS.read_text(encoding="utf-8"))
SITE_CSS = Path(__file__).parent / "templates" / "site.css"
env.globals["site_css"] = Markup(SITE_CSS.read_text(encoding="utf-8"))
SITE_JS = Path(__file__).parent / "templates" / "site.js"
env.globals["site_js"] = Markup(SITE_JS.read_text(encoding="utf-8"))


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


def _by_game(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r["game"], []).append(r)
    return grouped


def _recent(rows: list[dict]) -> list[dict]:
    """A player's games, newest first, in that player's own terms: their role, their result."""
    recent = []
    for r in rows:
        role = None
        if r["game"] in BLIND_GAMES:
            state = get_game(r["game"]).initial_state(len(r["scores"]), r["seed"], r["options"])
            role = state.roles[r["seat"]]
        outcome = seat_outcome(r["game"], r["scores"], r["seat"])
        recent.append(
            {
                "id": r["match_id"],
                "game": r["game"],
                "role": role,
                "outcome": outcome,
                "cost_usd": r["cost_usd"],
            }
        )
    return recent


async def player_data(pool: asyncpg.Pool, player_id: str) -> dict:
    """One player's record, per game and overall. Shared by the page and the JSON API."""
    rows = await read.seat_results(pool, player_id)
    games = []
    for game, played in _by_game(rows).items():
        board = await leaderboard(pool, game)
        field = contenders(board)
        place = next((i for i, r in enumerate(field, 1) if r["player_id"] == player_id), None)
        standing = next((r for r in board if r["player_id"] == player_id), None)
        games.append(
            {
                "game": game,
                "place": place,
                "field": len(field),
                "ordinal": standing["ordinal"] if standing else None,
                **summarise(played),
            }
        )
    roles = None
    if any(g["game"] in BLIND_GAMES for g in games):
        by_player = {r["player_id"]: r for r in await metrics.werewolf_metrics(pool)}
        roles = by_player.get(player_id)
    return {
        "player_id": player_id,
        "model": model_of(player_id),
        "overall": summarise(rows),
        "games": games,
        "roles": roles,
        "recent": _recent(rows),
    }


async def _find_player(pool: asyncpg.Pool, slug: str) -> str | None:
    return next((p for p in await read.player_ids(pool) if player_slug(p) == slug), None)


async def player_json(pool: asyncpg.Pool, slug: str) -> dict | None:
    player_id = await _find_player(pool, slug)
    return None if player_id is None else await player_data(pool, player_id)


async def player_page(pool: asyncpg.Pool, urls: Urls, slug: str) -> str | None:
    found = await player_json(pool, slug)
    return None if found is None else render("player.html", urls, **found)


async def compare_data(pool: asyncpg.Pool) -> dict:
    """Each real model across all its players: bots and random players aren't models."""
    rows = [r for r in await read.seat_results(pool) if r["provider"] not in ("bot", "random")]
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(model_of(r["player_id"]), []).append(r)
    names = sorted(grouped, reverse=True)
    return {
        "models": [
            {
                "model": name,
                "players": sorted({r["player_id"] for r in grouped[name]}),
                **summarise(grouped[name]),
            }
            for name in names
        ],
        "games": [
            {
                "game": game,
                "rows": [
                    {
                        "model": name,
                        **summarise(r for r in played if model_of(r["player_id"]) == name),
                    }
                    for name in names
                    if any(model_of(r["player_id"]) == name for r in played)
                ],
            }
            for game, played in sorted(_by_game(rows).items())
        ],
    }


async def compare_page(pool: asyncpg.Pool, urls: Urls) -> str:
    return render("compare.html", urls, **await compare_data(pool))


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
