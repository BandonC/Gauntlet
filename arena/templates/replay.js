"use strict";
// Replay stepping for the match page, plain JS, no framework. Data comes from the
// embedded #replay-data JSON: frames[i] is the spectator_view before turn i, and
// frames[turns.length] is the final state. One small renderer per game turns a frame
// into HTML; everything from the data is escaped. Loaded by Node in tests (module.exports).

const esc = (s) =>
  String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const DIE = ["", "⚀", "⚁", "⚂", "⚃", "⚄", "⚅"];
const name = (seats, i) => esc(seats[i] ?? `seat ${i}`);

const RENDERERS = {
  connect4(f, seats) {
    // Seat 0 plays X and moves first.
    const who = (sym) => `${name(seats, sym === "X" ? 0 : 1)} (${sym})`;
    const cells = f.board
      .map((row) => [...row].map((c) => `<span class="cell${c === "." ? "" : " disc " + c}"></span>`).join(""))
      .join("");
    let status;
    if (f.winner) status = `${who(f.winner)} wins${f.forfeited_by ? ` (${who(f.forfeited_by)} forfeited)` : ""}`;
    else if (f.draw) status = "Draw";
    else status = `Move ${f.move_number}: ${who(f.to_move)} to play`;
    return `<div class="c4">${cells}</div><p class="status">${status}</p>`;
  },

  liars_dice(f, seats) {
    const over = f.winner !== null;
    const last = f.bids.length ? f.bids[f.bids.length - 1] : null;
    const dice = (d, match) =>
      d.map((x) => `<span class="die${x === match ? " hit" : ""}">${DIE[x]}</span>`).join("");
    const rows = f.dice_counts
      .map((n, i) => {
        const cls = !over && i === f.to_move ? ' class="turn"' : "";
        const hand = over || n === 0 ? `<span class="muted">${n === 0 ? "out" : ""}</span>` : dice(f.dice[i], last && last.face);
        return `<tr${cls}><td>${name(seats, i)}</td><td class="dice">${hand}</td><td class="num">${n}</td></tr>`;
      })
      .join("");
    const bids = f.bids.length
      ? f.bids.map((b) => `${name(seats, b.seat)}: ${b.quantity} × <span class="die">${DIE[b.face]}</span>`).join(" → ")
      : '<span class="muted">no bids yet this round</span>';
    let reveal = "";
    const r = f.last_reveal;
    if (r) {
      const shown = r.dice.map((d, i) => `${name(seats, i)} ${dice(d, r.face) || "–"}`).join("; ");
      reveal =
        `<p class="reveal">Round ${r.round}: ${name(seats, r.challenger)} challenged ` +
        `${name(seats, r.bidder)}'s ${r.quantity} × <span class="die">${DIE[r.face]}</span>. Revealed: ${shown}. ` +
        `${r.actual} on the table, so ${name(seats, r.loser)} lost a die.</p>`;
    }
    const status = over
      ? `${name(seats, f.winner)} wins${f.forfeited.length ? " (by forfeit)" : ""}`
      : `Round ${f.round}, ${f.total_dice} dice in play: ${name(seats, f.to_move)} to act`;
    return (
      `<table class="ld"><thead><tr><th>Player</th><th>Dice</th><th class="num">Left</th></tr></thead>` +
      `<tbody>${rows}</tbody></table><p class="ld-bids">Bids: ${bids}</p>${reveal}<p class="status">${status}</p>`
    );
  },

  werewolf(f, seats) {
    const dead = new Map(f.deaths.map((d) => [d.seat, d]));
    const fate = { night: "killed at night", vote: "voted out", forfeit: "forfeited" };
    const rows = f.roles
      .map((role, i) => {
        const cls = i === f.to_move ? ' class="turn"' : "";
        const d = dead.get(i);
        const status = d ? `<span class="muted">${esc(fate[d.cause])}, day ${d.day}</span>` : "alive";
        return `<tr${cls}><td>${name(seats, i)}</td><td>${esc(role)}</td><td>${status}</td></tr>`;
      })
      .join("");
    const said = f.messages
      .map((m) => `<li>${name(seats, m.seat)} (round ${m.round}): ${esc(m.text)}</li>`)
      .join("");
    const pick = (v) => `${name(seats, v.seat)} → ${name(seats, v.target)}`;
    const line = (label, items) => (items.length ? `<p class="ld-bids">${label}: ${items.join(", ")}</p>` : "");
    const found = f.inspections.map((i) => `night ${i.night}: ${name(seats, i.seat)} ${i.wolf ? "is a wolf" : "is not a wolf"}`);
    let status;
    if (f.winner) status = `The ${esc(f.winner)} win${f.winner === "wolves" ? "" : "s"}`;
    else if (f.phase === "night") status = `Night ${f.day}: ${name(seats, f.to_move)} to act`;
    else if (f.phase === "vote") status = `Day ${f.day} vote: ${name(seats, f.to_move)} to vote`;
    else status = `Day ${f.day}, discussion round ${f.discussion_round}: ${name(seats, f.to_move)} to speak`;
    return (
      `<table class="ld"><thead><tr><th>Player</th><th>Role</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table>` +
      (said ? `<ul>${said}</ul>` : "") +
      line("Wolf votes tonight", f.wolf_votes.map(pick)) +
      line("Day votes", f.votes.map(pick)) +
      line("Seer", found) +
      `<p class="status">${status}</p>`
    );
  },
};

