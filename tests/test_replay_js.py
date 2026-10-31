"""The replay page's JS (arena/templates/replay.js), run in Node against real engine
frames. Skipped when Node isn't installed; it isn't a project dependency."""

import copy
import json
import os
import random
import shutil
import subprocess

import pytest

from arena.games.connect4 import Connect4
from arena.games.liars_dice import LiarsDice
from arena.games.werewolf import Werewolf, WerewolfDayFirst
from arena.web import REPLAY_JS

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

RENDER = """
const R = require(process.env.REPLAY_JS);
let input = "";
process.stdin.on("data", (d) => (input += d));
process.stdin.on("end", () => {
  const p = JSON.parse(input);
  console.log(JSON.stringify({
    frames: p.frames.map((f) => R.renderFrame(p.game, f, p.seats)),
    turns: p.turns.map((t) => R.describeTurn(t, p.seats)),
  }));
});
"""

EVIL = "<b>evil</b>"


def node(script: str, payload: dict) -> dict:
    out = subprocess.run(
        ["node", "-e", script],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env={**os.environ, "REPLAY_JS": str(REPLAY_JS)},
        check=True,
    )
    return json.loads(out.stdout)


def random_game(game, n_players: int, seed: int):
    rng = random.Random(seed)
    s = game.initial_state(n_players, seed)
    frames = [game.spectator_view(s)]
    while not game.is_terminal(s):
        seat = game.current_player(s)
        s = game.apply(s, seat, rng.choice(game.legal_actions_hint(s, seat)))
        frames.append(game.spectator_view(s))
    return frames


def test_connect4_frames_render_a_board_and_the_result():
    frames = random_game(Connect4(), 2, 4)
    out = node(RENDER, {"game": "connect4", "seats": [EVIL, "o"], "frames": frames, "turns": []})
    first, final = out["frames"][0], out["frames"][-1]
    assert first.count('class="cell"') == 42 and "Move 1:" in first
    assert final.count("disc X") + final.count("disc O") == len(frames) - 1
    assert " wins" in final or "Draw" in final
    assert all(EVIL not in h and "&lt;b&gt;evil&lt;/b&gt;" in h for h in out["frames"][:1])


def test_liars_dice_frames_show_dice_bids_reveals_and_the_winner():
    frames = random_game(LiarsDice(), 3, 2)
    seats = [EVIL, "b", "c"]
    out = node(RENDER, {"game": "liars_dice", "seats": seats, "frames": frames, "turns": []})
    html = out["frames"]
    assert html[0].count('class="die') == 15 and "no bids yet this round" in html[0]
    with_bids = next(i for i, f in enumerate(frames) if f["bids"])
    assert "Bids: " in html[with_bids] and ' × <span class="die">' in html[with_bids]
    with_reveal = next(i for i, f in enumerate(frames) if f["last_reveal"])
    assert "Revealed:" in html[with_reveal] and "lost a die" in html[with_reveal]
    winner = seats[frames[-1]["winner"]].replace("<", "&lt;").replace(">", "&gt;")
    assert f"{winner} wins" in html[-1]
    assert not any(EVIL in h for h in html)


def test_unknown_game_falls_back_to_the_raw_frame_and_turns_are_described():
    turns = [
        {"turn_no": 0, "seat": 0, "action": {"column": 3}, "forfeited": False,
         "attempts": 2, "error": "column 9 is out of range"},
        {"turn_no": 1, "seat": 1, "action": None, "forfeited": True, "attempts": 3, "error": None},
        {"turn_no": 2, "seat": 0, "action": {"column": 1}, "forfeited": False,
         "attempts": 1, "error": None, "label": "column 1"},
    ]  # fmt: skip
    out = node(RENDER, {"game": "chess", "seats": [EVIL, "b"], "frames": [{"x": EVIL}],
                        "turns": turns})  # fmt: skip
    assert out["frames"][0].startswith("<pre>") and EVIL not in out["frames"][0]
    assert "Turn 0: &lt;b&gt;evil&lt;/b&gt;" in out["turns"][0]
    assert "last rejected: column 9 is out of range" in out["turns"][0]
    assert out["turns"][1] == "Turn 1: b → forfeit after 3 invalid attempts"
    assert out["turns"][2] == "Turn 2: &lt;b&gt;evil&lt;/b&gt; → column 1"  # server label wins


