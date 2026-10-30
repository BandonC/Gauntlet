import pytest
from jinja2 import UndefinedError

from arena.web import SERVER_ROUTES, Urls, render

URLS = Urls()


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
    ms = [{"id": 3, "seats": ["a", "b"], "status": "queued", "scores": None, "cost_usd": 0.0}]
    html = render(
        "tournament.html", URLS, t=tournament(status="paused", last_error="x"), matches=ms
    )
    assert 'href="/matches/3"' in html and "a vs b" in html and "Paused because" in html
    assert "No matches yet." in render("tournament.html", URLS, t=tournament(), matches=[])


def test_match_page_shows_moves_rejections_and_forfeits():
    html = render("match.html", URLS, m=match())
    assert "<code>{&#34;column&#34;: 3}</code>" in html  # escaped JSON
    assert "last rejected: column 9 is out of range 0-6" in html
    assert ">forfeit<" in html and "300 / 40 / 1000" in html and "$0.00011" in html
    assert 'href="/tournaments/7"' in html and "seat 0: haiku (1.0)" in html


def test_links_follow_the_url_table_so_export_can_use_file_paths():
    files = Urls({**SERVER_ROUTES, "match": "matches/{id}.html", "index": "index.html"})
    html = render(
        "matches.html",
        files,
        matches=[{"id": 3, "seats": ["a", "b"], "status": "x", "scores": None, "cost_usd": 0.0}],
    )
    assert 'href="matches/3.html"' in html and 'href="index.html"' in html


def test_template_typos_fail_loudly():
    m = match()
    del m["seats"]
    with pytest.raises(UndefinedError):
        render("match.html", URLS, m=m)