const fallback = (f) => `<pre>${esc(JSON.stringify(f, null, 1))}</pre>`;

function renderFrame(game, frame, seats) {
  return (RENDERERS[game] || fallback)(frame, seats);
}

function describeTurn(t, seats) {
  if (!t) return "Start of the match";
  const why = t.error ? `; last rejected: ${t.error}` : "";
  let move;
  if (t.forfeited) move = `forfeit after ${t.attempts} invalid attempts${why}`;
  else {
    move = (t.label ?? JSON.stringify(t.action)) + (t.attempts > 1 ? ` (attempt ${t.attempts}${why})` : "");
    if (t.truncated) move += " (truncated)";
  }
  return `Turn ${t.turn_no}: ${name(seats, t.seat)} → ${esc(move)}`;
}

function initReplay(doc) {
  const el = doc.getElementById("replay-data");
  if (!el) return;
  const data = JSON.parse(el.textContent);
  const last = data.frames.length - 1;
  const $ = (id) => doc.getElementById(id);
  const slider = $("replay-pos");
  slider.max = String(last);
  let idx = last;
  let timer = null;

  function show(i) {
    idx = Math.max(0, Math.min(last, i));
    $("replay-frame").innerHTML = renderFrame(data.game, data.frames[idx], data.seats);
    $("replay-info").innerHTML = describeTurn(data.turns[idx - 1], data.seats);
    $("replay-label").textContent = `${idx} / ${last}`;
    slider.value = String(idx);
    doc.querySelectorAll("tr[data-frame]").forEach((tr) => {
      tr.classList.toggle("current", Number(tr.dataset.frame) === idx);
    });
    if (idx === last) stop();
  }
  function stop() {
    clearInterval(timer);
    timer = null;
    $("replay-play").textContent = "▶ Play";
  }
  function play() {
    if (timer) return stop();
    if (idx === last) show(0);
    $("replay-play").textContent = "❚❚ Pause";
    timer = setInterval(() => show(idx + 1), 700);
  }

  $("replay-first").onclick = () => { stop(); show(0); };
  $("replay-prev").onclick = () => { stop(); show(idx - 1); };
  $("replay-next").onclick = () => { stop(); show(idx + 1); };
  $("replay-last").onclick = () => { stop(); show(last); };
  $("replay-play").onclick = play;
  slider.oninput = () => { stop(); show(Number(slider.value)); };
  doc.querySelectorAll("tr[data-frame]").forEach((tr) => {
    tr.onclick = () => { stop(); show(Number(tr.dataset.frame)); };
  });
  doc.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") { stop(); show(idx - 1); }
    if (e.key === "ArrowRight") { stop(); show(idx + 1); }
  });
  show(last);
}

if (typeof document !== "undefined") initReplay(document);
if (typeof module !== "undefined") module.exports = { RENDERERS, renderFrame, describeTurn, esc, initReplay };