STEP = """
const R = require(process.env.REPLAY_JS);
let input = "";
process.stdin.on("data", (d) => (input += d));
process.stdin.on("end", () => {
  const data = JSON.parse(input);
  const els = {};
  const el = (id) => (els[id] ??= { innerHTML: "", textContent: "", value: "0", max: "0" });
  const rows = [1, 2, 3].map((n) => ({
    dataset: { frame: String(n) }, current: false,
    classList: { toggle(c, on) { rows[n - 1].current = on; } },
  }));
  let onKey;
  const doc = {
    getElementById: (id) => (id === "replay-data" ? { textContent: JSON.stringify(data) } : el(id)),
    querySelectorAll: () => rows,
    addEventListener: (_t, h) => (onKey = h),
  };
  R.initReplay(doc);
  const log = [];
  const snap = (what) => log.push([what, el("replay-label").textContent,
                                   rows.filter((r) => r.current).map((r) => r.dataset.frame)]);
  snap("init");
  el("replay-prev").onclick(); snap("prev");
  onKey({ key: "ArrowLeft" }); snap("left");
  el("replay-first").onclick(); snap("first");
  el("replay-prev").onclick(); snap("prev at start");
  rows[1].onclick(); snap("row 2");
  el("replay-pos").value = "1"; el("replay-pos").oninput(); snap("slider 1");
  el("replay-last").onclick(); el("replay-next").onclick(); snap("next at end");
  el("replay-play").onclick(); log.push(["play", el("replay-play").textContent]);
  el("replay-play").onclick(); log.push(["pause", el("replay-play").textContent]);
  const info = el("replay-info").innerHTML;
  console.log(JSON.stringify({ log, info, max: el("replay-pos").max }));
});
"""


def test_stepping_controls_move_through_frames_and_highlight_turn_rows():
    frames = random_game(Connect4(), 2, 1)[:4]  # 3 turns -> 4 frames
    turns = [{"turn_no": i, "seat": i % 2, "action": {"column": 0}, "forfeited": False,
              "attempts": 1, "error": None} for i in range(3)]  # fmt: skip
    out = node(STEP, {"game": "connect4", "seats": ["a", "b"], "frames": frames, "turns": turns})
    assert out["max"] == "3"
    assert out["log"] == [
        ["init", "3 / 3", ["3"]],
        ["prev", "2 / 3", ["2"]],
        ["left", "1 / 3", ["1"]],
        ["first", "0 / 3", []],
        ["prev at start", "0 / 3", []],
        ["row 2", "2 / 3", ["2"]],
        ["slider 1", "1 / 3", ["1"]],
        ["next at end", "3 / 3", ["3"]],
        ["play", "❚❚ Pause"],  # at the end, play restarts from frame 0
        ["pause", "▶ Play"],
    ]
    assert out["info"] == "Start of the match"  # play restarted from frame 0, then paused


def werewolf_frames(seed: int, game=None):
    game = game or Werewolf()
    rng = random.Random(seed)
    s = game.initial_state(7, seed)
    frames = [game.spectator_view(s)]
    while not game.is_terminal(s):
        seat = game.current_player(s)
        hint = game.legal_actions_hint(s, seat)
        action = rng.choice(hint) if hint else {"action": "say", "message": f"{EVIL} & hello"}
        s = game.apply(s, seat, action)
        frames.append(game.spectator_view(s))
    return frames


def test_werewolf_frames_render_roles_talk_and_the_result_escaped():
    frames = werewolf_frames(4)
    seats = [EVIL] + [f"p{i}" for i in range(1, 7)]
    turn = {"turn_no": 3, "seat": 0, "action": {}, "label": "says: hi", "truncated": True}
    turn |= {"forfeited": False, "attempts": 1, "error": None}
    out = node(RENDER, {"game": "werewolf", "frames": frames, "seats": seats, "turns": [turn]})
    html = "".join(out["frames"])
    assert "<b>evil</b>" not in html and "&lt;b&gt;evil&lt;/b&gt;" in html
    assert "villager" in html and "Night 1" in html and "discussion round" in html
    assert "killed at night" in html
    assert " win" in out["frames"][-1]
    assert "(truncated)" in out["turns"][0]


