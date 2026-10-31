"use strict";
// Sorting and filtering for any table marked up for it, on every page, plain JS. Inlined by
// base.html. Without JavaScript the tables stay readable, just fixed. Sorting is instant: a
// table reordered on click shouldn't animate. Loaded by Node in tests (module.exports).

// "$0.0123", "#116", "0.75 (4)" and "1620.4 (1500.1 – 1740.7)" sort by their leading number,
// and an empty cell or a dash has no value at all.
function sortValue(text) {
  const t = text.trim();
  if (t === "" || t === "–") return null;
  const n = t.match(/^[#$]?(-?\d+(?:\.\d+)?)/);
  return n ? Number(n[1]) : t.toLowerCase();
}

// Cells with no value stay last in both directions; numbers sort ahead of words.
function compareValues(a, b, direction) {
  if (a === null || b === null) return (a === null) - (b === null);
  if (typeof a !== typeof b) return typeof a === "number" ? -1 : 1;
  const order = typeof a === "number" ? a - b : a.localeCompare(b);
  return order * direction;
}

function initSorting(doc) {
  doc.querySelectorAll("table.sortable").forEach((table) => {
    const heads = [...table.tHead.rows[0].cells];
    heads.forEach((th, column) => {
      const button = doc.createElement("button");
      button.type = "button";
      button.className = "sort";
      button.textContent = th.textContent;
      th.textContent = "";
      th.append(button);
      button.onclick = () => {
        const rows = [...table.tBodies[0].rows];
        const values = new Map(rows.map((r) => [r, sortValue(r.cells[column].textContent)]));
        const numeric = [...values.values()].some((v) => typeof v === "number");
        const was = th.getAttribute("aria-sort");
        // Biggest first is what a number column is usually opened for, A to Z for words.
        const descending = was ? was === "ascending" : numeric;
        heads.forEach((h) => h.removeAttribute("aria-sort"));
        th.setAttribute("aria-sort", descending ? "descending" : "ascending");
        const direction = descending ? -1 : 1;
        rows.sort((a, b) => compareValues(values.get(a), values.get(b), direction));
        table.tBodies[0].append(...rows);
      };
    });
  });
}

// One row of chips per column worth filtering by: a single value has nothing to choose
// between, and a dozen would be noise. Chips combine, and the count says what's left.
const FACETS = ["game", "result"];
const MAX_CHIPS = 8;

function initFilters(doc) {
  doc.querySelectorAll("table.filterable").forEach((table) => {
    const rows = [...table.tBodies[0].rows];
    const chosen = {};
    const count = doc.createElement("span");
    count.className = "muted filter-count";
    const apply = () => {
      const shown = rows.filter((row) => {
        const on = Object.entries(chosen).every(([facet, value]) => row.dataset[facet] === value);
        row.hidden = !on;
        return on;
      });
      count.textContent = `${shown.length} of ${rows.length}`;
    };
    const bars = [];
    for (const facet of FACETS) {
      const values = [...new Set(rows.map((r) => r.dataset[facet]))].sort();
      if (values.length < 2 || values.length > MAX_CHIPS) continue;
      const bar = doc.createElement("div");
      bar.className = "filters";
      const chips = [null, ...values].map((value) => {
        const chip = doc.createElement("button");
        chip.type = "button";
        chip.textContent = value ?? "All";
        chip.setAttribute("aria-pressed", String(value === null));
        chip.onclick = () => {
          if (value === null) delete chosen[facet];
          else chosen[facet] = value;
          chips.forEach((c) => c.setAttribute("aria-pressed", String(c === chip)));
          apply();
        };
        return chip;
      });
      bar.append(...chips);
      bars.push(bar);
    }
    if (!bars.length) return;
    bars[bars.length - 1].append(count);
    apply();
    const panel = table.closest(".wrap");
    bars.forEach((bar) => panel.before(bar));
  });
}

if (typeof document !== "undefined") {
  initSorting(document);
  initFilters(document);
}

if (typeof module !== "undefined") module.exports = { sortValue, compareValues, initSorting, initFilters };
