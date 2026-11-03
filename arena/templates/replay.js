"use strict";
// Replay stepping for the match page, plain JS, no framework. Data comes from the
// embedded #replay-data JSON: frames[i] is the spectator_view before turn i, and
// frames[turns.length] is the final state. One small renderer per game turns a frame
// into HTML; everything from the data is escaped. Loaded by Node in tests (module.exports).
// A match still in progress also has data.events, an SSE URL (arena/live.py) that appends
// turns as they're played. Static exports have none, so they never try to connect.

const esc = (s) =>
  String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const DIE = ["", "⚀", "⚁", "⚂", "⚃", "⚄", "⚅"];
const name = (seats, i) => esc(seats[i] ?? `seat ${i}`);
// A seat's number in its colour, so talk can be followed without reading every name.
const avatar = (seat) => `<span class="avatar">${Number(seat)}</span>`;
const seatAttr = (seat) => `data-seat="${Number(seat)}"`;
const said = (seat, who, text, { tone = "", after = "" } = {}) =>
  `<p class="said${tone}" ${seatAttr(seat)}>${avatar(seat)}<span class="line"><b>${who}</b> ${text}${after}</span></p>`;

const pegs = (code) =>
  [...String(code)].map((c) => `<span class="peg p-${esc(c)}">${esc(c)}</span>`).join("");

const RENDERERS = {
  code_break(f, seats) {
    const over = f.scores !== null;
    const side = (guesses, seat) => {
      const rows = guesses
        .map((g, i) =>
          g.code === null
            ? `<tr><td class="num">${i + 1}</td><td colspan="2" class="muted">skipped</td></tr>`
            : `<tr><td class="num">${i + 1}</td><td class="pegs">${pegs(g.code)}</td>` +
              `<td>${g.exact} exact, ${g.colour} colour</td></tr>`,
        )
        .join("");
      const turn = !over && f.to_move === seat ? ' class="turn"' : "";
      const body = rows || '<tr><td class="muted">no guesses yet</td></tr>';
      return `<div${turn}><h3>${name(seats, seat)}</h3><table class="cb-side">${body}</table></div>`;
    };
    let status;
    if (over && f.scores[0] === f.scores[1]) status = "Draw";
    else if (over) status = `${name(seats, f.scores[0] > f.scores[1] ? 0 : 1)} wins`;
    else status = `${name(seats, f.to_move)} to guess`;
    return (
      `<p>Secret: <span class="pegs">${pegs(f.secret)}</span></p>` +
      `<div class="cb">${f.guesses.map(side).join("")}</div><p class="status">${status}</p>`
    );
  },

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

  word_link(f, seats) {
    const team = (t) => (t === "a" ? "A" : "B");
    const cells = f.board
      .map((c) => {
        const kind = f.key[c.word];
        const cls = `wl-cell k-${esc(kind)}${c.revealed ? " shown" : ""}`;
        return `<span class="${cls}">${esc(c.word)}</span>`;
      })
      .join("");
    const clues = f.clues.map((c) => `<li>Team ${team(c.team)}: ${esc(c.word)} ${Number(c.number)}</li>`).join("");
    let status;
    if (f.over && f.winner) {
      const seat = f.winner === "a" ? 0 : 2;
      status = `${name(seats, seat)} and ${name(seats, seat + 1)} win`;
    } else if (f.over) status = "Draw";
    else status = `Team ${team(f.turn)}: ${f.phase === "clue" ? "clue-giver" : "guesser"} to act. Left: A ${Number(f.left.a)}, B ${Number(f.left.b)}`;
    const roster =
      `<p>Team A: ${name(seats, 0)} and ${name(seats, 1)}. Team B: ${name(seats, 2)} and ${name(seats, 3)}.</p>`;
    return (
      `${roster}<div class="wl">${cells}</div>` +
      `${clues ? `<ul class="wl-clues">${clues}</ul>` : '<p class="muted">no clues yet</p>'}<p class="status">${status}</p>`
    );
  },

  split_deal(f, seats) {
    const items = f.items;
    const given = (keep) => items.map((it, i) => `${Number(keep[it])} ${esc(it)}`).join(", ");
    const rows = items
      .map(
        (it, i) =>
          `<tr><td>${esc(it)}</td><td class="num">${Number(f.quantities[i])}</td>` +
          `<td class="num">${Number(f.values[0][i])}</td><td class="num">${Number(f.values[1][i])}</td></tr>`,
      )
      .join("");
    const head =
      `<tr><th>Item</th><th class="num">On the table</th><th class="num">${name(seats, 0)} values</th>` +
      `<th class="num">${name(seats, 1)} values</th></tr>`;
    const offers = f.offers
      .map((o) =>
        said(o.seat, name(seats, o.seat), `keeps ${given(o.keep)}.${o.message ? ` <q>${esc(o.message)}</q>` : ""}`),
      )
      .join("");
    let status;
    if (f.deal) status = `Deal: ${name(seats, 0)} ${f.scores[0].toFixed(1)}, ${name(seats, 1)} ${f.scores[1].toFixed(1)}`;
    else if (f.over) status = "No deal: both score 0";
    else status = `${name(seats, f.to_move)} to act, ${6 - Number(f.used[f.to_move])} offers left`;
    return (
      `<table class="ld sd"><thead>${head}</thead><tbody>${rows}</tbody></table>` +
      `${offers || '<p class="muted">no offers yet</p>'}<p class="status">${status}</p>`
    );
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
    const fate = { night: "killed at night", vote: "voted out" };
    const rows = f.roles
      .map((role, i) => {
        const cls = i === f.to_move ? ' class="turn"' : "";
        const d = dead.get(i);
        const status = d ? `<span class="muted">${esc(fate[d.cause])}, day ${d.day}</span>` : "alive";
        return `<tr${cls} ${seatAttr(i)}><td>${avatar(i)} ${name(seats, i)}</td><td>${esc(role)}</td><td>${status}</td></tr>`;
      })
      .join("");
    const talk = f.messages
      .map((m) => said(m.seat, name(seats, m.seat), `<small>round ${Number(m.round)}</small> ${esc(m.text)}`))
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
      `<p class="status">${status}</p>` +
      `<div class="ww"><div class="wrap"><table class="ld"><thead><tr><th>Player</th><th>Role</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table></div>` +
      `<div>${talk}` +
      line("Wolf votes tonight", f.wolf_votes.map(pick)) +
      line("Day votes", f.votes.map(pick)) +
      (f.votes.length
        ? roundTable({ seats: f.roles.length, dead: f.deaths.map((d) => d.seat), votes: f.votes, roles: f.roles })
        : "") +
      line("Seer", found) +
      "</div></div>"
    );
  },
};

