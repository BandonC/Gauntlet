import json

import pytest
from jinja2 import UndefinedError

from arena.web import SERVER_ROUTES, Urls, move_label, render

URLS = Urls()


def list_row(**over):
    row = {
        "id": 3,
        "game": "connect4",
        "tournament_id": 7,
        "round": None,
        "seats": ["a", "b"],
        "status": "completed",
        "scores": None,
        "cost_usd": 0.0,
    }
    return row | over  # fmt: skip


def lb_row(pid, rating=1600.0, rd=100.0):
    return {
        "player_id": pid,
        "rating": rating,
        "rd": rd,
        "low": rating - 2 * rd,
        "high": rating + 2 * rd,
        "elo": 1516.0,
        "games": 3,
        "points": 2.5,
    }


def tournament(**over):
    t = {
        "id": 7,
        "game": "connect4",
        "format": "round_robin",
        "status": "running",
        "rounds": None,
        "current_round": 0,
        "last_error": None,
        "budget_usd": 2.0,
        "spent_usd": 0.1234,
        "total": 10,
        "completed": 4,
        "failed": 0,
    }
    return t | over


def match(**over):
    m = {
        "id": 3,
        "tournament_id": 7,
        "round": None,
        "game": "connect4",
        "status": "completed",
        "scores": [1.0, 0.0],
        "last_error": None,
        "seats": ["haiku", "random"],
        "turns": [
            {
                "turn_no": 0,
                "seat": 0,
                "action": {"column": 3},
                "forfeited": False,
                "attempts": 2,
                "error": "column 9 is out of range 0-6",
                "cost_usd": 0.00011,
                "latency_ms": 640,
                "tokens": {
                    "input_tokens": 300,
                    "output_tokens": 40,
                    "cache_read_tokens": 1000,
                    "cache_write_tokens": 0,
                },
            },
            {
                "turn_no": 1,
                "seat": 1,
                "action": None,
                "forfeited": True,
                "attempts": 3,
                "error": None,
                "cost_usd": 0.0,
                "latency_ms": 0,
                "tokens": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                },
            },
        ],
        "frames": [],
    }
    return m | over


def test_leaderboard_escapes_player_ids_and_shows_confidence():
    html = render(
        "index.html",
        URLS,
        boards={"connect4": [lb_row("<script>x</script>")], "liars_dice": []},
    )
    assert "<script>x</script>" not in html and "&lt;script&gt;x&lt;/script&gt;" in html
    assert "1600.0 ± 200.0" in html and ">1516.0<" in html
    assert "No rated matches yet." in html  # liars_dice has none


def test_tournaments_show_round_failures_pause_reason_and_budget():
    html = render(
        "tournaments.html",
        URLS,
        tournaments=[
            tournament(),
            tournament(
                id=8,
                format="swiss",
                status="paused",
                rounds=3,
                current_round=2,
                failed=1,
                last_error="match 12 failed: boom",
                budget_usd=None,
            ),
        ],
    )
    assert 'href="/tournaments/7"' in html and "$0.1234 / $2.00" in html
    assert "2 / 3" in html and "(1 failed)" in html and 'title="match 12 failed: boom"' in html


def test_tournament_page_lists_its_matches():
    ms = [list_row(status="queued")]
    html = render(
        "tournament.html", URLS, t=tournament(status="paused", last_error="x"), matches=ms
    )
    assert 'href="/matches/3"' in html and "a vs b" in html and "Paused because" in html
    assert "No matches yet." in render("tournament.html", URLS, t=tournament(), matches=[])


def test_match_page_shows_moves_rejections_and_forfeits():
    html = render("match.html", URLS, m=match())
    assert "<td>column 3" in html  # a readable label, not raw JSON
    assert "last rejected: column 9 is out of range 0-6" in html
    assert ">forfeit<" in html and "300 / 40 / 1000" in html and "$0.00011" in html
    assert 'href="/tournaments/7"' in html and "seat 0: haiku (1.0)" in html


