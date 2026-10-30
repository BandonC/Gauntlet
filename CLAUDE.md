# CLAUDE.md

Guidance for Claude Code working in this repo. Read this before changing anything.

## What this is

**LLM Arena** (working name): a service that runs tournaments between LLM "players"
(provider + model + versioned prompt + settings) across turn-based games, records every
move, computes ratings, tracks token cost, and serves a leaderboard with replays.

It's a backend/distributed-systems portfolio project. The game logic is deliberately
simple; the interesting parts are **resumable matches (event-sourced turn log), lease
fencing, a Postgres job queue, provider rate limiting/retries, and cost accounting.**
Optimise for correctness and clear failure semantics over features.

## Status

| Area | State |
|---|---|
| Game contract, Connect 4, match runner, players, Elo, round-robin | Done, unit-tested |
| Anthropic/OpenAI adapters, retries, rate limiter | Done, tested against canned HTTP responses. **No real API call has been made yet.** |
| Postgres schema, queue + leases, fenced turn writes, worker, budget cap | Done, integration-tested on Postgres 16 (`tests/test_store_db.py`) |
| JSON API + Jinja2 pages (leaderboards, tournaments, matches, match turns) | Done; replay stepping is the next UI item |
| Ratings (Elo + Glicko-2) in the completion transaction, tournament completion, graceful shutdown, metrics endpoint, cross-process rate limit | Done, db-tested |
| Swiss tournaments, pause/resume on failed matches | Done, db-tested |
| Liar's Dice engine + bots (2-6 seats; tournaments stay heads-up) | Done, unit-tested; no UI renderer yet |
| Presentation, Werewolf, multiplayer ratings | See Roadmap |

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env && cp roster.example.toml roster.toml   # then edit both
docker compose up -d db testdb

pytest                                   # unit tests; db tests skip without DATABASE_URL
DATABASE_URL=postgresql://arena:arena@localhost:5433/arena_test pytest -m db
ruff check arena tests && ruff format arena tests

arena play connect4 --p1 random:1 --p2 random:2           # free, in memory
arena migrate
arena tournament --game connect4 --players random,haiku-5.5/c4-v1 --games-per-pair 10 --budget 2
arena worker --concurrency 4             # Ctrl-C any time; matches resume elsewhere
arena leaderboard --game connect4
arena serve                              # http://localhost:8000
```

`tests/test_store_db.py` DROPS the public schema. Only ever point it at `testdb` (:5433)
or the throwaway database from `scripts/test-db.sh`.

No Docker (e.g. cloud runs): `export DATABASE_URL=$(scripts/test-db.sh)` starts a
throwaway Postgres from the system binaries, then run `pytest -m db`.

Keep API keys in `.env` only. Don't `export ANTHROPIC_API_KEY` in your shell: Claude
Code prefers it over your claude.ai login, which hides `/schedule` and bills the API.

## Architecture

```
CLI / API ──► Postgres ◄── worker(s) ──► MatchRunner ──► Game engine (pure)
                 ▲                            │
   turns (append-only log)                    └──► Player ──► RetryingProvider ──► vendor adapter
   matches (queue + leases)                              (shared RateLimiter per vendor)
```

```
arena/
  types.py              Action (JSON dict), ActionSpec (tool schema), Usage
  games/base.py         Game protocol  ← read first
  games/connect4.py     reference implementation
  games/liars_dice.py   hidden information (dice), per-round randomness
  engine/match_runner.py  MatchRunner, iter_states (replay), TurnStore, LeaseLost
  players/              Player protocol, RandomPlayer/ScriptedPlayer, LLMPlayer, prompts
  players/prompts/<game>/<version>/{system,turn}.txt
  providers/            Provider protocol, anthropic/openai adapters, retry, ratelimit, mock
  store/                asyncpg pool, migrations runner, queue (claim/heartbeat/complete),
                        PostgresTurnStore (fenced), tournament creation, leaderboard
  worker.py             claim → run with heartbeat → complete / release for retry
  api.py                FastAPI: JSON API + HTML routes
  web.py, templates/    Jinja2 page builders (pool, Urls) -> HTML; reused by export
  store/read.py         read queries shared by the API, pages and export
  roster.py             roster.toml → PlayerConfig → Player (+ Pricing)
  costs.py, ratings/{elo,glicko2}.py, tournaments.py (round_robin), cli.py, config.py
