"""The replay page's JS (arena/templates/replay.js), run in Node against real engine
frames. Skipped when Node isn't installed; it isn't a project dependency."""

import json
import os
import random
import shutil
import subprocess

import pytest

from arena.games.connect4 import Connect4
from arena.games.liars_dice import LiarsDice
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
    out = node(RENDER, {"game": "werewolf", "seats": [EVIL, "b"], "frames": [{"x": EVIL}],
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
