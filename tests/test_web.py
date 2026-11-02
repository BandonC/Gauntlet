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
    clear_leader,
    compact,
    contenders,
    game_name,
    match_html,
    match_result,
    model_of,
    move_label,
    pct,
    player_slug,
    render,
    result_cells,
    result_tally,
    seat_outcome,
    summarise,
    village_share,
    wilson,
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
    assert _tokens(system) == _tokens(toggled) and len(_tokens(toggled)) == 44


@pytest.mark.parametrize("index", [0, 2], ids=["light", "dark"])
@pytest.mark.parametrize("text", ["fg", "muted", "accent", "bad", "good", "x", "o", "link"])
@pytest.mark.parametrize("on", ["bg", "surface"])
def test_every_palette_stays_readable(index, text, on):
    t = _tokens(_palettes()[index])
    assert _contrast(t[text], t[on]) >= 4.5


@pytest.mark.parametrize("index", [0, 2], ids=["light", "dark"])
@pytest.mark.parametrize(
    "wash", [f"{k}wash-{n}" for k in ("", "c4-", "dice-", "ww-", "df-") for n in (1, 2, 3)]
)
@pytest.mark.parametrize("text", ["fg", "muted", "link"])
def test_text_stays_readable_on_every_colour_of_the_page_wash(index, wash, text):
    t = _tokens(_palettes()[index])
    assert _contrast(t[text], t[wash]) >= 4.5


def _glows(block):
    return [
        ((int(r), int(g), int(b)), float(a))
        for r, g, b, a in re.findall(r"--glow-[ab]: rgba\((\d+), (\d+), (\d+), ([\d.]+)\)", block)
    ]


@pytest.mark.parametrize("index", [0, 2], ids=["light", "dark"])
@pytest.mark.parametrize(
    "wash", [f"{k}wash-{n}" for k in ("", "c4-", "dice-", "ww-", "df-") for n in (1, 2, 3)]
)
@pytest.mark.parametrize("text", ["fg", "muted", "link"])
def test_text_stays_readable_where_a_glow_and_the_wallpaper_cross_the_wash(index, wash, text):
    """The pixels text really sits on: wash, then the corner glows, then the wallpaper, which
    is at most 16% of a mark behind the text column, faded to 28% there."""
    block = _palettes()[index]
    t = _tokens(block)
    for rgb, alpha in _glows(block):
        glow = "#" + "".join(f"{c:02x}" for c in rgb)
        lit = _mixed(glow, t[wash], alpha)
        for mark in ("g-c4", "g-dice", "g-ww", "g-df"):
            assert _contrast(t[text], _mixed(t[mark], lit, 0.16 * 0.28)) >= 4.5


@pytest.mark.parametrize("index", [0, 2], ids=["light", "dark"])
def test_primary_buttons_stay_readable(index):
    t = _tokens(_palettes()[index])
    assert _contrast(t["on-accent"], t["accent"]) >= 4.5


@pytest.mark.parametrize("index", [0, 2], ids=["light", "dark"])
@pytest.mark.parametrize("mark", ["g-c4", "g-dice", "g-ww", "g-df", "mark-wolf", "mark-village"])
def test_chart_marks_stand_out_from_the_surface(index, mark):
    t = _tokens(_palettes()[index])
    assert _contrast(t[mark], t["surface"]) >= 3


@pytest.mark.parametrize("index", [0, 2], ids=["light", "dark"])
@pytest.mark.parametrize("seat", range(7))
def test_a_seat_number_is_readable_on_its_colour(index, seat):
    t = _tokens(_palettes()[index])
    assert _contrast(t[f"seat-{seat}"], t["surface"]) >= 4.5


def _mixed(colour, base, share):
    channels = (
        round(share * int(colour[i : i + 2], 16) + (1 - share) * int(base[i : i + 2], 16))
        for i in (1, 3, 5)
    )
    return "#" + "".join(f"{c:02x}" for c in channels)