def test_links_follow_the_url_table_so_export_can_use_file_paths():
    files = Urls({**SERVER_ROUTES, "match": "matches/{id}.html", "index": "index.html"})
    html = render(
        "matches.html",
        files,
        matches=[list_row()],
    )
    assert 'href="matches/3.html"' in html and 'href="index.html"' in html


def test_template_typos_fail_loudly():
    m = match()
    del m["seats"]
    with pytest.raises(UndefinedError):
        render("match.html", URLS, m=m)


def _embedded(html):
    start = html.index('<script id="replay-data" type="application/json">') + len(
        '<script id="replay-data" type="application/json">'
    )
    return html[start : html.index("</script>", start)]


def test_match_page_embeds_replay_data_and_rows_jump_to_frames():
    m = match(frames=[{"n": 0}, {"n": 1}, {"n": 2}])
    html = render("match.html", URLS, m=m)
    data = json.loads(_embedded(html))
    assert data["frames"] == m["frames"] and data["seats"] == m["seats"]
    assert data["game"] == "connect4" and len(data["turns"]) == 2
    assert '<tr data-frame="1">' in html and '<tr data-frame="2">' in html
    assert "function initReplay" in html  # the stepping script is inlined


def test_hostile_player_id_cannot_close_the_data_script():
    evil = "</script><script>alert(1)</script>"
    html = render("match.html", URLS, m=match(seats=[evil, "b"], frames=[{"x": evil}]))
    blob = _embedded(html)
    assert "</script" not in blob and "<script" not in blob
    assert json.loads(blob)["seats"][0] == evil  # still round-trips exactly


def test_inlined_replay_script_cannot_close_its_own_tag():
    from arena.web import REPLAY_JS

    assert "</script" not in REPLAY_JS.read_text(encoding="utf-8").lower()


@pytest.mark.parametrize(
    ("game", "action", "forfeited", "label"),
    [
        ("connect4", {"column": 3}, False, "column 3"),
        ("liars_dice", {"face": 6, "action": "bid", "quantity": 3}, False, "bid 3 × 6"),
        ("liars_dice", {"action": "challenge"}, False, "challenge"),
        ("liars_dice", None, True, "forfeit"),
        # Unknown games or unexpected shapes fall back to sorted JSON, never crash.
        ("liars_dice", {"action": "pass"}, False, '{"action": "pass"}'),
        ("werewolf", {"vote": 2, "a": 1}, False, '{"a": 1, "vote": 2}'),
    ],
)
def test_move_labels(game, action, forfeited, label):
    assert move_label(game, action, forfeited) == label


def test_replay_data_carries_the_same_labels_as_the_turn_table():
    html = render("match.html", URLS, m=match(frames=[{}, {}, {}]))
    turns = json.loads(_embedded(html))["turns"]
    assert [t["label"] for t in turns] == ["column 3", "forfeit"]


def test_match_list_shows_game_and_tournament_round():
    rows = [list_row(id=4, game="liars_dice", round=2), list_row(id=5, tournament_id=None)]
    html = render("matches.html", URLS, matches=rows)
    assert "<td>liars_dice</td>" in html and "<td>connect4</td>" in html
    assert '<a href="/tournaments/7">#7</a>, round 2' in html


def test_export_routes_are_flat_and_cover_every_server_route():
    from arena.export import FILE_ROUTES

    assert FILE_ROUTES.keys() == SERVER_ROUTES.keys()
    # Every page sits in the same directory, so relative links work from any of them.
    assert not any("/" in r or r.startswith(".") for r in FILE_ROUTES.values())
    assert len(set(FILE_ROUTES.values())) == len(FILE_ROUTES)


def test_export_json_encodes_postgres_decimals_as_numbers():
    from decimal import Decimal

    from arena.export import _json_default

    out = json.dumps({"n": Decimal(3), "x": Decimal("0.25")}, default=_json_default)
    assert json.loads(out) == {"n": 3, "x": 0.25}
    with pytest.raises(TypeError):
        json.dumps({"s": {1}}, default=_json_default)