def test_day_first_frames_open_on_day_one_with_the_werewolf_renderer():
    frames = werewolf_frames(4, WerewolfDayFirst())
    seats = [f"p{i}" for i in range(7)]
    out = node(RENDER, {"game": "werewolf_dayfirst", "frames": frames, "seats": seats, "turns": []})
    assert "Day 1" in out["frames"][0] and "discussion round" in out["frames"][0]
    assert "Night 1" not in "".join(out["frames"]) and "Night 2" in "".join(out["frames"])
    assert " win" in out["frames"][-1]


ON_LOAD = """
// A browser runs the file top to bottom, so build the page first and require it after.
const els = {};
const el = (id) => (els[id] ??= { innerHTML: "", textContent: "", value: "0", max: "0" });
let input = "";
process.stdin.on("data", (d) => (input += d));
process.stdin.on("end", () => {
  els["replay-data"] = { textContent: input };
  globalThis.document = {
    getElementById: (id) => (id === "blind-start" ? null : el(id)),
    querySelectorAll: () => [],
    addEventListener: () => {},
  };
  require(process.env.REPLAY_JS);
  console.log(JSON.stringify({ html: el("replay-frame").innerHTML }));
});
"""


@pytest.mark.parametrize("game", [Werewolf(), WerewolfDayFirst()], ids=lambda g: g.name)
def test_the_first_paint_uses_the_game_renderer_not_the_raw_json_fallback(game):
    frames = werewolf_frames(4, game)
    seats = [f"p{i}" for i in range(7)]
    out = node(ON_LOAD, {"game": game.name, "seats": seats, "frames": frames, "turns": []})
    assert "<pre>" not in out["html"] and "<table" in out["html"]


BLIND = """
const R = require(process.env.REPLAY_JS);
let input = "";
process.stdin.on("data", (d) => (input += d));
process.stdin.on("end", () => {
  const p = JSON.parse(input);
  const steps = R.blindSteps(p.frames, p.turns);
  console.log(JSON.stringify({
    kinds: steps.map((s) => s.kind),
    html: steps.filter((s) => s.kind !== "end").map((s) => R.renderBlind(s)),
    steps,
  }));
});
"""

SEATS = [f"model-{i}" for i in range(7)]


def played(game, seed):
    rng = random.Random(seed)
    s = game.initial_state(7, seed)
    frames, turns = [game.spectator_view(s)], []
    while not game.is_terminal(s):
        seat = game.current_player(s)
        hint = game.legal_actions_hint(s, seat)
        action = rng.choice(hint) if hint else {"action": "say", "message": f"seat {seat} speaks"}
        turns.append({"turn_no": len(turns), "seat": seat, "action": action})
        s = game.apply(s, seat, action)
        frames.append(game.spectator_view(s))
    return frames, turns


def scrubbed(frames, turns):
    """Everything a villager can't know, destroyed. Dead seats' roles are public."""
    gone = copy.deepcopy(frames)
    for f in gone:
        f["roles"] = ["SECRET"] * 7
        f["wolf_votes"], f["inspections"], f["votes"] = [], [], []
        if f["phase"] == "night":
            f["to_move"] = None
    hidden = copy.deepcopy(turns)
    for t in hidden:
        if t["action"]["action"] in ("kill", "inspect"):
            t["action"] = {"action": "night"}
    return gone, hidden


@pytest.mark.parametrize("game", [Werewolf(), WerewolfDayFirst()], ids=lambda g: g.name)
@pytest.mark.parametrize("seed", range(8))
def test_blind_steps_depend_on_nothing_a_villager_cannot_know(game, seed):
    frames, turns = played(game, seed)
    seen = node(BLIND, {"frames": frames, "turns": turns})
    gone, hidden = scrubbed(frames, turns)
    assert seen == node(BLIND, {"frames": gone, "turns": hidden})
    assert seen["kinds"][-1] == "end" and "talk" in seen["kinds"]


@pytest.mark.parametrize("seed", range(4))
def test_day_votes_stay_hidden_until_the_pick_is_locked(seed):
    frames, turns = played(WerewolfDayFirst(), seed)
    steps = node(BLIND, {"frames": frames, "turns": turns})["steps"]
    for i, step in enumerate(steps):
        if step["kind"] == "vote":
            assert "votes" not in step and steps[i + 1]["kind"] == "result"
            assert all(d["day"] < step["day"] or d["cause"] == "night" for d in step["dead"])


