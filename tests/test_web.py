import json
import re

import pytest
from jinja2 import UndefinedError

from arena.games.registry import list_games
from arena.live import format_event, resume_point
from arena.web import (
    GAME_BLURBS,
    GAME_NAMES,
    REPLAY_JS,
    SERVER_ROUTES,
    SITE_CSS,
    Urls,
    baselines,
    compact,
    contenders,
    game_name,
    match_html,
    match_result,
    move_label,
    render,
    result_cells,
    result_tally,
)

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
    assert "<script>x" not in html and "&lt;script&gt;x&lt;" in html
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
        "forfeits": 2,
    }
    html = render("index.html", URLS, boards={}, role_stats=[row])
    assert "Werewolf roles" in html and "paraphrases are missed" in html
    assert "0.75 (4)" in html and "– (0)" in html and "0.5 (8)" in html
    assert "<b>x</b>" not in html
    assert "Werewolf roles" not in render("index.html", URLS, boards={}, role_stats=[])


def test_werewolf_moves_have_readable_labels():
    assert move_label("werewolf", {"action": "kill", "target": 3}) == "kill seat 3"
    assert move_label("werewolf_dayfirst", {"action": "kill", "target": 3}) == "kill seat 3"
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
    assert 'href="/tournaments/7"' in html and '<span class="pid">haiku</span>' in html
    assert "<dt>Result</dt><dd>haiku won</dd>" in html
    assert '<td class="num">1.0</td>' in html  # each seat's score has its own row


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


@pytest.mark.parametrize(
    ("game", "status", "offered"),
    [
        ("werewolf", "completed", True),
        ("werewolf_dayfirst", "completed", True),
        ("werewolf_dayfirst", "running", False),
        ("connect4", "completed", False),
        ("liars_dice", "completed", False),
    ],
)
def test_play_blind_is_only_offered_on_finished_werewolf_games(game, status, offered):
    html = match_html(URLS, match(game=game, status=status))
    assert ('id="blind-start"' in html) is offered
    assert ('id="blind-view"' in html) is offered


@pytest.mark.parametrize(
    ("over", "result"),
    [
        ({"seats": ["a", "b"], "scores": [1.0, 0.0]}, "a won"),
        ({"seats": ["a", "b"], "scores": [0.0, 1.0]}, "b won"),
        ({"seats": ["a", "b"], "scores": [0.5, 0.5]}, "Draw"),
        ({"game": "liars_dice", "seats": ["a", "b", "c"], "scores": [0, 1, 0]}, "b won"),
        ({"game": "werewolf", "seats": list("abcdefg"), "scores": [1, 1, 0, 0, 0, 0, 0]}, "Wolves"),
        (
            {
                "game": "werewolf_dayfirst",
                "seats": list("abcdefg"),
                "scores": [0, 0, 1, 1, 1, 1, 1],
            },
            "Village",
        ),
        ({"status": "running", "scores": None}, None),
        ({"status": "failed", "scores": None}, None),
    ],
)
def test_a_result_says_who_won(over, result):
    assert match_result(list_row() | over) == result


def test_an_unfinished_match_shows_its_status_where_the_result_goes():
    html = render("matches.html", URLS, matches=[list_row(status="running")])
    assert "<td>running</td>" in html


def test_game_names_read_well_and_unknown_games_fall_back_to_their_id():
    assert game_name("werewolf_dayfirst") == "Werewolf (day-first)"
    assert game_name("chess") == "chess"
    assert all(g in GAME_NAMES for g in list_games())  # a new game needs a display name


def test_a_table_of_seven_is_summarised_with_the_names_on_hover():
    seats = [f"p{i}" for i in range(7)]
    html = render("matches.html", URLS, matches=[list_row(seats=seats)])
    assert ">7 players<" in html and 'title="p0, p1, p2, p3, p4, p5, p6"' in html
    assert "p0 vs p1" not in html