@pytest.mark.parametrize("index", [0, 2], ids=["light", "dark"])
@pytest.mark.parametrize("mark", ["g-c4", "g-dice", "g-ww", "g-df"])
def test_hero_text_stays_readable_on_its_stronger_colours(index, mark):
    t = _tokens(_palettes()[index])
    assert "34%" in SITE_CSS.read_text("utf-8").split(".hero {")[1].split("}")[0]
    assert _contrast(t["fg"], _mixed(t[mark], t["surface"], 0.34)) >= 4.5


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
    assert "No clear leader yet" in html and 'href="#liars_dice"' in html
    assert "No rated matches yet." in html


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
        rule = css.split(f"body.g-{game} {{")[1].split("}")[0]
        assert "--wash-1: var(" in rule and "--wash-3: var(" in rule  # each has its own colours


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
    boards = {"connect4": [lb_row("random"), ranked("a", 20.0), ranked("b", 5.0)]}
    html = render("index.html", URLS, boards=boards, role_stats=[])
    assert "<summary>Baselines (1)</summary>" in html and ">random<" in html
    assert "Leads: a<" in html and "No clear leader" not in html


def ranked(pid, ordinal, sigma=5.0, final=None):
    row = lb_row(pid) | {"ordinal": ordinal, "sigma": sigma}
    if final:
        row["final"] = {"rating": sum(final) / 2, "low": final[0], "high": final[1]}
    return row


@pytest.mark.parametrize(
    ("top", "second", "clear"),
    [
        ((1600, 1700), (1400, 1500), True),  # apart
        ((1500, 1700), (1400, 1500), False),  # touching counts as overlap
        ((1450, 1700), (1400, 1500), False),  # overlap
        ((1400, 1500), (1600, 1700), False),  # apart, but the ordinal's top is the worse one
    ],
)
def test_a_leader_needs_a_final_interval_clear_of_the_second(top, second, clear):
    rows = [ranked("a", 20.0, final=top), ranked("b", 1.0, final=second)]
    assert clear_leader(rows) == ("a" if clear else None)


def test_finals_decide_even_when_the_ordinal_gap_says_otherwise():
    rows = [ranked("a", 5.1, final=(1600, 1700)), ranked("b", 5.0, final=(1400, 1500))]
    assert clear_leader(rows) == "a"


@pytest.mark.parametrize(
    ("gap", "sigmas", "clear"),
    [
        (7.3, (7.0, 7.2), True),
        (7.2, (7.0, 7.2), False),  # must exceed, not equal
        (7.1, (7.0, 7.2), False),  # beats only the smaller sigma
        (7.1, (7.2, 7.0), False),
    ],
)
def test_with_no_finals_the_ordinal_gap_must_exceed_both_sigmas(gap, sigmas, clear):
    rows = [ranked("a", 10.0 + gap, sigmas[0]), ranked("b", 10.0, sigmas[1])]
    assert clear_leader(rows) == ("a" if clear else None)


def test_a_rival_without_a_final_is_not_skipped_to_find_a_lead():
    # Liar's Dice after tournament 9: the bot's final is clear of the leader's, but the real
    # rival played a different tournament and has none.
    rows = [
        ranked("sonnet-5", 24.43, 3.46, final=(1533.3, 1657.6)),
        ranked("sonnet-5.5", 23.82, 3.65),
        ranked("bot", 19.31, 2.76, final=(1342.4, 1466.7)),
    ]
    assert clear_leader(rows) is None


def test_nobody_leads_alone_and_baselines_are_not_rivals():
    assert clear_leader([ranked("a", 20.0)]) is None
    assert clear_leader([ranked("a", 20.0), lb_row("random") | {"ordinal": 1.0}]) is None
    rows = [lb_row("random") | {"ordinal": 30.0}, ranked("a", 20.0), ranked("b", 5.0)]
    assert clear_leader(rows) == "a"


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


def seat_row(**over):
    row = {
        "player_id": "haiku-5.5/ww-v2-plain",
        "provider": "anthropic",
        "match_id": 9,
        "game": "werewolf",
        "seed": 1,
        "options": None,
        "scores": [1, 1, 0, 0, 0, 0, 0],
        "seat": 0,
        "turns": 10,
        "accepted": 9,
        "attempts": 12,
        "latency_ms": 2000,
        "cost_usd": 0.01,
        "input_tokens": 100,
        "cache_read_tokens": 300,
        "cache_write_tokens": 0,
    }
    return row | over