def test_a_night_kill_is_announced_when_the_day_opens():
    frames, turns = played(Werewolf(), 1)
    seen = node(BLIND, {"frames": frames, "turns": turns})
    victim = seen["steps"][0]["dead"][0]
    expected = f"Player {victim['seat']} was killed in the night: a {victim['role']}."
    assert expected in seen["html"][0]
    assert expected not in seen["html"][1 + seen["kinds"].index("vote")]


def test_day_first_opens_with_everyone_alive_and_the_original_with_a_death():
    frames, turns = played(WerewolfDayFirst(), 1)
    first = node(BLIND, {"frames": frames, "turns": turns})["steps"][0]
    assert first["alive"] == list(range(7)) and first["dead"] == []
    frames, turns = played(Werewolf(), 1)
    night = node(BLIND, {"frames": frames, "turns": turns})["steps"][0]
    assert len(night["alive"]) == 6 and night["dead"][0]["cause"] == "night"


SCORE = """
const R = require(process.env.REPLAY_JS);
let input = "";
process.stdin.on("data", (d) => (input += d));
process.stdin.on("end", () => {
  const p = JSON.parse(input);
  const picks = Object.fromEntries(Object.entries(p.picks).map(([d, s]) => [d, new Set(s)]));
  console.log(JSON.stringify(R.blindScore(p.frames, p.turns, picks)));
});
"""


def test_the_score_counts_your_picks_and_the_villages_votes_not_the_wolves():
    frames = [
        {"day": 1, "phase": "vote", "roles": ["wolf", "wolf", "seer", "villager"]},
        {"day": 1, "phase": "vote", "roles": ["wolf", "wolf", "seer", "villager"]},
        {"day": 1, "phase": "vote", "roles": ["wolf", "wolf", "seer", "villager"]},
        {"day": 2, "phase": "night", "roles": ["wolf", "wolf", "seer", "villager"]},
    ]
    turns = [
        {"turn_no": 0, "seat": 0, "action": {"action": "vote", "target": 3}},
        {"turn_no": 1, "seat": 2, "action": {"action": "vote", "target": 1}},
        {"turn_no": 2, "seat": 3, "action": {"action": "vote", "target": 2}},
    ]
    out = node(SCORE, {"frames": frames, "turns": turns, "picks": {"1": [0, 3]}})
    assert out == [{"day": 1, "hits": 1, "picked": 2, "villageHits": 1, "villageVotes": 2}]


PLAY = """
const R = require(process.env.REPLAY_JS);
let input = "";
process.stdin.on("data", (d) => (input += d));
process.stdin.on("end", () => {
  const data = JSON.parse(input);
  const els = {};
  const el = (id) => (els[id] ??= { innerHTML: "", hidden: false, disabled: false });
  const classes = new Set();
  const classList = {
    add: (c) => classes.add(c),
    remove: (c) => classes.delete(c),
    contains: (c) => classes.has(c),
  };
  const doc = { getElementById: el, documentElement: { classList } };
  R.initBlind(doc, data);
  el("blind-start").onclick();
  const pick = (seat) => {
    const dataset = { pick: String(seat) };
    el("blind-view").onclick({ target: { closest: () => ({ dataset }) } });
  };
  const early = [];
  let refusedOverPick = true, lockedOut = 0;
  for (let n = 0; n < 500 && !el("blind-next").hidden; n++) {
    early.push(el("blind-view").innerHTML);
    const found = el("blind-view").innerHTML.matchAll(/data-pick="(\\d+)"/g);
    const buttons = [...found].map((m) => Number(m[1]));
    if (buttons.length) {
      if (el("blind-next").disabled) lockedOut++;
      for (const s of buttons) pick(s); // only as many as there are wolves left should count
      refusedOverPick &&= (el("blind-view").innerHTML.match(/seat on/g) || []).length <= 2;
    }
    el("blind-next").onclick();
  }
  const end = el("blind-view").innerHTML;
  el("blind-exit").onclick();
  const stillBlind = classes.has("blind");
  const report = { early: early.join(""), end, lockedOut, refusedOverPick, stillBlind };
  console.log(JSON.stringify(report));
});
"""