def test_inside_a_tournament_rows_drop_what_the_page_already_says():
    t = tournament()
    html = render("tournament.html", URLS, t=t, matches=[list_row()], final=None)
    assert "<th>Result</th>" in html
    assert "<th>Game</th>" not in html and "<th>Tournament</th>" not in html


def test_a_table_tournament_drops_the_seat_count_and_keeps_pairs_named():
    t = tournament()
    seats = [f"p{i}" for i in range(7)]
    table = render("tournament.html", URLS, t=t, matches=[list_row(seats=seats)], final=None)
    assert "<th>Players</th>" not in table and "7 players" not in table
    pair = render("tournament.html", URLS, t=t, matches=[list_row()], final=None)
    assert "<th>Players</th>" in pair and "a vs b" in pair


def test_a_werewolf_tournament_totals_its_results_and_others_do_not():
    seven = list("abcdefg")
    wolves = list_row(game="werewolf", seats=seven, scores=[1, 1, 0, 0, 0, 0, 0])
    village = list_row(game="werewolf", seats=seven, scores=[0, 0, 1, 1, 1, 1, 1])
    live = list_row(game="werewolf", seats=seven, status="running")
    assert result_tally([wolves, village, wolves, live]) == "Wolves 2, Village 1"
    assert result_tally([list_row(scores=[1.0, 0.0])]) is None
    assert result_tally([]) is None
    t = tournament()
    html = render("tournament.html", URLS, t=t, matches=[wolves], final=None)
    assert "<dt>Results</dt><dd>Wolves 1</dd>" in html


def test_a_player_id_splits_into_a_model_and_a_quieter_prompt():
    html = render(
        "index.html", URLS, boards={"connect4": [lb_row("haiku-5.5/c4-v1")]}, role_stats=[]
    )
    assert '<span class="pid">haiku-5.5<span>/c4-v1</span></span>' in html


def test_the_home_page_invites_you_to_play_blind_only_when_a_game_is_available():
    boards = {"connect4": []}
    html = render("index.html", URLS, boards=boards, role_stats=[], blind_url="/matches/9#blind")
    assert 'href="/matches/9#blind"' in html
    assert "/matches/9#blind" not in render("index.html", URLS, boards=boards, role_stats=[])
    assert 'class="btn primary"' not in render(
        "index.html", URLS, boards=boards, role_stats=[], blind_url=None
    )


def _palettes():
    css = SITE_CSS.read_text("utf-8")
    light = css[css.index(":root {") : css.index("@media (prefers-color-scheme: dark)")]
    system = css[
        css.index("@media (prefers-color-scheme: dark)") : css.index(':root[data-theme="dark"]')
    ]
    toggled = css[css.index(':root[data-theme="dark"]') : css.index("/* Base */")]
    return light, system, toggled


def _tokens(block):
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-f]{6})", block))


def _luminance(colour):
    channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_the_dark_palette_is_the_same_for_the_system_setting_and_the_toggle():
    _, system, toggled = _palettes()
    assert _tokens(system) == _tokens(toggled) and len(_tokens(toggled)) == 21


@pytest.mark.parametrize("index", [0, 2], ids=["light", "dark"])
@pytest.mark.parametrize("text", ["fg", "muted", "accent", "bad", "good", "x", "o", "link"])
@pytest.mark.parametrize("on", ["bg", "surface"])
def test_every_palette_stays_readable(index, text, on):
    t = _tokens(_palettes()[index])
    assert _contrast(t[text], t[on]) >= 4.5


@pytest.mark.parametrize("index", [0, 2], ids=["light", "dark"])
@pytest.mark.parametrize("wash", ["wash-1", "wash-2", "wash-3"])
@pytest.mark.parametrize("text", ["fg", "muted", "link"])
def test_text_stays_readable_on_every_colour_of_the_page_wash(index, wash, text):
    t = _tokens(_palettes()[index])
    assert _contrast(t[text], t[wash]) >= 4.5