def player_context(**over):
    overall = summarise([seat_row()])
    context = {
        "player_id": "haiku-5.5/ww-v2-plain",
        "model": "haiku-5.5",
        "overall": overall,
        "games": [
            {"game": "werewolf", "place": 2, "field": 8, "ordinal": 3.1, **overall},
        ],
        "roles": {
            "wolf_games": 10,
            "wolf_win_rate": 0.9,
            "seer_games": 0,
            "seer_win_rate": None,
            "villager_games": 20,
            "villager_win_rate": 0.25,
            "true_claims": 3,
            "false_claims": 2,
            "leaks": 1,
            "forfeits": 0,
        },
        "recent": [
            {"id": 9, "game": "werewolf", "role": "wolf", "outcome": "won", "cost_usd": 0.01}
        ],
    }
    return context | over


@pytest.mark.parametrize(
    ("player_id", "slug"),
    [
        ("random2", "random2"),
        ("haiku-5.5/c4-v1", "haiku-5.5_2f_c4-v1"),
        ("a_b", "a_5f_b"),
        ("Haiku", "_48_aiku"),
    ],
)
def test_a_player_slug_is_one_safe_path_segment(player_id, slug):
    assert player_slug(player_id) == slug


def test_no_two_players_share_a_slug():
    ids = ["a/b", "a_2f_b", "A/b", "a b", "a%20b", "a.b", "é", "<x>"]
    assert len({player_slug(i) for i in ids}) == len(ids)


@pytest.mark.parametrize(
    ("value", "text"), [(None, "–"), (0.0, "0.0%"), (0.425, "42.5%"), (1.0, "100.0%")]
)
def test_pct(value, text):
    assert pct(value) == text


def test_wilson_matches_the_intervals_recorded_for_tournaments_6_and_7():
    assert [round(100 * x, 1) for x in wilson(17, 40)] == [28.5, 57.8]
    low, high = wilson(7, 40)
    assert round(100 * low) == 9 and round(100 * high) == 32


def test_wilson_stays_inside_zero_and_one():
    assert wilson(0, 5)[0] == 0.0 and wilson(5, 5)[1] == 1.0


def test_the_village_share_counts_finished_games_only():
    seven = list("abcdefg")
    v = list_row(id=1, game="werewolf", seats=seven, scores=[0, 0, 1, 1, 1, 1, 1])
    w = list_row(id=2, game="werewolf", seats=seven, scores=[1, 1, 0, 0, 0, 0, 0])
    live = list_row(id=3, game="werewolf", seats=seven, status="running")
    share = village_share([v, w, w, w, live])
    assert (share["won"], share["games"], share["share"]) == (1, 4, 0.25)
    assert village_share([live]) is None


def test_the_rule_change_panels_lead_with_the_percentage_and_its_uncertainty():
    seven = list("abcdefg")
    row = list_row(game="werewolf", seats=seven, scores=[0, 0, 1, 1, 1, 1, 1])
    lost = list_row(game="werewolf", seats=seven, scores=[1, 1, 0, 0, 0, 0, 0])
    both = [
        {"game": "werewolf", "matches": [row, lost, lost, lost]},
        {"game": "werewolf_dayfirst", "matches": [row]},
    ]
    html = render("index.html", URLS, boards={}, role_stats=[], stories=both)
    assert '<p class="headline">25.0%</p>' in html and "Village wins, 1 of 4." in html
    assert "95% interval 5% to 70%" in html


@pytest.mark.parametrize(
    ("game", "scores", "seat", "outcome"),
    [
        ("connect4", [1.0, 0.0], 0, "won"),
        ("connect4", [1.0, 0.0], 1, "lost"),
        ("connect4", [0.5, 0.5], 1, "drew"),
        ("liars_dice", [0, 1, 0], 1, "won"),
        ("werewolf", [1, 1, 0, 0, 0, 0, 0], 1, "won"),  # winners share a score
        ("werewolf_dayfirst", [0, 0, 1, 1, 1, 1, 1], 0, "lost"),
    ],
)
def test_a_seat_outcome_is_from_that_seats_point_of_view(game, scores, seat, outcome):
    assert seat_outcome(game, scores, seat) == outcome


def test_summarise_totals_a_players_seats():
    won = seat_row()
    lost = seat_row(seat=3, cost_usd=0.03, attempts=8, accepted=8, cache_read_tokens=100)
    s = summarise([won, lost])
    assert (s["games"], s["won"], s["lost"], s["win_rate"]) == (2, 1, 1, 0.5)
    assert s["cost_usd"] == pytest.approx(0.04) and s["cost_per_game"] == pytest.approx(0.02)
    assert s["illegal_rate"] == pytest.approx(3 / 20)  # as store/metrics.py defines it
    assert s["cache_hit"] == pytest.approx(400 / 600)
    assert s["latency_ms"] == 200