const fallback = (f) => `<pre>${esc(JSON.stringify(f, null, 1))}</pre>`;

// Seats in a ring with a vote drawn as an arrow, so a pile-on reads at a glance. Plain text
// beside it always carries the same votes. `roles` is only passed where they may be seen.
function roundTable({ seats, dead, votes, out = null, roles = null }) {
  const centre = 130;
  const at = (seat) => {
    const angle = ((-90 + (seat * 360) / seats) * Math.PI) / 180;
    return [centre + 100 * Math.cos(angle), centre + 100 * Math.sin(angle)];
  };
  const arrows = votes
    .map(({ seat, target }) => {
      const [x1, y1] = at(seat);
      const [x2, y2] = at(target);
      const len = Math.hypot(x2 - x1, y2 - y1);
      const [ux, uy] = [(x2 - x1) / len, (y2 - y1) / len];
      const [nx, ny] = [-uy * 3, ux * 3]; // two seats voting for each other shouldn't overlap
      const x = (a, by) => (a + ux * by + nx).toFixed(1);
      const y = (a, by) => (a + uy * by + ny).toFixed(1);
      return `<line x1="${x(x1, 23)}" y1="${y(y1, 23)}" x2="${x(x2, -27)}" y2="${y(y2, -27)}" marker-end="url(#vote-arrow)"/>`;
    })
    .join("");
  const circles = Array.from({ length: seats }, (_, seat) => {
    const [cx, cy] = at(seat).map((v) => v.toFixed(1));
    const tone = [
      dead.includes(seat) ? "dead" : "",
      seat === out ? "out" : "",
      roles?.[seat] === "wolf" ? "wolf" : "",
    ].join(" ");
    return `<g class="${tone.trim()}" ${seatAttr(seat)}><circle cx="${cx}" cy="${cy}" r="20"/><text x="${cx}" y="${cy}">${seat}</text></g>`;
  }).join("");
  const label = votes.map((v) => `${player(v.seat)} votes ${player(v.target)}`).join(", ");
  return (
    `<svg class="round" viewBox="0 0 260 260" role="img" aria-label="${esc(label || "No votes")}">` +
    '<defs><marker id="vote-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0 10 5 0 10z"/></marker></defs>' +
    `${arrows}${circles}</svg>`
  );
}

