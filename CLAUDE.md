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
| Anthropic adapter, retries, rate limiter | Done, proven by real runs (see First real run under Gotchas) |
| OpenAI adapter | Done, tested against canned HTTP responses. **Never called for real**: no key yet, so OpenAI players are optional. |
| Postgres schema, queue + leases, fenced turn writes, worker, budget cap | Done, integration-tested on Postgres 16 (`tests/test_store_db.py`) |
| JSON API + Jinja2 pages (leaderboards, tournaments, matches, replay stepping, live view over SSE), static export | Done |
| Live ratings (Weng-Lin on every match, Elo + Glicko-2 heads-up) in the completion transaction, final ratings (Bradley-Terry per tournament), tournament completion, graceful shutdown, metrics endpoint, cross-process rate limit | Done, db-tested |
| Swiss tournaments, multi-seat table tournaments, pause/resume on failed matches | Done, db-tested |
| Liar's Dice engine + bots (2-6 seats) | Done, unit-tested, replay renderer |
| Werewolf engine + bots (7 seats), role balance, claim metrics | Done, db-tested on bots, replay renderer, proven by a real run (tournament 6, see Gotchas) |
| `werewolf_dayfirst` (Werewolf opening on day 1) | Done, db-tested on bots, replay renderer, proven by a real run (tournament 7, see Gotchas) |

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env && cp roster.example.toml roster.toml   # then edit both
docker compose up -d db testdb
scripts/install-hooks.sh                 # pre-commit hook: runs scripts/check.sh

scripts/check.sh                         # ruff, format check, pytest; db tests too if DATABASE_URL is set
pytest                                   # unit tests; db tests skip without DATABASE_URL
pytest -m slow                           # balance simulations, excluded by default
DATABASE_URL=postgresql://arena:arena@localhost:5433/arena_test pytest -m db
ruff check arena tests && ruff format arena tests