@pytest.mark.parametrize("index", [0, 2], ids=["light", "dark"])
def test_primary_buttons_stay_readable(index):
    t = _tokens(_palettes()[index])
    assert _contrast(t["on-accent"], t["accent"]) >= 4.5


@pytest.mark.parametrize("index", [0, 2], ids=["light", "dark"])
@pytest.mark.parametrize("mark", ["g-c4", "g-dice", "g-ww", "g-df", "mark-wolf", "mark-village"])
def test_chart_marks_stand_out_from_the_surface(index, mark):
    t = _tokens(_palettes()[index])
    assert _contrast(t[mark], t["surface"]) >= 3


def test_the_site_has_no_3d():
    css = SITE_CSS.read_text("utf-8")
    assert not re.search(r"rotate[XYZ3]|perspective|preserve-3d|backface", css)
    js = REPLAY_JS.read_text("utf-8")
    assert "unmask" not in js and "face front" not in js


def test_the_theme_button_is_a_sun_and_a_moon():
    html = render("matches.html", URLS, matches=[])
    assert 'class="sun"' in html and 'class="moon"' in html


@pytest.mark.parametrize(
    ("template", "context", "current"),
    [
        ("index.html", {"boards": {}, "role_stats": []}, "Leaderboards"),
        ("tournaments.html", {"tournaments": []}, "Tournaments"),
        ("matches.html", {"matches": []}, "Matches"),
    ],
)
def test_the_nav_marks_the_current_page(template, context, current):
    html = render(template, URLS, **context)
    assert html.count('aria-current="page">') == 1
    assert f'aria-current="page">{current}</a>' in html


def test_the_waffle_groups_results_and_links_only_what_exists():
    seven = list("abcdefg")
    v = list_row(id=1, game="werewolf", seats=seven, scores=[0, 0, 1, 1, 1, 1, 1])
    w = list_row(id=2, game="werewolf", seats=seven, scores=[1, 1, 0, 0, 0, 0, 0])
    live = list_row(id=3, game="werewolf", seats=seven, status="running")
    assert [c["side"] for c in result_cells([v, w, live])] == ["wolves", "village"]
    assert result_cells([list_row(scores=[1.0, 0.0])]) == []
    html = render("tournament.html", URLS, t=tournament(), matches=[v, w], final=None)
    assert 'class="cell wolves" href="/matches/2"' in html and "Wolves 1" in html
    assert 'title="#1: Village"' in html
    assert 'class="waffle"' not in render(
        "tournament.html", URLS, t=tournament(), matches=[list_row()], final=None
    )


def test_a_result_chip_carries_its_mark_beside_plain_text():
    seven = list("abcdefg")
    row = list_row(game="werewolf", seats=seven, scores=[1, 1, 0, 0, 0, 0, 0])
    html = render("matches.html", URLS, matches=[row])
    assert '<span class="chip wolves">Wolves</span>' in html


@pytest.mark.parametrize(
    ("n", "text"), [(0, "0"), (999, "999"), (1500, "1.5K"), (7_700_217, "7.7M")]
)
def test_headline_numbers_are_compact(n, text):
    assert compact(n) == text


def test_every_game_has_a_blurb_and_a_pattern():
    css = SITE_CSS.read_text("utf-8")
    for game in list_games():
        assert game in GAME_BLURBS
        assert f".g-{game} " in css and f".banner.g-{game}::before" in css


def test_the_home_page_leads_with_the_headlines_and_a_card_for_each_game():
    boards = {"connect4": [lb_row("haiku-5.5/c4-v1")], "liars_dice": []}
    totals = {"games": 131, "tokens": 7_700_217, "cost_usd": 4.5124}
    html = render("index.html", URLS, boards=boards, role_stats=[], totals=totals)
    assert "<dd>131</dd>" in html and "<dd>7.7M</dd>" in html and "<dd>$4.51</dd>" in html
    assert "Leads: haiku-5.5/c4-v1" in html and 'href="#liars_dice"' in html