// The shape of a match, for the strip under the slider: phases as bands, and each moment
// that changes the game as a tick. A game with no such moments gets an empty strip.
function timeline(game, frames, seats) {
  const bands = [];
  const ticks = [];
  if (game === "word_link") {
    let seen = 0;
    frames.forEach((f, i) => {
      f.reveals.slice(seen).forEach((r) => {
        if (r.kind === "trap") ticks.push({ at: i, text: `Team ${r.team === "a" ? "A" : "B"} hit the trap` });
      });
      seen = f.reveals.length;
      if (f.over && f.winner && i === frames.length - 1) {
        ticks.push({ at: i, text: `Team ${f.winner === "a" ? "A" : "B"} won` });
      }
    });
  }
  if (game === "split_deal") {
    let ended = false;
    frames.forEach((f, i) => {
      if (!f.over || ended) return;
      ended = true;
      ticks.push({ at: i, text: f.deal ? "Deal reached" : "No deal" });
    });
  }
  if (game === "code_break") {
    const done = [false, false];
    frames.forEach((f, i) => {
      f.guesses.forEach((side, seat) => {
        const at = side.findIndex((g) => g.exact === 4);
        if (at < 0 || done[seat]) return;
        done[seat] = true;
        ticks.push({ at: i, text: `${name(seats, seat)} solved it on guess ${at + 1}` });
      });
    });
  }
  if (game === "liars_dice" || game === "liars_dice_small") {
    let round = null;
    frames.forEach((f, i) => {
      const r = f.last_reveal;
      if (!r || r.round === round) return;
      round = r.round;
      ticks.push({ at: i, text: `Round ${Number(r.round)}: ${name(seats, r.loser)} lost a die` });
    });
  }
  if (game === "werewolf" || game === "werewolf_dayfirst") {
    const fate = { night: "killed at night", vote: "voted out" };
    let phase = null;
    let dead = 0;
    frames.forEach((f, i) => {
      if (!f.winner && f.phase !== phase) {
        phase = f.phase;
        const label = phase === "night" ? "Night" : phase === "vote" ? "Vote, day" : "Day";
        bands.push({ from: i, phase, text: `${label} ${Number(f.day)}` });
      }
      f.deaths.slice(dead).forEach((d) => {
        ticks.push({ at: i, text: `${name(seats, d.seat)} ${fate[d.cause]}, day ${Number(d.day)}` });
      });
      dead = f.deaths.length;
    });
  }
  return { bands, ticks };
}

function renderTimeline({ bands, ticks }, last) {
  if (!last || (!bands.length && !ticks.length)) return "";
  const at = (frame) => ((100 * frame) / last).toFixed(2);
  const strip = bands.map((b, i) => {
    const to = bands[i + 1]?.from ?? last;
    const style = `left:${at(b.from)}%;width:${at(to - b.from)}%`;
    return `<button type="button" class="band ${esc(b.phase)}" style="${style}" data-jump="${b.from}" title="${b.text}" aria-label="${b.text}"></button>`;
  });
  const marks = ticks.map(
    (t) => `<button type="button" class="tick" style="left:${at(t.at)}%" data-jump="${t.at}" title="${t.text}" aria-label="${t.text}"></button>`,
  );
  return strip.join("") + marks.join("");
}

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

// Autoplay gives a move a beat, and a message long enough to actually read it.
function holdFor(turn) {
  const text = turn?.action?.message ?? "";
  return Math.min(6000, 700 + text.length * 30);
}

