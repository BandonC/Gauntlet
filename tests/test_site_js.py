"""Table sorting and filtering (arena/templates/site.js), run in Node on a small fake DOM.
Skipped when Node isn't installed; it isn't a project dependency."""

import json
import os
import shutil
import subprocess

import pytest

from arena.web import SITE_JS

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

FAKE_DOM = """
const S = require(process.env.SITE_JS);
const node = (tag) => ({
  tag, attrs: {}, children: [], dataset: {}, hidden: false, textContent: "",
  setAttribute(k, v) { this.attrs[k] = v; },
  getAttribute(k) { return this.attrs[k] ?? null; },
  removeAttribute(k) { delete this.attrs[k]; },
  append(...kids) { this.children.push(...kids); },
});
const table = (headers, rows, data = []) => {
  const heads = headers.map((h) => Object.assign(node("th"), { textContent: h }));
  const body = rows.map((cells, i) => Object.assign(node("tr"), {
    cells: cells.map((c) => ({ textContent: c })), dataset: data[i] ?? {},
  }));
  const tbody = {
    get rows() { return body; },
    append: (...r) => body.splice(0, body.length, ...r),
  };
  const bars = [];
  const wrap = { before: (bar) => bars.push(bar) };
  const tHead = { rows: [{ cells: heads }] };
  return { tHead, tBodies: [tbody], closest: () => wrap, bars, heads };
};
const doc = (t) => ({ createElement: node, querySelectorAll: () => [t] });
let input = "";
process.stdin.on("data", (d) => (input += d));
"""

SORT = (
    FAKE_DOM
    + """
process.stdin.on("end", () => {
  const p = JSON.parse(input);
  const t = table(p.headers, p.rows);
  S.initSorting(doc(t));
  const column = () => t.tBodies[0].rows.map((r) => r.cells[p.column].textContent);
  const button = t.heads[p.column].children[0];
  button.onclick();
  const first = { order: column(), aria: t.heads[p.column].attrs["aria-sort"] };
  button.onclick();
  const second = { order: column(), aria: t.heads[p.column].attrs["aria-sort"] };
  console.log(JSON.stringify({ first, second }));
});
"""
)

FILTER = (
    FAKE_DOM
    + """
process.stdin.on("end", () => {
  const p = JSON.parse(input);
  const t = table(["ID"], p.rows.map((_, i) => [String(i)]), p.rows);
  S.initFilters(doc(t));
  const shown = () => t.tBodies[0].rows.filter((r) => !r.hidden).length;
  const chips = t.bars.map((b) => b.children.filter((c) => c.tag === "button"));
  const labels = chips.map((c) => c.map((b) => b.textContent));
  const log = [];
  for (const [bar, chip] of p.clicks) {
    chips[bar][chip].onclick();
    const pressed = chips[bar].map((b) => b.attrs["aria-pressed"]);
    const count = t.bars.at(-1).children.at(-1).textContent;
    log.push({ shown: shown(), pressed, count });
  }
  console.log(JSON.stringify({ labels, log }));
});
"""
)


def node(script: str, payload: dict) -> dict:
    out = subprocess.run(
        ["node", "-e", script],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env={**os.environ, "SITE_JS": str(SITE_JS)},
        check=True,
    )
    return json.loads(out.stdout)


def sort(cells: list[str]) -> dict:
    rows = [[c] for c in cells]
    return node(SORT, {"headers": ["x"], "rows": rows, "column": 0})


def test_numbers_open_biggest_first_and_a_second_click_reverses():
    out = sort(["2", "10", "1.5"])
    assert out["first"] == {"order": ["10", "2", "1.5"], "aria": "descending"}
    assert out["second"] == {"order": ["1.5", "2", "10"], "aria": "ascending"}


def test_words_open_a_to_z():
    out = sort(["beta", "Alpha", "gamma"])
    assert out["first"] == {"order": ["Alpha", "beta", "gamma"], "aria": "ascending"}


@pytest.mark.parametrize(
    "cells",
    [
        ["$0.0030", "$0.1200", "$0.0400"],
        ["#3", "#120", "#40"],
        ["0.3 (4)", "12.5 (1)", "0.75 (8)"],
        ["1500.1 (1400 – 1600)", "1620.4 (1500.1 – 1740.7)", "1510 (1 – 2)"],
        ["-1.06", "5.2", "0.31"],
    ],
)
def test_money_ids_and_annotated_numbers_sort_as_numbers(cells):
    assert (
        sort(cells)["first"]["order"][0]
        == {
            "$": "$0.1200",
            "#": "#120",
            "0": "12.5 (1)",
            "1": "1620.4 (1500.1 – 1740.7)",
            "-": "5.2",
        }[cells[0][0]]
    )


def test_a_cell_with_no_value_stays_last_both_ways():
    out = sort(["1", "–", "3", ""])
    assert out["first"]["order"][:2] == ["3", "1"] and out["second"]["order"][:2] == ["1", "3"]


FILTER_ROWS = [
    {"game": "Werewolf", "result": "Wolves"},
    {"game": "Werewolf", "result": "Village"},
    {"game": "Connect 4", "result": "Wolves"},
]


def test_filters_combine_count_what_is_left_and_all_clears_them():
    out = node(FILTER, {"rows": FILTER_ROWS, "clicks": [[0, 2], [1, 2], [1, 0], [0, 0]]})
    assert out["labels"] == [
        ["All", "Connect 4", "Werewolf"],
        ["All", "Village", "Wolves"],
    ]
    assert [(s["shown"], s["count"]) for s in out["log"]] == [
        (2, "2 of 3"),
        (1, "1 of 3"),
        (2, "2 of 3"),
        (3, "3 of 3"),
    ]
    assert out["log"][0]["pressed"] == ["false", "false", "true"]
    assert out["log"][3]["pressed"] == ["true", "false", "false"]


def test_a_filter_needs_a_real_choice():
    assert node(FILTER, {"rows": [FILTER_ROWS[0]] * 3, "clicks": []})["labels"] == []
    many = [{"game": "g", "result": f"r{i}"} for i in range(9)]
    assert node(FILTER, {"rows": many, "clicks": []})["labels"] == []


def test_the_inlined_script_cannot_close_its_own_tag():
    assert "</script" not in SITE_JS.read_text("utf-8").lower()