def test_the_rule_change_story_needs_both_rulesets():
    seven = list("abcdefg")
    row = list_row(game="werewolf", seats=seven, scores=[1, 1, 0, 0, 0, 0, 0])
    both = [{"game": "werewolf", "matches": [row]}, {"game": "werewolf_dayfirst", "matches": [row]}]
    assert "One rule change" in render("index.html", URLS, boards={}, role_stats=[], stories=both)
    assert "One rule change" not in render(
        "index.html", URLS, boards={}, role_stats=[], stories=both[:1]
    )


def test_a_jump_to_a_section_lands_clear_of_the_top_edge():
    assert "[id] { scroll-margin-top:" in SITE_CSS.read_text("utf-8")


def test_every_game_has_its_own_wallpaper_and_the_home_page_a_mix():
    css = SITE_CSS.read_text("utf-8")
    assert "--tile-mix:" in css
    for game in list_games():
        assert f"--tile-{game}:" in css and f"body.g-{game} {{" in css


def test_game_pages_carry_their_game_on_the_body():
    html = render("tournament.html", URLS, t=tournament(), matches=[], final=None)
    assert '<body class="g-connect4">' in html
    assert '<body class="g-liars_dice">' in match_html(URLS, match(game="liars_dice"))
    assert "<body>" in render("matches.html", URLS, matches=[])


@pytest.mark.parametrize("name", ["random", "random2", "random13"])
def test_random_players_fold_into_baselines(name):
    rows = [lb_row("haiku-5.5/c4-v1"), lb_row(name)]
    assert contenders(rows) == rows[:1] and baselines(rows) == rows[1:]


@pytest.mark.parametrize("name", ["randomized-bot", "liars-dice-bot", "my-random"])
def test_a_name_that_merely_contains_random_is_a_real_player(name):
    assert baselines([lb_row("a"), lb_row(name)]) == []


def test_a_board_of_only_baselines_still_shows_them():
    rows = [lb_row("random"), lb_row("random2")]
    assert contenders(rows) == rows and baselines(rows) == []


def test_baselines_stay_on_the_page_folded_and_out_of_the_lead():
    boards = {"connect4": [lb_row("random"), lb_row("haiku-5.5/c4-v1")]}
    html = render("index.html", URLS, boards=boards, role_stats=[])
    assert "<summary>Baselines (1)</summary>" in html and ">random<" in html
    assert "Leads: haiku-5.5/c4-v1" in html


def test_the_theme_is_applied_before_the_stylesheet_paints():
    html = render("matches.html", URLS, matches=[])
    assert html.index('localStorage.getItem("theme")') < html.index("--ease-out")
    assert 'id="theme-toggle"' in html
    # Without JavaScript the system setting still works, so the button stays out of the way.
    assert re.search(r'id="theme-toggle"[^>]*hidden', html)


def test_hidden_buttons_stay_hidden_despite_the_button_display_rule():
    assert "[hidden] { display: none !important; }" in SITE_CSS.read_text("utf-8")


def test_the_stylesheet_is_inlined_and_cannot_close_its_own_tag():
    html = render("matches.html", URLS, matches=[])
    assert "--ease-out" in html and "</style" not in SITE_CSS.read_text("utf-8").lower()
    assert "transition: all" not in SITE_CSS.read_text("utf-8")  # always name the property


def test_the_spoilers_are_all_inside_what_play_blind_hides():
    html = match_html(URLS, match(game="werewolf_dayfirst", status="completed"))
    hidden = html[html.index('<div class="spoiler">') :]
    for part in ("<dt>Result</dt>", "<th>Player</th>", 'id="replay-frame"', "<summary>Turns"):
        assert part in hidden


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
    assert '<span class="game-tag g-liars_dice">Liar&#39;s Dice</span>' in html
    assert '<span class="game-tag g-connect4">Connect 4</span>' in html
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