function initReplay(doc) {
  const el = doc.getElementById("replay-data");
  if (!el) return;
  const data = JSON.parse(el.textContent);
  let last = data.frames.length - 1;
  const $ = (id) => doc.getElementById(id);
  const slider = $("replay-pos");
  slider.max = String(last);
  let idx = last;
  let timer = null;
  const marks = () => {
    $("replay-marks").innerHTML = renderTimeline(timeline(data.game, data.frames, data.seats), last);
  };

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
    clearTimeout(timer);
    timer = null;
    $("replay-play").textContent = "▶ Play";
  }
  function play() {
    if (timer) return stop();
    if (idx === last) show(0);
    $("replay-play").textContent = "❚❚ Pause";
    const tick = () => {
      show(idx + 1);
      if (timer !== null) timer = setTimeout(tick, holdFor(data.turns[idx - 1]));
    };
    timer = setTimeout(tick, holdFor(data.turns[idx - 1]));
  }

  $("replay-first").onclick = () => { stop(); show(0); };
  $("replay-prev").onclick = () => { stop(); show(idx - 1); };
  $("replay-next").onclick = () => { stop(); show(idx + 1); };
  $("replay-last").onclick = () => { stop(); show(last); };
  $("replay-play").onclick = play;
  slider.oninput = () => { stop(); show(Number(slider.value)); };
  $("replay-marks").onclick = (e) => {
    const frame = e.target.closest?.("[data-jump]")?.dataset.jump;
    if (frame !== undefined) { stop(); show(Number(frame)); }
  };
  doc.querySelectorAll("tr[data-frame]").forEach((tr) => {
    tr.onclick = () => { stop(); show(Number(tr.dataset.frame)); };
  });
  doc.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") { stop(); show(idx - 1); }
    if (e.key === "ArrowRight") { stop(); show(idx + 1); }
  });
  marks();
  show(last);

  function follow() {
    const stream = new EventSource(`${data.events}?after=${data.turns.length}`);
    $("replay-live").textContent = "● Live";
    stream.addEventListener("turn", (e) => {
      const { turn, frame } = JSON.parse(e.data);
      if (turn.turn_no !== data.turns.length) return; // already have it (a reconnect overlap)
      const atEnd = idx === last;
      data.turns.push(turn);
      data.frames.push(frame);
      last += 1;
      slider.max = String(last);
      marks();
      show(atEnd ? last : idx); // only viewers at the newest frame are moved along
    });
    stream.addEventListener("done", (e) => {
      stream.close();
      $("replay-live").textContent = `Match ${JSON.parse(e.data).status}: reload for the turn table`;
    });
  }
  if (data.events && typeof EventSource !== "undefined") follow();
}

// Play blind (Werewolf): the game as a villager sees it. Spectator frames hold every role,
// so blindSteps copies out only public facts, and renderBlind sees nothing else until the
// end. Seats are "Player N" because that's what the models call each other, and because
// the page's model names shouldn't carry over. The data is in the page, so this keeps a
// game honest for a player, not secret from someone reading the source.
const STARTING_WOLVES = 2;
const player = (seat) => `Player ${Number(seat)}`;

function blindSteps(frames, turns) {
  const final = frames[frames.length - 1];
  const steps = [];
  let past = [];
  let seen = -1;
  let votedOn = 0;
  const publicFacts = (f) => ({
    day: f.day,
    alive: [...f.alive],
    dead: f.deaths.map(({ seat, role, cause, day }) => ({ seat, role, cause, day })),
  });
  const said = (f) => f.messages.map(({ seat, text }) => ({ day: f.day, seat, text }));
  frames.forEach((f, i) => {
    if (f.winner || f.phase === "night") return;
    if (f.phase === "discussion") {
      const key = f.day * 1000 + f.messages.length;
      if (key === seen) return; // a forfeited turn says nothing, so there's nothing to step to
      seen = key;
      steps.push({ kind: "talk", ...publicFacts(f), messages: [...past, ...said(f)] });
    } else if (f.day !== votedOn) {
      votedOn = f.day;
      past = [...past, ...said(f)];
      const facts = publicFacts(f);
      steps.push({ kind: "vote", ...facts, messages: past });
      const votes = turns
        .filter((t) => t.action?.action === "vote" && frames[t.turn_no].day === f.day)
        .map((t) => ({ seat: t.seat, target: t.action.target }));
      const out = final.deaths.find((d) => d.day === f.day && d.cause === "vote") ?? null;
      steps.push({
        kind: "result",
        day: f.day,
        alive: out ? facts.alive.filter((s) => s !== out.seat) : facts.alive,
        dead: out ? [...facts.dead, { seat: out.seat, role: out.role, cause: "vote", day: f.day }] : facts.dead,
        messages: past,
        votes,
        out: out && { seat: out.seat, role: out.role },
      });
    }
  });
  steps.push({ kind: "end" });
  return steps;
}

const wolvesLeft = (step) => STARTING_WOLVES - step.dead.filter((d) => d.role === "wolf").length;

function renderSeats(step, chosen) {
  const fate = new Map(step.dead.map((d) => [d.seat, d]));
  const seats = [...step.alive, ...step.dead.map((d) => d.seat)].sort((a, b) => a - b);
  return seats
    .map((seat) => {
      const d = fate.get(seat);
      if (d) return `<span class="seat dead" ${seatAttr(seat)}>${player(seat)} <small>${esc(d.role)}</small></span>`;
      if (step.kind !== "vote") return `<span class="seat" ${seatAttr(seat)}>${player(seat)}</span>`;
      const on = chosen.has(seat);
      return `<button class="seat${on ? " on" : ""}" ${seatAttr(seat)} data-pick="${Number(seat)}" aria-pressed="${on}">${player(seat)}</button>`;
    })
    .join("");
}