arena play connect4 --p1 random:1 --p2 random:2           # free, in memory
arena migrate
arena tournament --game connect4 --players random,haiku-5.5/c4-v1 --games-per-pair 10 --budget 2
arena tournament --game liars_dice --players a,b,c,d --format tables --table-size 3 --tables 12
arena tournament --game werewolf --players a,b,c,d,e,f,g --tables 1
arena tournament --game werewolf_dayfirst --players a,b,c,d,e,f,g --tables 1
arena worker --concurrency 4             # Ctrl-C any time; matches resume elsewhere
arena worker --record recordings         # also append every model reply for fixtures
arena leaderboard --game connect4
arena serve                              # http://localhost:8000
arena export --out site                  # static HTML + JSON for GitHub Pages
arena export --out docs --tournaments 6,7 --matches 116,142   # only those pages
python -m arena.balance --focus 0.7      # Werewolf what-if rulesets, free, no database
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
  games/werewolf.py     hidden roles, free text, phases, role-claim classifier; also
                        WerewolfDayFirst, the same game opening on day 1
  engine/match_runner.py  MatchRunner, iter_states (replay), TurnStore, LeaseLost
  players/              Player protocol, RandomPlayer/ScriptedPlayer, LLMPlayer, prompts
  players/prompts/<game>/<version>/{system,turn}.txt
  providers/            Provider protocol, anthropic/openai adapters, retry, ratelimit, mock
  store/                asyncpg pool, migrations runner, queue (claim/heartbeat/complete),
                        PostgresTurnStore (fenced), tournament creation, leaderboard
  worker.py             claim → run with heartbeat → complete / release for retry
  api.py                FastAPI: JSON API + HTML routes
  web.py, templates/    Jinja2 page builders (pool, Urls) -> HTML; reused by export; all
                        styles live in templates/site.css, inlined into every page
  live.py               Server-Sent Events stream of one match's turns
  export.py             arena export: same builders with flat file Urls, plus api/*.json
  store/read.py         read queries shared by the API, pages and export
  roster.py             roster.toml → PlayerConfig → Player (+ Pricing)
  costs.py, ratings/{elo,glicko2,weng_lin,bradley_terry}.py, cli.py, config.py
  tournaments.py        pure scheduling: round_robin, Swiss pairing, table_schedule
  balance.py            Werewolf what-if rulesets, simulated; not a game, nothing plays them
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
   up attempts; `max_attempts` (default 3) invalid attempts = forfeit: a loss in Connect 4,
   out of the game in Liar's Dice, a skipped action in Werewolf.
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
3 LLM players. v1 uses Elo. Post-v1, Weng-Lin ranks the leaderboard because it rates every
table size; Glicko-2 and Elo stay beside it as heads-up-only extras and are always shown.

Items tagged `[human]` need real API calls or a decision from the user. Unattended
runs (loops, scheduled tasks) skip them.

- [x] **Phase 1**: contracts, Connect 4, match runner, players, providers, CLI
- [x] **Phase 2**: schema, queue/leases, fenced turn store, worker, budget cap, read API
- [x] `[human]` **First real run** (2026-10-30): `haiku-5.5/c4-v1` vs `haiku-4.5/c4-v1`
      (Anthropic-only, no OpenAI key), 20 games, `--budget 1`, $0.2277 spent. Recorded with
      `arena worker --record`; one game is the fixture in `tests/fixtures/`.

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
- [x] Replay page: every turn's `spectator_view` frame embedded as JSON; plain-JS
      prev/next/play stepping; one small renderer function per game: Connect 4 (board),
      Liar's Dice (all dice, bids, reveals).
- [x] `arena export`: render the same templates to static HTML + JSON files for GitHub
      Pages ($0 hosting). No server needed to browse the result.
- [x] Live match view via Server-Sent Events: `/api/matches/{id}/events`, followed by the
      replay page while a match is queued or running.

**Phase 5: v2 games**
- [x] Liar's Dice engine, `LiarsDiceBot`, prompt `liars_dice/v1`: hidden dice, round r's
      dice from `Random(f"{seed}:{r}")`; first real test of `player_view` hiding.
- [x] Multiplayer ratings and pairing: Weng-Lin Plackett-Luce on every match, placements
      on matches, `--format tables` (round-robin, Swiss, Elo and Glicko-2 stay heads-up).
- [x] Werewolf engine + scripted bots (flagship): 7 seats, multi-phase turns (night →
      discussion → vote), per-seat views, discussion log in state reduced only past a token
      limit, scheduled role balance, role-claim metric, `bot` roster provider.
- [x] `[human]` First real Liar's Dice and Werewolf tournaments (2026-10-30): Liar's Dice
      tournament 4, 30 matches, $0.2833; Werewolf tournament 6, 40 tables, 8 players, $1.4761.

**Maintenance**
- [ ] Default pytest run takes ~55s, ~20s in collection; profile imports
      (`python -X importtime`) and lazy-load heavy modules.
- [ ] Simulator: replace fixed p with per-day accuracy fitted from tournaments 6–7.

## Unattended runs (loops and scheduled tasks)

When working without the user watching (a `/loop`, or a cloud scheduled task):

1. Work on **one** roadmap item per run: the first unchecked item not tagged `[human]`.
   If an open `claude/` branch or PR already covers it, continue that instead.
2. Work on a branch named `claude/<short-item-name>`. Never commit to or merge
   into `main` without the user's explicit approval. Never push from a local loop. 
3. Before committing: `ruff format arena tests`, then `scripts/check.sh` with
   `DATABASE_URL` set to the test database when Postgres is available. It must pass;
   always use it rather than running the steps by hand, and never pipe test output
   through `tail` or `head` without `set -o pipefail`: the pipe's exit code hides the
   failure. If db tests can't run, say so in the PR description.
   Never use `git commit --no-verify`; if the hook blocks a commit, fix the cause.
4. Tick the item's checkbox in this file in the same branch, and add any new gotchas.
5. Open a PR (cloud) or leave a commit summary (local) describing what changed, how it
   was tested, and anything left undone.
6. **Stop and report instead** if the item needs real API calls, a new dependency
   beyond the dev tools, a change to an invariant above, or a design choice the
   roadmap doesn't settle. Write the open question in the PR or the final message.
7. Never set or read API keys, never run `arena play`/`arena worker` with LLM players,
   and never edit an existing migration or prompt version.

## Adding a game (checklist)

1. `arena/games/<name>.py`: `class <Name>(Game)` implementing every method of `Game` in
   `games/base.py`. Subclass it explicitly to inherit the default `placements()`; override
   that when finishing order says more than scores do.
2. Register it in `games/registry.py`.
3. Unit tests: win/loss/draw, illegal actions, `apply` doesn't mutate, `player_view`
   hides other seats' private info, and replay determinism (same seed + actions → same
   final state).
4. `RandomPlayer` vs `RandomPlayer` through `MatchRunner` (needs `legal_actions_hint`).
   For free-text actions, add a scripted bot instead.
5. Prompt in `players/prompts/<name>/v1/`.
6. A small JS renderer for its `spectator_view` frames on the replay page (one function
   per game, no framework), a move label formatter in `web.MOVE_LABELS`, and a display name
   in `web.GAME_NAMES` (a test checks every registered game has one).
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
  before that existed are unrated. Weng-Lin rates every match at any table size; Elo and
  Glicko-2 update only with exactly 2 seats (their history rows show before == after
  otherwise). A player seated twice in one match is never rated.
  Ratings rows are locked in `player_id` order; keep that order to avoid deadlocks.
- Weng-Lin (`ratings/weng_lin.py`): Plackett-Luce with one seat per team, mu 25, sigma
  25/3, beta 25/6, kappa 1e-4. No tau (openskill's additive dynamics): skill is fixed by
  config_hash, so sigma only shrinks. The leaderboard and Swiss seeding use the ordinal
  mu - 3*sigma. `tests/test_weng_lin.py` holds reference values from openskill 6.2.0
  with `PlackettLuce(tau=0.0)`, which is not a dependency. After applying migration 0005
  run `arena ratings rebuild` once: existing rows start at the Weng-Lin defaults.
- Werewolf biases Weng-Lin: wolves and villagers are asymmetric teams, and ties at rank 1
  rate each winner individually with no idea of which side they were on. The mitigation
  is balancing roles per player and showing win rate by role beside the rating; the
  maths is unchanged, and multi-player teams aren't modelled in `rate`.
- Placements: per-seat ranks on `matches.placements`, 1 is best, ties allowed.
  `Game.placements()` defaults to competition ranking of `scores()` (1, 2, 2, 4); only
  their order matters to ratings. `Game.final_placements` then moves forfeits below
  everyone with `rank_forfeits_last`, so engines ignore forfeits in `placements`; Werewolf
  overrides it, because a Werewolf forfeit removes nobody. Tournament points still come
  from `scores`, never placements. Liar's Dice records `eliminated` in state for this.
- `--format tables`: `table_schedule` is pure. Players' game counts and each player's
  seat counts always differ by at most 1 (seats are an edge colouring, so that's exact,
  not searched). Pair meetings differ by at most 1 only if the annealing finds such a
  schedule within its step budget. Some configs have none (6 players, 5 tables of 3),
  and large ones can miss (12/6/22, 24/6/40): `create_tables` logs a warning and uses
  the most even schedule found. The whole schedule is queued at once, in no rounds.
- Glicko-2: every match is its own rating period, tau = 0.3, and RD never grows from
  inactivity (skill is fixed by config_hash). After applying migration 0003 run `arena ratings rebuild` once: existing rows start at
  Glicko defaults. Rebuild replays by (completed_at, id), and `queue.complete` re-stamps
  completed_at with clock_timestamp() under the rating locks so that order is exact.
- Tournaments advance in the transaction that completes a match (`advance_tournament`):
  once nothing is queued, running or failed, Swiss queues the next round or the
  tournament completes. A budget-capped tournament with queued matches stays `running`.
  Lock order in a completion: match row, ratings rows, tournament row last.
  Lock the tournament `FOR NO KEY UPDATE`, never `FOR UPDATE`: the completed_at re-stamp
  re-runs the matches→tournaments FK check (KEY SHARE), and upgrading that deadlocks.
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
  stateless (decide from the view). Bots reach Postgres tournaments through the roster:
  `provider = "bot"`, `model = "liars_dice"` or `"werewolf"`, each with its own `seed`
  (identity is `bot-<model>-<seed>`, and the players table keeps those unique).
- Werewolf forfeits skip the action and don't eliminate (changed after match 67, where
  the first design removed a seer for three illegal targets): a night action is lost, a
  vote abstains, a discussion turn is silent, and the seat stays alive, so it can forfeit
  again. If both wolves forfeit, the kill is drawn from the seed, which keeps the game
  finite. Placements ignore forfeits; `werewolf_metrics` counts them. Replaying match 67
  now shows its seer killed by the wolves, not forfeited, with the same winner; its stored
  placements still rank the seer last.
- Werewolf: exactly 7 seats (2 wolves, seer, 4 villagers); the engine docstring has the
  rules. `player_view` has no `to_move`: at night it would expose the wolves and seer.
  Only the night kill draws ties (`Random(f"{seed}:night:{n}")`); a tied day vote
  eliminates nobody, so wolves killing every night is what guarantees the game ends.
  Random players can't play it: discussion is free text, so `legal_actions_hint` is None.
- `Game.normalize_action` runs after validation and the result is what enters the log;
  if it differs, the turn gets `truncated=true` (the full text stays in `raw_response`).
  Werewolf messages over 300 characters are truncated, never invalid.
- `matches.options` goes through `MatchSpec` to `Game.initial_state`, so it's part of
  replay. `Game.role_pool()` opts a game into `assign_roles`, which `create_tables` calls
  and stores as `options.roles`; without options Werewolf shuffles from the seed. Role
  counts differ by at most 1 per role when the search finds that, else it logs a warning.
  Any new read of `initial_state` for a stored match must pass the options.
- Werewolf style prompts: `werewolf/v1-plain`, `-cautious`, `-aggressive`, `-analytical` are
  `werewolf/v1` plus one play-style paragraph, so both Haikus give 8 distinct players
  (`haiku-5.5/ww-v1-plain`, ...). `werewolf/v2-*` add one line asking for short messages.
  The `maxLength` in the tool schema is only a hint: truncation still enforces the limit.
  Prompts are read as UTF-8, whatever the platform's default. A table needs 7 distinct ids: `table_schedule` refuses
  duplicates and identical configs can't share the players table. `--tables` alone means
  `--format tables`, and `--table-size` is optional when a game seats one number. With more
  ids than seats, a single table seats a seeded 7 and the rest sit out.
- Werewolf context: full text by default. `context_token_limit` (option, default 20,000,
  estimated at 4 characters a token over rules + view) reduces the oldest finished days
  to votes and deaths inside `player_view`. The current day always stays in full, so a
  low limit can still be exceeded. A 7-seat game doesn't approach the default: it needs
  longer games or an explicit low limit.
- Role claims (`werewolf.classify_claims`, reported by `store/metrics.werewolf_metrics`,
  `/api/werewolf/roles` and the index page) are literal matches only: "I am a/the X",
  "I'm just a X", "as the X, I", "player/seat N is X", "Player N here, X". A role word followed by a hyphen never matches ("I'm wolf-hunting").
  Paraphrases, negations and hedged wording are missed, and
  quoted or hypothetical phrasing is counted as if it were a claim.
- Pages: `web.py` page builders take `(pool, Urls)` and return HTML; FastAPI wraps them
  and `arena export` should call the same builders with file-path `Urls`. Never hard-code
  an href in a template: use `url('match', id=...)`. Templates use StrictUndefined, so a
  missing field raises (tested in `tests/test_web.py`); test pages by calling the builders
  (no `TestClient`: it needs `httpx`). Read queries live in `store/read.py`, shared with
  the JSON API so both always show the same data.
- Replay: `templates/replay.js` is inlined into the match page (a Markup global, not
  `{% include %}`: JS braces clash with Jinja), so pages stay single-file for export. It
  must never contain the text `</script`. Frame data is embedded with `| tojson`, which
  escapes `<>&`. A new game adds one function to `RENDERERS` (escape everything with
  `esc`) and a move formatter to `web.MOVE_LABELS` (turn table + replay info line;
  unknown games fall back to sorted JSON, since jsonb reorders keys). `tests/test_replay_js.py` runs the real file in Node against engine frames,
  including stepping on a fake DOM; it is skipped when `node` isn't installed.
- Export: pages are written flat (`index.html`, `tournament-7.html`, `match-3.html`) so
  relative links work from every page with no `../`; JSON mirrors the API under `api/`.
  A new page route needs an entry in `export.FILE_ROUTES` too (a test checks the keys
  match `SERVER_ROUTES`). Every match gets a page, but `matches.html` lists only the
  latest 50, like the server. Export writes over files of the same name and deletes
  nothing. asyncpg returns `sum()` over bigint as Decimal; `_json_default` turns it into
  a number.
- UI work: `.claude/skills/` holds three skills copied from `emilkowalski/skill` (MIT, licence
  beside them): `emil-design-eng` for motion and component rules, `review-animations` to review
  motion, `break-ui` to test against worst-case data. Stepping a replay stays instant, name the
  property in every `transition`, and gate hover behind `(hover: hover) and (pointer: fine)`.
  Before merging a page change, run `npx impeccable detect <url>` on the rendered page with
  `DO_NOT_TRACK=1`, and check 320px: headless Chrome won't go that narrow, so measure in a
  same-origin iframe. Impeccable is run this way only; it isn't installed in the repo.
- Themes: light parchment, dark moonlit, following the system until the nav toggle saves a
  choice. `site.css` has two identical dark blocks (system, toggle) because CSS can't share
  one; tests fail if they differ, or if any text token drops below WCAG AA (4.5:1) on `bg`
  or `surface`. The theme is set in `<head>` before paint, and switching never animates.
  Replay stepping stays instant. No 3D anywhere: the user removed it, and a test holds that.
- Colour by job: `--g-*` and `--mark-*` are chart marks, separate from text tokens, checked
  with the dataviz skill's `validate_palette.js` against `surface` in both themes; re-run it
  if one changes. Words stay in text colours, with a coloured dot beside them. Each game has
  a colour and a pure-CSS pattern (`.banner.g-<game>::before`), so a new game needs one, plus
  `web.GAME_BLURBS`; tests check both. Text in `GAME_BLURBS` and the home page's rule-change
  section is hand-written, while its numbers are live.
- Page wash: `--wash-1` to `--wash-3` colour the whole page in each theme, under the glows and
  wallpaper; text is tested against all three, so a new wash must keep AA.
- Page wallpaper: `body::before` masks a game's motif tile (`--tile-<game>`, SVG data URIs in
  `site.css`) over its colour; home mixes all four. A new game needs a tile and a body rule.
- `read.match_detail` puts `claims` on each Werewolf turn for the reveal's truth overlay.
  That's spectator data: `blindSteps` must never read it, and a test feeds it false claims to
  prove it can't reach the blind view. A false claim is a bluff from a wolf or about the
  speaker's own seat; a villager's false accusation is shown as wrong, not as a lie.
- `web.baselines` folds players named `random` or `random<n>` under each leaderboard. They
  stay on the page and in the JSON, and everyone else is still ranked by the same numbers.
- Match tables say who won (`web.match_result`), not raw scores, and a table tournament's rows
  drop the seat count. Werewolf results come from the score sum: 2 is wolves, 5 is village.
- Play blind (`match-116.html#blind`, Werewolf, finished matches only): `replay.js` sits you in
  as a villager. Spectator frames hold every role, so `blindSteps` copies out public facts
  only; `test_blind_steps_depend_on_nothing_a_villager_cannot_know` destroys the hidden
  fields and requires identical output, and any new step field must keep that test passing.
  The data is still in the page, so this keeps a game honest, not secret. Per Emil Kowalski's
  rules, stepping stays instant and only the final reveal animates.
- `RENDERERS.werewolf_dayfirst` must be set before `initReplay` runs. It was set after at
  first, so day-first replays opened on raw JSON, which tests missed by requiring the whole
  file before rendering. `npx impeccable detect <url>` found it as a `pre` overflow.
  Scan rendered pages: file scans don't execute the page, and found nothing here.
- Partial export (`--tournaments`, `--matches`): `export.PartialUrls` returns "" for a page
  left out, and templates use the `link` macro so it shows as plain text. A new link to a
  match or tournament page must use that macro too, or a partial export gets a dead link.
  `docs/` is that export, for GitHub Pages: regenerate it with the command above, and scan
  it before publishing, since replays show every role and every message.
- Live view: `live.match_events` polls `read.match_progress` once a second per viewer and
  replays the log only when the turn count moves, so the turn log stays the only source
  of truth (no LISTEN/NOTIFY). An event's id is the turn count, so EventSource resumes from
  Last-Event-ID. A retry keeps the stream open; only `completed` and `failed` end it, and
  resuming a failed match needs a page reload to watch it. Like replays, the stream shows
  spectator frames: every role and die, live. Keep that in mind if the server is ever
  public while models play. New turns reach the replay controls, not the server-rendered
  turn table: the page says to reload once the match ends. Exports set the route to ""
  (`FILE_ROUTES`), and the page only sends a URL while a match is queued or running.
- Recordings: `--record` (`arena play`, `arena worker`) writes one file per vendor per
  process, so two players of one vendor share a file; each line's `model` tells them
  apart, and files recorded before that field still load. With `--concurrency` above 1,
  lines from different matches interleave, so record fixtures at concurrency 1. Only
  successful completions are written: failed calls never reach RecordingProvider.
- First real run (tournament 3, 20 Connect 4 games, Haiku 4.5 12-8 Haiku 5.5):
  the first player won 16 of 20, so seat order drowns out most model differences. Connect 4
  needs hundreds of games to separate similar models; 20 only proves the pipeline. Haiku
  4.5 doesn't cache at this prompt length (0 cache reads or writes in 196 calls) while
  Haiku 5.5 hit 64%, so with its 10x prices it cost 19x as much. All 56 rejected attempts
  were "column 3 is full", though the prompt lists legal columns. `max_tokens = 256` was
  enough: every recorded reply had a tool call.
- First real Liar's Dice run (tournament 4, 30 matches, $0.2833): the probability bot went
  16-4, and the Haikus finished level at 7-13 (5-5 head to head, 2-8 each against the bot).
  No illegal moves in 351 attempts. Haiku 5.5 hit 71% cache, Haiku 4.5 0%, 22x the cost.
  From the turn log, Haiku 5.5 / Haiku 4.5 / the bot: bids impossible given their own dice
  1 of 100 / 0 of 102 / 0 of 95; challenges of a bid their own dice already proved true
  15 of 68 (22%) / 12 of 81 (15%) / 0 of 63; challenges won 18% / 14% / 35%. Losing
  challenges, not wild bids, are where the models give up dice.
- First real Werewolf run (tournament 6, 40 tables of 7 from the 8 `ww-v2-*` players, $1.4761,
  25 minutes): wolves won 33 of 40, on 2 days (26) or 3 (14). Each player got exactly 10 wolf,
  5 seer and 20 villager games. Wolves won 90% on Haiku 5.5 and 75% on Haiku 4.5, seers 25% and
  10%, villagers 21% and 14%; that gap is p = 0.08 overall, and tables share results, so it's
  not proven. Haiku 4.5 aggressive won 5 of 10 as a wolf, everyone else 8 or 9. Weng-Lin
  ordinals ran from 5.20 to -0.41 with sigma near 7, so the order means little.
  No forfeits, and 3 rejected attempts in 1,246 (Haiku 5.5 seers); all 3 were inspecting the
  seat voted out that day, as in match 67. `render_view` lists a vote-out above that day's
  discussion. The 300-character line worked: 2 of 676 messages truncated (6 of 12 in match
  67), mean 194 characters. The API accepts `maxLength` on a non-strict tool. Threshold never
  reached (largest estimate 1,974 tokens). Haiku 4.5 cost 10x Haiku 5.5 ($1.34 vs $0.14) with
  no cache hits.
  Claims: 167 true, 52 false (49 wolves posing as villagers, none claiming seer), 2 leaks, and
  neither is deliberate: a hypothetical in match 96 and seat 2 accusing "Player 2" in match
  102. 111 of the 167 are Haiku 5.5 villagers' "Player N here, villager" habit, so true-claim
  counts say more about style than honesty.
- First real day-first run (tournament 7, 40 tables, the 8 `wwdf-v1-*` players, $2.4901): the
  village won 17 of 40, 42.5% (Wilson 95% interval 28.5-57.8%), against 41.4% from the simulator
  and 7 of 40 on the current rules (p = 0.027). Turns matched too: 43.3 per game, predicted
  43.3. No forfeits, 4 rejected attempts in 1,735 (3 Haiku 4.5 self-votes on day 1, 1 seer
  inspecting the seat just voted out), 12 of 1,068 messages truncated (10 from Haiku 5.5
  analytical). 186 true claims, 73 false (65 wolves posing as villagers, 8 seers hiding), 0 leaks.
  Threshold never reached (largest estimate 2,325 tokens). Haiku 4.5 cost $2.26, Haiku 5.5 $0.23.
- Village votes on a wolf, by day, tournament 6 / tournament 7: day 1 86/160 (54%) / 73/200
  (36.5%); day 2 34/63 (54%) / 66/133 (49.6%); day 3 5/6 (83%) / 28/36 (77.8%). Chance on day 1
  is 40% / 33%, so a day-first first vote is close to chance, with nobody dead and nothing
  inspected. The first vote removed a wolf 40% / 30% of the time.
- Haiku 4.5 aggressive replicated: flagged after the fact in tournament 6 (5 of 10 as a wolf,
  everyone else 8 or 9), then fresh games in tournament 7 gave 2 of 10 as a wolf (p = 0.015) and
  8 of 35 overall (p = 0.003), last on ordinal both times (-0.41, -1.06). Cautious led
  aggressive 59% to 34% in tournament 7 but didn't stand out in tournament 6 (39%). Haiku 5.5
  against Haiku 4.5 is unproven: 51% to 43% (p = 0.23), teammates sharing results.
- Simulator caveat: its 41.4% matched for partly the wrong reason. It assumed p = 0.54 every
  day and a wolf out on 46% of first votes; tournament 7 had 36.5% and 30%, with later days
  making up the difference. Fixed p got the total and missed the path, so don't trust it for
  rulesets that change when information arrives. Moving p by 0.10 also moves day-first by about
  15 points either way. The roadmap has the fix: per-day accuracy.
- Crash recovery in practice (tournament 7, 2026-10-30). Two real interruptions, no failed
  match. 23:17 to 23:49: everything on the machine stopped for 32 minutes, most likely sleep
  (not confirmed by Windows' power log), so leases expired; when it woke, 2 stale writes were
  fenced (matches 112 turn 24, 113 turn 9), and the same process reclaimed both and re-asked those
  turns. 23:50:15: the worker was killed for low memory with 113 and 114 mid-game; a new
  process at 23:58:52 resumed them at turns 34 and 12. Afterwards all 40 matches had
  continuous turn numbers with no duplicates, and all three replayed cleanly. Recordings hold
  1,737 replies against 1,735 persisted attempts; matched line by line, the 2 extra are exactly
  the fenced turns, $0.0006 of $2.49 (0.02%), and the model wrote different text when asked again.
  `turns` never sees that money, so reported spend is short by it. The kill lost no recorded
  reply, though a request in flight at the kill can't be seen in recordings (at most 2, under a
  cent). Match 113 used 3 of its 5 run attempts.
- `werewolf_dayfirst` is `Werewolf` opening on discussion, so the first kill and inspection
  come on night 2 (night n still precedes day n, and there is no night 1). It exists so
  tournament 6 stays valid: `werewolf` keeps its rules, and ratings are per game. Its
  `v1-*` prompts are byte copies of `werewolf/v2-*`, because no prompt mentions night; the
  rules reach them through `$rules`. Bots use `model = "werewolf"` for both. A new game id
  also needs `metrics.WEREWOLF_GAMES` (else its role stats are silently empty),
  `web.MOVE_LABELS` and `RENDERERS` in `replay.js`. Don't compare ordinals across the two.
- `scripts/check.sh` stops at the first failure, and refuses any `DATABASE_URL` whose
  database isn't `arena_test`, because `test_store_db.py` drops the schema. The hook comes
  from `scripts/install-hooks.sh`, per clone, and checks the working tree rather than the
  staged files, so unstaged edits count. A commit waits for the whole run (over a
  minute, about two and a half with db tests). Git sets `GIT_DIR` while a hook runs, so a test that runs
  git must strip `GIT_*` (see `tests/test_check_script.py`) or it hits this repository.
- `addopts` excludes `slow`; a `-m` on the command line replaces it, so `pytest -m db` and
  `pytest -m slow` both work. Plain `pytest` keeps a few-game smoke test of the simulator.
- Werewolf balance: a non-wolf voted out on day 1 means the next kill reaches parity, so the
  game is lopsided. Tournament 6 village votes hit a wolf 125 of 229 times (54%), but they
  bunch: one seat took 5 of 6 votes on 23 of 40 first days. So `arena/balance.py` has `focus`,
  how much a table votes together, which leaves each voter's p alone. Independent voters
  (`focus = 0`) tie far too often (41% against a real 12%) and give the village 5%. `focus =
  0.7` was fitted to first-vote outcomes only (40/49/11 wolf/other/tie, real 40/48/12) and
  gives 22%, against the real 7 of 40 (Wilson 95% interval 9-32%). The real engine with
  `AccuracyVoteBot` agrees with the simulator (`tests/test_balance.py`). p is fixed by
  assumption, so seers and doctors add only their rule effects, never skill. Any adopted
  ruleset gets a new game id, so its ratings don't mix with tournament 6. See the simulator
  caveat above before trusting a prediction.
- Live ratings depend on play order, and that run proved how much: pairings played as blocks
  put Haiku 4.5 148 Glicko points above Haiku 5.5 on identical records. Over 200 random
  orders of the same 30 results the gap averaged -0.2 (sd 64, p5 -100, p95 +98), split
  104/96, and the real order beat 99% of them. Weng-Lin ordinal: +6.66 against mean 0 (sd
  2.9). So `create_round_robin` now shuffles matches (seeded), and final ratings exist.
- Final ratings (`ratings/bradley_terry.py`, `store.ratings.final_ratings`): a maximum-
  likelihood fit to one finished tournament, computed on read, no table. The leaderboard
  shows the game's latest heads-up tournament and still ranks by live ordinal, because not
  every player has a final rating. Heads-up only; a table of N isn't N-1 independent
  games. No finite MLE exists if the win graph isn't strongly connected (anyone unbeaten
  or winless), and then there's no final rating rather than one invented by a prior.
  Finals average 1500 within their tournament, so never compare them across tournaments.
- `turns.rejections` holds every rejected attempt of a turn; `error` stays as the last
  reason, including when a retry succeeds. Rows from before migration 0007 are empty,
  except tournament 3, restored once from its recording.
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