def test_playing_blind_from_start_to_reveal():
    frames, turns = played(WerewolfDayFirst(), 3)
    out = node(
        PLAY, {"game": "werewolf_dayfirst", "seats": SEATS, "frames": frames, "turns": turns}
    )
    assert "model-" not in out["early"]  # names only come with the reveal
    assert out["lockedOut"] >= 1 and out["refusedOverPick"]  # no lock-in without a full pick
    assert " win" in out["end"] and all(name in out["end"] for name in SEATS)
    assert "picks were wolves" in out["end"] and "votes on a wolf" in out["end"]
    assert out["stillBlind"] is False


LIVE = """
const R = require(process.env.REPLAY_JS);
let input = "";
process.stdin.on("data", (d) => (input += d));
process.stdin.on("end", () => {
  const { data, events } = JSON.parse(input);
  const els = {};
  const el = (id) => (els[id] ??= { innerHTML: "", textContent: "", value: "0", max: "0" });
  const doc = {
    getElementById: (id) => (id === "replay-data" ? { textContent: JSON.stringify(data) } : el(id)),
    querySelectorAll: () => [],
    addEventListener: () => {},
  };
  const opened = [];
  globalThis.EventSource = class {
    constructor(url) { this.url = url; this.on = {}; this.closed = false; opened.push(this); }
    addEventListener(type, handler) { this.on[type] = handler; }
    close() { this.closed = true; }
  };
  R.initReplay(doc);
  const log = [];
  const label = () => el("replay-label").textContent;
  const snap = (what) => log.push([what, label(), el("replay-live").textContent]);
  snap("init");
  const stream = opened[0];
  for (const [type, payload] of events) {
    if (type === "back") el("replay-first").onclick();
    else if (stream) stream.on[type]({ data: JSON.stringify(payload) });
    snap(type);
  }
  console.log(JSON.stringify({ log, urls: opened.map((s) => s.url), closed: stream?.closed,
                               max: el("replay-pos").max, info: el("replay-info").innerHTML }));
});
"""


def live_payload(events_url):
    frames = random_game(Connect4(), 2, 1)[:5]
    turns = [{"turn_no": i, "seat": i % 2, "action": {"column": 0}, "forfeited": False,
              "attempts": 1, "error": None, "label": f"column {i}"} for i in range(4)]  # fmt: skip
    data = {"game": "connect4", "seats": ["a", "b"], "frames": frames[:3], "turns": turns[:2]}
    data["events"] = events_url
    new = [["turn", {"turn": turns[i], "frame": frames[i + 1]}] for i in (2, 3)]
    return data, new


def test_live_turns_append_and_follow_the_newest_frame_then_done_closes_the_stream():
    data, new = live_payload("/api/matches/3/events")
    payload = {"data": data, "events": [*new, ["done", {"status": "completed"}]]}
    out = node(LIVE, payload)
    assert out["urls"] == ["/api/matches/3/events?after=2"]
    assert out["log"] == [
        ["init", "2 / 2", "● Live"],
        ["turn", "3 / 3", "● Live"],
        ["turn", "4 / 4", "● Live"],
        ["done", "4 / 4", "Match completed: reload for the turn table"],
    ]
    assert out["closed"] is True and out["max"] == "4"
    assert out["info"] == "Turn 3: b → column 3"


def test_a_viewer_stepping_through_history_stays_put_as_turns_arrive():
    data, new = live_payload("/api/matches/3/events")
    out = node(LIVE, {"data": data, "events": [["back", None], *new]})
    assert [row[1] for row in out["log"]] == ["2 / 2", "0 / 2", "0 / 3", "0 / 4"]
    assert out["max"] == "4"


def test_a_turn_seen_twice_after_a_reconnect_is_ignored():
    data, new = live_payload("/api/matches/3/events")
    out = node(LIVE, {"data": data, "events": [new[0], new[0], new[1]]})
    assert [row[1] for row in out["log"]] == ["2 / 2", "3 / 3", "3 / 3", "4 / 4"]


def test_a_page_with_no_stream_never_connects():
    data, _ = live_payload("")
    out = node(LIVE, {"data": data, "events": []})
    assert out["urls"] == [] and out["log"] == [["init", "2 / 2", ""]]