function renderTranscript(messages) {
  let day = 0;
  return messages
    .map((m) => {
      const head = m.day === day ? "" : `<h3>Day ${Number(m.day)}</h3>`;
      day = m.day;
      return head + said(m.seat, player(m.seat), esc(m.text));
    })
    .join("");
}

function renderBlind(step, chosen = new Set()) {
  const head = (lines) =>
    `<div class="blind-head">${lines}<div class="seats">${renderSeats(step, chosen)}</div></div>`;
  const log = `<div class="transcript">${renderTranscript(step.messages)}</div>`;
  if (step.kind === "talk") {
    const killed = step.dead
      .filter((d) => d.day === step.day && d.cause === "night")
      .map((d) => `${player(d.seat)} was killed in the night: a ${esc(d.role)}.`);
    const opening = step.messages.length ? [] : ["Two wolves are at this table. Nobody has spoken yet."];
    const note = [...killed, ...opening].map((line) => `<p>${line}</p>`).join("");
    return head(`<p class="status">Day ${Number(step.day)}</p>${note}`) + log;
  }
  if (step.kind === "vote") {
    const n = wolvesLeft(step);
    const ask = `Day ${Number(step.day)} vote: who ${n === 1 ? "is the wolf" : `are the ${n} wolves`}?`;
    return head(`<p class="status">${ask}</p>`) + log;
  }
  const votes = step.votes.map((v) => `${player(v.seat)} → ${player(v.target)}`).join(", ");
  const out = step.out
    ? `${player(step.out.seat)} is voted out: a ${esc(step.out.role)}.`
    : "A tied vote: nobody goes.";
  const table = roundTable({
    seats: step.alive.length + step.dead.length,
    dead: step.dead.map((d) => d.seat),
    votes: step.votes,
    out: step.out?.seat ?? null,
  });
  return head(`<p class="status">${out}</p><p class="ld-bids">Votes: ${votes || "none"}</p>`) + table + log;
}

// Per day: how many of your picks were wolves, and how the village's own votes did.
function blindScore(frames, turns, picks) {
  const roles = frames[frames.length - 1].roles;
  return Object.keys(picks)
    .map(Number)
    .sort((a, b) => a - b)
    .map((day) => {
      const chosen = [...picks[day]];
      const votes = turns.filter(
        (t) => t.action?.action === "vote" && frames[t.turn_no].day === day && roles[t.seat] !== "wolf",
      );
      return {
        day,
        hits: chosen.filter((s) => roles[s] === "wolf").length,
        picked: chosen.length,
        villageHits: votes.filter((t) => roles[t.action.target] === "wolf").length,
        villageVotes: votes.length,
      };
    });
}

// What a claim was, once the roles are known. A wolf lying and a villager who was simply
// wrong are different things, and only the classifier's literal matches are ever marked.
function tell(claim, speaker, roles) {
  if (claim.kind === "leak") return { kind: "bluff", text: "Gave the wolves away" };
  if (claim.kind !== "false") return null;
  const about = claim.seat === speaker ? "claims to be" : `calls ${player(claim.seat)}`;
  const text = `${about} a ${esc(claim.role)}`;
  if (roles[speaker] === "wolf" || claim.seat === speaker) return { kind: "bluff", text: `Bluff: ${text}` };
  return { kind: "wrong", text: `Wrong: ${text}` };
}

function renderTruth(frames, turns, roles) {
  let day = 0;
  let bluffs = 0;
  const lines = turns
    .filter((t) => t.action?.action === "say")
    .map((t) => {
      const today = frames[t.turn_no].day;
      const head = today === day ? "" : `<h3>Day ${Number(today)}</h3>`;
      day = today;
      const tells = (t.claims ?? []).map((c) => tell(c, t.seat, roles)).filter(Boolean);
      bluffs += tells.filter((x) => x.kind === "bluff").length;
      const tone = tells.find((x) => x.kind === "bluff") ? " bluff" : tells.length ? " wrong" : "";
      const notes = tells.map((x) => `<span class="tell">${x.text}</span>`).join("");
      return head + said(t.seat, player(t.seat), `<small>${esc(roles[t.seat])}</small> ${esc(t.action.message)}`, { tone, after: notes });
    })
    .join("");
  const count = bluffs === 1 ? "1 bluff" : `${bluffs} bluffs`;
  return (
    `<section class="truth"><h2>The same game, with the truth on</h2>` +
    `<p class="muted">${count} marked. Only literal claims count, so paraphrased lies slip through.</p>` +
    `<div class="transcript">${lines}</div></section>`
  );
}