migrations/             forward-only SQL, applied in filename order
```

## Invariants (do not break these)

1. **Game engines are pure and deterministic.** `apply` returns a new state, never
   mutates. No I/O, no LLM calls, no wall clock. All randomness derives from the match
   `seed` and lives in the game state (e.g. an RNG state field), so replay is exact.
2. **The turn log is the only source of truth.** Match state is never stored. Resume =
   `iter_states(game, spec, turns)`. Replays re-validate every action
   (`CorruptTurnLog` on mismatch).
3. **Persist a turn before advancing.** `MatchRunner` writes the `TurnRecord`, then
   updates in-memory state. A turn that was played but not persisted is replayed (and
   re-paid) after a crash; that's accepted.
4. **Turn writes are fenced by the lease.** `PostgresTurnStore.append_turn` inserts only
   if this worker holds an unexpired lease (`FOR SHARE` on the match row), and the
   `(match_id, turn_no)` PK rejects duplicates. Both raise `LeaseLost`; the worker then
   stops immediately and writes nothing else.
5. **Infrastructure errors never forfeit a player.** `ProviderError` (429/5xx/timeouts)
   is retried by `RetryingProvider`, then propagates and the worker re-queues the match
   with backoff. Only *model behaviour* (no tool call, illegal or malformed action) uses
   up attempts; `max_attempts` (default 3) invalid attempts = forfeit.
6. **Players are stateless between turns.** Everything a player needs must come from
   `TurnContext` (built from `player_view`). This is what makes resumption on a new
   worker correct. Werewolf-style memory must be derived from the game state's
   per-seat view (e.g. a discussion log in state), never from Python object state.
7. **Prompts are immutable once used.** New wording = new version directory
   (`connect4/v2`). Player identity = `identity_hash(provider, model, prompt + content
   hash, temperature, max_tokens)`. Any change = new roster id;
   `upsert_players` refuses to reuse an id with a different config.
8. **Prices come only from `roster.toml`.** Never hard-code prices. Missing cache
   prices fall back to the input price (over-count, never under-count).
9. **One `RetryingProvider` + `RateLimiter` per vendor per process** (`PlayerFactory`
   does this). Don't construct adapters per player.
10. **Hidden information goes through `player_view`.** Never put another seat's private
    info in a `TurnContext`. `spectator_view` is for replays/UI only.

## Spending money (rules for Claude Code)

- **Tests never call real APIs.** Use `RandomPlayer`, `ScriptedPlayer`, `MockProvider`,
  or `load_recording()` fixtures. Adapter tests use `httpx2.MockTransport`.
- **Don't run anything that calls a real model without the user explicitly asking in
  this session.** That includes `arena play` with LLM players and `arena worker` on a
  tournament that contains LLM players.
- When the user does ask: always pass `--budget`, start with the cheapest model, and
  report tokens and cost from the output afterwards.
- Total project budget is about $50. Rough split: ~$5 dev/testing, ~$10 a cheap-model
  second game, ~$25 Werewolf, ~$10 buffer.

## SDK notes (verified against anthropic 2.3.0 / openai 3.41.0, Oct 2026)

- Both SDKs use **`httpx2`**, not `httpx`. Inject `httpx2.AsyncClient(transport=
  httpx2.MockTransport(handler))` via the adapters' `http_client` argument in tests.
- Anthropic `messages.create()` takes **no `temperature`** in this SDK version; the
  adapter never sends one. Forced tool use is `tool_choice={"type": "tool", "name": ...}`.
  `usage.input_tokens` excludes cached tokens.
- OpenAI chat completions: `max_completion_tokens`; some models reject a non-default
  temperature (use `temperature = "omit"` in the roster). `prompt_tokens` INCLUDES cached
  tokens; the adapter subtracts them so `Usage.input_tokens` means uncached everywhere.
- Reasoning models bill hidden reasoning as output tokens. Give them a larger
  `max_tokens` and expect several times the cost.
- Prompt caching only applies above a model-specific minimum prompt length; Connect 4's
  short system prompt likely won't cache. Check `cache_read_tokens` before assuming.
- SDK built-in retries are disabled (`max_retries=0`); `RetryingProvider` is the only
  retry layer. Keep it that way so retries are visible and testable.

## Conventions

- Python 3.12+, async throughout, type hints everywhere, ruff (line length 100).
- Core modules (`types`, `games`, `engine`, `players`, `ratings`, `costs`) use stdlib
  dataclasses only. No pydantic or DB imports there.
- Actions are plain JSON dicts validated by the engine. Tool schemas use
  `additionalProperties: false`.
- SQL is hand-written in module constants. In `INSERT ... SELECT $n` add explicit casts
  (`$1::bigint`); Postgres doesn't infer target column types there.
- Migrations are forward-only. Never edit an applied file; add `000N_*.sql`.
- New behaviour needs a test. Concurrency/failure behaviour needs a `db` test that
  actually exercises the race (see `test_expired_lease_is_reclaimed_and_old_writer_is_fenced`).
- Keep the UI thin. Spend no more than ~20% of effort there.

## Roadmap

**v1 definition of done:** Connect 4 · Postgres workers with leases · round-robin ·
Elo · cost tracking + budget cap · replay page · first real tournament with at least
3 LLM players. v1 uses Elo; Glicko-2 runs beside it as a post-v1 addition (it ranks the
leaderboard, but Elo stays the v1 rating and is always shown).

Items tagged `[human]` need real API calls or a decision from the user. Unattended
runs (loops, scheduled tasks) skip them.

- [x] **Phase 1**: contracts, Connect 4, match runner, players, providers, CLI
- [x] **Phase 2**: schema, queue/leases, fenced turn store, worker, budget cap, read API
- [ ] `[human]` **First real run**: two cheap models, 20 games, `--budget 1`. Record
      responses with `RecordingProvider` into `recordings/` for regression fixtures.
      Check illegal-move rate, latency, and whether caching kicks in.

**Phase 3: correctness and ops**
- [x] Write ratings + `rating_history` in the same transaction as `queue.complete`
      (the UNIQUE (player_id, match_id) constraint guards double-rating); leaderboard
      reads `ratings` instead of recomputing.
- [x] Mark tournaments `completed` when no queued/running matches remain.
- [x] Graceful SIGTERM: stop claiming, let the in-flight turn persist, exit.
- [x] Metrics endpoint: illegal-move rate per player, forfeits, latency p50/p95,
      cost per game, cache hit rate.
- [x] Cross-process rate limiting. The limiter is per process today; options: a token
      table with `SELECT ... FOR UPDATE`, or a static per-worker share of the quota.
- [x] Glicko-2 beside Elo, post-v1 (rating deviation → confidence on the leaderboard).
- [x] Swiss pairing (`--format swiss`). Prefer round-robin below about 10 players: Swiss
      plays fewer matches at every size (2 games per pairing: 6 players 18 vs 30,
      10 players 40 vs 90, 16 players 64 vs 240), but each player meets only
      ceil(log2 N) opponents, so a small field gets a noisier ranking for a small saving.

**Phase 4: presentation**
- [x] `[human]` Pick the UI approach (decided 2026-10-29): Jinja2 templates served by
      FastAPI; `arena export` renders the same templates to static HTML + JSON. Replay
      stepping in plain JS from frames embedded in the page. No Vite, no HTMX, no build
      step. Adding `jinja2` to the project dependencies is approved.
- [x] Jinja2 templates replace `static/index.html`: leaderboard (rating ± 2·RD, Elo,
      games), tournaments (round, paused reason), match list, replay page. Same data as
      the JSON API, which stays.
- [ ] Replay page: every turn's `spectator_view` frame embedded as JSON; plain-JS
      prev/next/play stepping; one small renderer function per game: Connect 4 (board),
      Liar's Dice (all dice, bids, reveals).
- [ ] `arena export`: render the same templates to static HTML + JSON files for GitHub
      Pages ($0 hosting). No server needed to browse the result.
- [ ] Live match view via Server-Sent Events. Optional; do it last.

**Phase 5: v2 games**
- [x] Liar's Dice engine, `LiarsDiceBot`, prompt `liars_dice/v1`: hidden dice, round r's
      dice from `Random(f"{seed}:{r}")`; first real test of `player_view` hiding.
- [ ] Multiplayer ratings and pairing. Engines take more than 2 seats (Liar's Dice 2-6,
      Werewolf), but round-robin, Swiss, Elo and Glicko-2 are heads-up only today.
- [ ] Werewolf engine + scripted bots (flagship): multi-phase turns (discussion → vote →
      night), per-seat views, discussion log in state with summarisation of old rounds
      to bound context (cost grows ~quadratically otherwise), "role leaked" metric.
- [ ] `[human]` First real Liar's Dice and Werewolf tournaments.

## Unattended runs (loops and scheduled tasks)

When working without the user watching (a `/loop`, or a cloud scheduled task):

1. Work on **one** roadmap item per run: the first unchecked item not tagged `[human]`.
   If an open `claude/` branch or PR already covers it, continue that instead.
2. Work on a branch named `claude/<short-item-name>`. Never commit to or merge
   into `main` without the user's explicit approval. Never push from a local loop. 
3. Before committing: `ruff check arena tests`, `ruff format arena tests`, `pytest`,
   and `pytest -m db` when Postgres is available. All must pass. If db tests can't
   run, say so in the PR description.
4. Tick the item's checkbox in this file in the same branch, and add any new gotchas.
5. Open a PR (cloud) or leave a commit summary (local) describing what changed, how it
   was tested, and anything left undone.
6. **Stop and report instead** if the item needs real API calls, a new dependency
   beyond the dev tools, a change to an invariant above, or a design choice the
   roadmap doesn't settle. Write the open question in the PR or the final message.
7. Never set or read API keys, never run `arena play`/`arena worker` with LLM players,
   and never edit an existing migration or prompt version.

## Adding a game (checklist)

1. `arena/games/<name>.py` implementing every method of `Game` in `games/base.py`.
2. Register it in `games/registry.py`.
3. Unit tests: win/loss/draw, illegal actions, `apply` doesn't mutate, `player_view`
   hides other seats' private info, and replay determinism (same seed + actions → same
   final state).
4. `RandomPlayer` vs `RandomPlayer` through `MatchRunner` (needs `legal_actions_hint`).
   For free-text actions, add a scripted bot instead.
5. Prompt in `players/prompts/<name>/v1/`.
6. A small JS renderer for its `spectator_view` frames on the replay page (one function
   per game, no framework).
7. Only then: one real match with a cheap model and `--budget`.

## Gotchas

- `iter_states` raises if a turn's seat doesn't match `current_player`, so engines must
  make `current_player` a pure function of state.
- The worker treats setup errors (unknown player id, unknown game) as permanent: the
  match is marked `failed` immediately with `last_error` set.
- Budget enforcement is at claim time plus per-turn spend accounting: matches already
  running finish even if they push spend slightly over the cap.
- `arena play` uses an in-memory store and a per-match `--budget` (default $0.50); it
  never touches Postgres.
- Ratings are written only by `queue.complete` (`store/ratings.py`). Matches completed
  before that existed are unrated, and ratings skip matches without exactly 2 seats.
  Ratings rows are locked in `player_id` order; keep that order to avoid deadlocks.
- Glicko-2: every match is its own rating period, tau = 0.3, and RD never grows from
  inactivity (skill is fixed by config_hash). The leaderboard ranks by rating - 2·RD.
  After applying migration 0003 run `arena ratings rebuild` once: existing rows start at
  Glicko defaults. Rebuild replays by (completed_at, id), and `queue.complete` re-stamps
  completed_at with clock_timestamp() under the rating locks so that order is exact.
- Tournaments advance in the transaction that completes a match (`advance_tournament`):
  once nothing is queued, running or failed, Swiss queues the next round or the
  tournament completes. A budget-capped tournament with queued matches stays `running`.
  Lock order in a completion: match row, ratings rows, tournament row last.
- A match that fails for good (run attempts used up, or a setup error) pauses its
  tournament, both formats: nothing is claimed or paired until `arena tournament resume
  <id>` re-queues failed matches with fresh run attempts. Failed matches are never scored.
- Swiss: round n+1 is created only under the tournament row lock AND by the conditional
  `current_round = n -> n+1` UPDATE. Byes are worth 2 points (winning both games of a
  pairing) and live in `tournament_byes`; they are not matches, so ratings ignore them.
  After migration 0004, round-robin tournaments made earlier have no `tournament_players`
  rows (only Swiss reads them).
- Liar's Dice: randomness is derived, not stored. Round r's dice are
  `Random(f"{seed}:{r}")` over the current dice counts, so replay re-rolls them exactly.
  `player_view` has only your own dice plus public info (counts, this round's bids, the
  previous challenge's reveal); `test_player_view_never_depends_on_other_players_dice`
  scrambles other seats' dice at every turn of random games to prove it. Bots must stay
  stateless (decide from the view). `LiarsDiceBot` isn't a roster provider yet, so
  Postgres tournaments can only use it once roster.py learns a bot provider.
- Pages: `web.py` page builders take `(pool, Urls)` and return HTML; FastAPI wraps them
  and `arena export` should call the same builders with file-path `Urls`. Never hard-code
  an href in a template: use `url('match', id=...)`. Templates use StrictUndefined, so a
  missing field raises (tested in `tests/test_web.py`); test pages by calling the builders
  (no `TestClient`: it needs `httpx`). Read queries live in `store/read.py`, shared with
  the JSON API so both always show the same data.
  Lock the tournament `FOR NO KEY UPDATE`, never `FOR UPDATE`: the completed_at re-stamp
  re-runs the matches→tournaments FK check (KEY SHARE), and upgrading that deadlocks.
- Graceful stop: the first SIGTERM/Ctrl-C lets in-flight turns persist, then hands each
  match back via `queue.release_on_shutdown` (queued now, run attempt refunded). A second
  signal is a hard stop. On Windows an external SIGTERM is a hard kill (TerminateProcess);
  only Ctrl-C there is graceful.
- `/api/metrics/{game}` definitions live in `store/metrics.py`'s docstring. Cost per
  game counts completed matches only; turn-level stats include running matches.
  API endpoints have no HTTP tests: `TestClient` needs `httpx`, which isn't a dependency
  (the SDKs use `httpx2`). Test the store function instead.
- Requests/minute across workers: `arena worker` seeds `rate_limits` from roster
  `[providers]` at startup and takes tokens with one atomic UPDATE (`store/ratelimit.py`).
  Re-seeding keeps the current fill (clamped), so restarts don't grant a fresh burst.
  `max_concurrency` and retry-after pauses stay per process; `arena play` stays fully
  in-process. A vendor with no row raises rather than waiting forever.