def test_summarise_has_no_rates_without_anything_to_measure():
    s = summarise([])
    assert s["games"] == 0
    assert s["win_rate"] is s["cost_per_game"] is s["illegal_rate"] is None
    assert s["cache_hit"] is s["latency_ms"] is None


def test_a_model_is_the_part_of_the_id_before_the_prompt():
    assert model_of("haiku-5.5/c4-v1") == "haiku-5.5" and model_of("random") == "random"


def test_every_leaderboard_and_seat_list_links_to_the_player():
    boards = {"connect4": [lb_row("haiku-5.5/c4-v1")]}
    index = render("index.html", URLS, boards=boards, role_stats=[])
    assert 'href="/players/haiku-5.5_2f_c4-v1"' in index
    assert 'href="/players/haiku"' in render("match.html", URLS, m=match())
    final = {"p/1": {"rating": 1700.0, "low": 1600.0, "high": 1800.0}}
    html = render("tournament.html", URLS, t=tournament(), matches=[], final=final)
    assert 'href="/players/p_2f_1"' in html


def test_the_player_page_shows_the_record_by_game_and_role():
    html = render("player.html", URLS, **player_context())
    assert "<title>haiku-5.5/ww-v2-plain · LLM Arena</title>" in html
    assert '<body class="g-werewolf">' in html and "Werewolf: 2 of 8" in html
    assert "90% of 10" in html and "– of 0" in html and "<dt>Bluffs</dt><dd>2</dd>" in html
    assert '<a href="/matches/9">#9</a>' in html and "<td>wolf</td>" in html
    assert 'data-result="Won"' in html and "Their cost" in html


def test_a_player_without_finished_games_still_has_a_page():
    context = player_context(overall=summarise([]), games=[], roles=None, recent=[])
    html = render("player.html", URLS, **context)
    assert "No finished games yet." in html and "<body>" in html


def test_a_seat_that_never_acted_shows_no_latency_instead_of_crashing():
    silent = summarise([seat_row(turns=0, accepted=0, attempts=0, latency_ms=0, cost_usd=0.0)])
    assert silent["latency_ms"] is None and silent["illegal_rate"] is None
    context = player_context(
        games=[{"game": "werewolf", "place": 2, "field": 8, "ordinal": 3.1, **silent}]
    )
    assert ">–<" in render("player.html", URLS, **context)
    row = {"model": "m", **silent}
    models = [{"model": "m", "players": [], **silent}]
    html = render("compare.html", URLS, models=models, games=[{"game": "werewolf", "rows": [row]}])
    assert ">–<" in html


def test_a_player_page_for_another_game_has_no_role_section():
    context = player_context(
        roles=None,
        recent=[{"id": 2, "game": "connect4", "role": None, "outcome": "drew", "cost_usd": 0.0}],
    )
    html = render("player.html", URLS, **context)
    assert "Werewolf by role" not in html and "<th>Role</th>" not in html and ">Drew<" in html


def test_the_comparison_lists_each_model_and_its_players():
    s = summarise([seat_row()])
    models = [{"model": "haiku-5.5", "players": ["haiku-5.5/ww-v2-plain"], **s}]
    games = [{"game": "werewolf", "rows": [{"model": "haiku-5.5", **s}]}]
    html = render("compare.html", URLS, models=models, games=games)
    assert "<h2>haiku-5.5</h2>" in html and 'href="/players/haiku-5.5_2f_ww-v2-plain"' in html
    assert 'class="game-tag g-werewolf"' in html and "100.0%" in html
    assert 'aria-current="page">Compare</a>' in html
    assert "No games with real models yet." in render("compare.html", URLS, models=[], games=[])


def test_tables_that_can_sort_say_so_and_match_rows_carry_their_filters():
    html = render("matches.html", URLS, matches=[list_row(status="running")])
    assert 'class="sortable filterable"' in html
    assert 'data-game="Connect 4" data-result="running"' in html
    assert "function initSorting" in html  # the script is inlined on every page


def test_the_match_page_has_a_timeline_strip_under_a_full_width_slider():
    html = render("match.html", URLS, m=match())
    assert '<div class="scrub">' in html
    assert html.index('id="replay-pos"') < html.index('id="replay-marks"')
    assert '<tr data-seat="1">' in html and '<span class="avatar">1</span>' in html