function renderBlindEnd(frames, turns, picks, seats) {
  const final = frames[frames.length - 1];
  const fates = new Map(final.deaths.map((d) => [d.seat, d]));
  const how = { night: "killed at night", vote: "voted out" };
  const cards = final.roles
    .map((role, i) => {
      const d = fates.get(i);
      const fate = d ? `${how[d.cause]}, day ${Number(d.day)}` : "survived";
      const tone = role === "wolf" || role === "seer" ? ` ${esc(role)}` : "";
      return `<div class="card${tone}" style="--i:${i}"><small>${player(i)}</small><b>${esc(role)}</b><span class="pid">${name(seats, i)}</span><small>${fate}</small></div>`;
    })
    .join("");
  const score = blindScore(frames, turns, picks)
    .map((s) => {
      const you = `${s.hits} of your ${s.picked} picks were wolves`;
      const them = `the village put ${s.villageHits} of ${s.villageVotes} votes on a wolf`;
      return `<li>Day ${s.day}: ${you}, and ${them}.</li>`;
    })
    .join("");
  return (
    `<p class="status">The ${esc(final.winner)} win${final.winner === "wolves" ? "" : "s"}.</p>` +
    (score ? `<ul>${score}</ul>` : "") +
    `<div class="cards">${cards}</div>` +
    renderTruth(frames, turns, final.roles)
  );
}

function initBlind(doc, data) {
  const start = doc.getElementById("blind-start");
  if (!start) return;
  const $ = (id) => doc.getElementById(id);
  const steps = blindSteps(data.frames, data.turns);
  const picks = {};
  let at = 0;

  function draw() {
    const step = steps[at];
    const end = step.kind === "end";
    const chosen = (picks[step.day] ??= new Set());
    if (step.kind !== "vote" && !chosen.size) delete picks[step.day];
    $("blind-view").innerHTML = end
      ? renderBlindEnd(data.frames, data.turns, picks, data.seats)
      : renderBlind(step, chosen);
    const next = $("blind-next");
    next.hidden = end;
    next.disabled = step.kind === "vote" && chosen.size !== wolvesLeft(step);
    next.textContent = step.kind === "vote" ? "Lock in" : "Next";
    $("blind-skip").hidden = step.kind !== "talk";
    const scroll = end ? 0 : doc.documentElement.scrollHeight; // the newest message, instantly
    doc.defaultView?.scrollTo(0, scroll);
  }
  function enter() {
    doc.documentElement.classList.add("blind");
    at = 0;
    for (const day of Object.keys(picks)) delete picks[day];
    draw();
  }
  function advance(i) {
    at = Math.min(i, steps.length - 1);
    draw();
  }
  start.onclick = enter;
  $("blind-exit").onclick = () => doc.documentElement.classList.remove("blind");
  $("blind-next").onclick = () => advance(at + 1);
  $("blind-skip").onclick = () => advance(steps.findIndex((s, i) => i > at && s.kind !== "talk"));
  $("blind-view").onclick = (e) => {
    const seat = e.target.closest?.("[data-pick]")?.dataset.pick;
    if (seat === undefined) return;
    const step = steps[at];
    const chosen = picks[step.day];
    const n = Number(seat);
    if (chosen.has(n)) chosen.delete(n);
    else if (chosen.size < wolvesLeft(step)) chosen.add(n);
    draw();
  };
  if (doc.documentElement.classList.contains("blind")) enter();
}

RENDERERS.werewolf_dayfirst = RENDERERS.werewolf;
RENDERERS.liars_dice_small = RENDERERS.liars_dice;
if (typeof document !== "undefined") {
  initReplay(document);
  initBlind(document, JSON.parse(document.getElementById("replay-data").textContent));
}

if (typeof module !== "undefined") module.exports = {
  RENDERERS, renderFrame, describeTurn, esc, initReplay, holdFor,
  blindSteps, renderBlind, renderBlindEnd, blindScore, wolvesLeft, initBlind, roundTable,
  timeline, renderTimeline,
};
