import json

import pytest
from jinja2 import UndefinedError

from arena.live import format_event, resume_point
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
        "ordinal": 9.75,
        "mu": 27.75,
        "sigma": 6.0,
        "elo": 1516.0,
        "games": 3,
        "points": 2.5,
        "final": None,
        "final_tournament_id": None,
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
        "placements": [1, 2],
        "last_error": None,
        "seats": ["haiku", "random"],
        "turns": [
            {
                "turn_no": 0,
                "seat": 0,
                "action": {"column": 3},
                "forfeited": False,
                "truncated": False,
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
                "truncated": False,
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
        role_stats=[],
    )
    assert "<script>x</script>" not in html and "&lt;script&gt;x&lt;/script&gt;" in html
    assert ">9.75<" in html and "27.75 ± 18.00" in html  # Weng-Lin ranks the board
    assert "1600.0 ± 200.0" in html and ">1516.0<" in html
    assert "No rated matches yet." in html  # liars_dice has none


def test_leaderboard_labels_live_ratings_and_shows_the_final_rating():
    final = {"rating": 1620.4, "low": 1500.1, "high": 1740.7}
    row = lb_row("<b>x</b>") | {"final": final, "final_tournament_id": 4}
    other = lb_row("newcomer") | {"final_tournament_id": 4}
    html = render("index.html", URLS, boards={"liars_dice": [row, other]}, role_stats=[])
    assert "Final rating (95%)" in html and "Live ordinal" in html and "Live Elo" in html
    assert "1620.4 (1500.1 – 1740.7)" in html
    assert 'href="/tournaments/4"' in html
    assert ">–<" in html  # a player outside that tournament has no final rating


def test_tournament_page_lists_final_ratings_only_when_there_are_any():
    t = tournament()
    final = {"<b>x</b>": {"rating": 1700.0, "low": 1600.0, "high": 1800.0}}
    html = render("tournament.html", URLS, t=t, matches=[], final=final)
    assert "Final ratings" in html and "1600.0 – 1800.0" in html and "<b>x</b>" not in html
    assert "Final ratings" not in render("tournament.html", URLS, t=t, matches=[], final=None)


def test_werewolf_role_stats_show_win_rates_by_role_and_claims():
    row = {
        "player_id": "<b>x</b>",
        "wolf_games": 4,
        "wolf_win_rate": 0.75,
        "seer_games": 0,
        "seer_win_rate": None,
        "villager_games": 8,
        "villager_win_rate": 0.5,
        "true_claims": 3,
        "false_claims": 2,
        "leaks": 1,
        "truncated_messages": 6,
    }
    html = render("index.html", URLS, boards={}, role_stats=[row])
    assert "Werewolf roles" in html and "paraphrases are missed" in html
    assert "0.75 (4)" in html and "– (0)" in html and "0.5 (8)" in html
    assert "<b>x</b>" not in html
    assert "Werewolf roles" not in render("index.html", URLS, boards={}, role_stats=[])


def test_werewolf_moves_have_readable_labels():
    assert move_label("werewolf", {"action": "kill", "target": 3}) == "kill seat 3"
    assert move_label("werewolf", {"action": "vote", "target": 0}) == "vote seat 0"
    assert move_label("werewolf", {"action": "say", "message": "hi"}) == "says: hi"


def replay_data(html):
    return json.loads(
        html.split('id="replay-data" type="application/json">')[1].split("</script>")[0]
    )


@pytest.mark.parametrize(
    ("status", "events"),
    [
        ("queued", "/api/matches/3/events"),
        ("running", "/api/matches/3/events"),
        ("completed", ""),
        ("failed", ""),
    ],
)
def test_only_a_match_in_progress_gets_a_live_stream(status, events):
    html = render("match.html", URLS, m=match(status=status))
    assert replay_data(html)["events"] == events


def test_exported_pages_never_go_live():
    from arena.export import FILE_URLS

    html = render("match.html", FILE_URLS, m=match(status="running"))
    assert replay_data(html)["events"] == ""


def test_event_format_is_one_valid_sse_message():
    msg = format_event("turn", 3, {"text": "line one\nline two"})
    assert msg == 'id: 3\nevent: turn\ndata: {"text":"line one\\nline two"}\n\n'


@pytest.mark.parametrize(
    ("after", "header", "expected"),
    [(0, None, 0), (4, None, 4), (4, "9", 9), (9, "4", 9), (2, "junk", 2), (-5, None, 0)],
)
def test_resume_point_takes_the_furthest_the_client_has_seen(after, header, expected):
    assert resume_point(after, header) == expected


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
        "tournament.html",
        URLS,
        t=tournament(status="paused", last_error="x"),
        matches=ms,
        final=None,
    )
    assert 'href="/matches/3"' in html and "a vs b" in html and "Paused because" in html
    assert "No matches yet." in render(
        "tournament.html", URLS, t=tournament(), matches=[], final=None
    )


def test_match_page_shows_moves_rejections_and_forfeits():
    html = render("match.html", URLS, m=match())
    assert "<td>column 3" in html  # a readable label, not raw JSON
    assert "last rejected: column 9 is out of range 0-6" in html
    assert ">forfeit<" in html and "300 / 40 / 1000" in html and "$0.00011" in html
    assert 'href="/tournaments/7"' in html and "seat 0: haiku (1.0, place 1)" in html


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
