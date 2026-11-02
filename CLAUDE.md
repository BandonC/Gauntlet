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
| Code Break (deduction race), minimax solver, cancellable tournaments | Done, db-tested, replay renderer, proven by a real run (tournament 13, see Gotchas) |
| Split the Deal (negotiation), conceding bot, value-claim metrics | Done, db-tested, replay renderer, proven by a real run (tournament 14, see Gotchas) |

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
arena tournament --game connect4 --players a,b --budget 2 --spend-mode sharded
arena tournament cancel 12 --reason "..."   # keep the data, drop it from ratings
arena worker --concurrency 4             # Ctrl-C any time; matches resume elsewhere
arena worker --record recordings         # also append every model reply for fixtures
arena worker --metrics-port 9100         # also serve Prometheus metrics on /metrics
docker compose --profile observability up -d   # Jaeger :16686, Prometheus :9090, both optional
arena leaderboard --game connect4
arena serve                              # http://localhost:8000
arena export --out site                  # static HTML + JSON for GitHub Pages
arena export --out docs --tournaments 6,7,8,9,10,11,13,14 --matches 116,142   # only those pages
arena bench --matches 5000 --workers 1,2,4,8   # scaling table, bots only, own database
arena chaos --matches 2000 --workers 4   # kill workers, freeze Postgres, check invariants
python -m arena.balance --focus 0.7      # Werewolf what-if rulesets, free, no database
python -m arena.dice_sim sweep           # Liar's Dice bot tuning (also field, evaluate), free
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
  web.py, templates/    Jinja2 page builders (pool, Urls) -> HTML; reused by export; table
                        sorting and filtering live in templates/site.js; all
                        styles live in templates/site.css, inlined into every page
  live.py               Server-Sent Events stream of one match's turns
  export.py             arena export: same builders with flat file Urls, plus api/*.json
  bench.py              arena bench: real worker processes draining bot matches
  chaos.py              arena chaos: bench under kills and `docker pause`, invariant checker
  telemetry.py          OpenTelemetry spans, Prometheus metrics, TracedPlayer
  store/read.py         read queries shared by the API, pages and export
  store/spend.py        exact and sharded spend, TOTAL_SPEND_SQL, BudgetGuard
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
   hash, temperature, max_tokens, plus tool_choice and thinking settings when set)`. Any change
   = new roster id;
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
- Sonnet 5.5, Opus 5.5 and Fable 5.1 reject forced tool use with a 400, on `messages.create` and
  `count_tokens` (checked 2026-10-31, both); Sonnet 5, Opus 5, Fable 5 and Haiku 5.5 accept it.
  Roster `tool_choice = "auto"` is for those three. A forced reply has no thinking block.
- `thinking_budget` sends `budget_tokens`, which the docs list as a 400 on Sonnet 5.5, Opus 5.5,
  Fable, Sonnet 5, Opus 5, Haiku 5.5 and Opus 4.7-4.8. `count_tokens` doesn't check this: it
  answered 200 for Sonnet 5.5, so it can't prove a thinking setting. Those models think by
  default when `thinking` is omitted, and `max_tokens` is the only hard limit, including thinking.
- `thinking_mode = "between_tools"` is the lowest thinking setting on Sonnet 5.5 and a 400 on
  other current models, and at effort `xhigh` or `max`. Roster `effort` exists so that rule can
  be enforced; effort is a signal, not a limit, and no player set it when it was added.
  It isn't an off switch: `disabled` is a 400, and in the real test games thinking tokens were 0
  while reasoning came back as visible text beside the tool call, still billed as output.
- `turns.stop_reasons` has one entry per attempt, in the provider's own words, so `max_tokens`
  (cut off) can be told from `end_turn` (skipped the tool). Turns from before it are empty.
- `turns.reply_blocks` and recordings keep each attempt's content blocks: type, characters, and
  the words of text blocks only. Thinking is size only and signatures are never stored. It stays
  out of `read.match_detail` on purpose: text can be private reasoning, and match data is
  embedded in pages and exported. The adapter used to drop text when a reply had a tool call.
- `usage.output_tokens_details.thinking_tokens` is a breakdown of `output_tokens`, which stays
  the billed total. It lands in `Usage.thinking_tokens` and `turns.thinking_tokens`; 0 means none
  reported. `costs.py` never reads it, and a test holds that.
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
- [x] `liars-dice-bot-v2`: same rule as v1, tuned threshold, chosen with `arena.dice_sim`.
- [x] `[human]` First real Liar's Dice and Werewolf tournaments (2026-10-30): Liar's Dice
      tournament 4, 30 matches, $0.2833; Werewolf tournament 6, 40 tables, 8 players, $1.4761.

### Phase 6: Stronger models (paid, pre-approved within budgets)
- [x] Werewolf: when `action` is missing and exactly one action is legal, fill it in
      and record the completed action plus `auto_filled: true` on the turn; a present
      but wrong action is still rejected. Count auto-fills per player in reports so the
      quirk stays visible. Old logs must still replay; prompts and identities unchanged.
      Verify by replaying the Stage 2 test recording: the 10 rejections would be accepted.
- [x] Werewolf (werewolf_dayfirst) with stronger models: players as in Stage 2
      (roster.toml already has them). Run a fresh 7-seat test game (--budget 0.50) to
      confirm the fix on real calls, then 40 tables. Budget: $12.
- [x] Liar's Dice round-robin: sonnet-5.5/ld-v1-think, sonnet-5/ld-v1, liars-dice-bot-v2,
      30 games per pair. Test game first: Sonnet 5.5 vs v2. Budget: $5.

### Phase 7: Scale and reliability (bots only, no API spend)
New dependencies pre-approved for this phase: opentelemetry-api, opentelemetry-sdk,
opentelemetry-exporter-otlp, prometheus-client.
- [x] Verify the README quick start from the real GitHub URL: clone into a scratch dir,
      run the keyless bot tournament, check the pages load. Fix anything that breaks.
      Passed 2026-11-01 at `9b6119a` with no credentials: 20 games, five local pages 200,
      nothing to fix. GitHub Pages wasn't enabled then, so the README's site links were 404.
- [x] `arena bench`: queue N bot matches and run M worker processes; report throughput
      (matches/s, turns/s), queue wait and claim latency p50/p95/p99, and DB connections
      used. Produce a scaling table for M = 1, 2, 4, 8 at N = 5,000. Bot matches only.
- [x] `arena chaos`: run a bench while randomly SIGKILLing workers and pausing Postgres
      for a few seconds (`docker pause`), then verify invariants: every match completed
      exactly once, turn logs continuous with no duplicates, no match rated twice,
      tournament spend equals the sum of turn costs. Exit non-zero on any violation.
      Add a short version as a slow test.
- [x] Graceful SIGTERM in the worker, if not already done: stop claiming, finish
      persisting the current turn, release the lease, exit. Test it under chaos.
- [x] Observability: OpenTelemetry traces (match → turn → provider call) and a
      Prometheus /metrics endpoint (matches claimed/completed/failed, turn latency,
      provider errors, retries, spend). Optional Jaeger + Prometheus services in
      docker-compose under a profile, so the default setup stays light.
- [x] Write up results: a "Scale & reliability" README section with the scaling table,
      chaos results (kills survived, invariants held) and a trace screenshot.
- [x] [human] Decide whether to deploy a hosted read-only API (costs money monthly).
      Decided 2026-11-01: no. GitHub Pages already covers it, with no monthly cost.

### Phase 8: Sharded spend (bots only, no spend)
- [x] Sharded spend counters: per-worker-slot spend rows summed for the budget check, with a
      documented overshoot bound (worker slots × max turn cost); keep the exact single-row
      mode as an option. Rerun arena bench for M = 1, 2, 4, 8 in both modes and add the
      before/after table to the README's Scale & reliability section. Rerun a chaos check
      in sharded mode.

**Project spend cap: $80 total (all real API spend, including in-memory games).**
Before every paid step, compute the project total so far plus the step's --budget.
If that exceeds $72, stop and ask. Report the project total in every final message.

### Phase 9: New capabilities
Each game: engine + tests + bots + prompt v1 + replay renderer + metrics, then a
Haiku 5.5 test game (--budget 0.20), then a tournament with haiku-5.5, sonnet-5 and the
best bot. Budget per game tournament: $4. Same Game contract; no invariant changes.
- [x] Code Break (deduction): 4 pegs, 6 colours, repeats allowed, 10-guess limit. Two
      players race on the same seeded secret; fewer guesses wins, a tie is a draw.
      Feedback = exact/colour-only counts. Bots: random-consistent and an optimal
      minimax solver (≤5 guesses). Metrics: guesses above the solver's count, and
      inconsistent guesses (guesses that contradict earlier feedback).
- [x] Split the Deal (negotiation): 3 item types, seeded quantities 1–4; private
      per-unit values (seeded, each player's total value is 10). Alternate offers with a
      ≤200-char message, at most 6 offers each; no deal = 0. Score = own value / 10.
      Bot: concedes linearly. Metrics: deal rate, efficiency, and lies about values
      (literal matching).
- [ ] Word Link (team semantic game): 4 seats, 2 teams of clue-giver + guesser; 16
      words from an original word list in the repo: 6 per team, 3 neutral, 1 trap.
      Clue = one word + a number; the guesser picks one at a time and may stop; trap =
      loss. Weng-Lin teams. Bots: random guesser, and a clue bot using a hand-made
      association table. Metrics: words per clue, trap hits, clues that break the rules.
- [ ] README: a "What each game tests" table and findings per game, with intervals.

### Phase 10: Paid experiments
- [ ] Effort sweep: sonnet-5.5 Liar's Dice vs liars-dice-bot-v2 at effort low, medium,
      high (tool_choice auto, max_tokens 2048), 30 games each. Report win rate, challenge
      blunders and thinking tokens. Budget: $8.
- [ ] Prompt intervention: liars_dice/v2-prob (estimate the probability the bid is true
      before acting). haiku-5.5 and sonnet-5 with v2-prob vs v1, 40 games each vs
      bot-v2. Report the change in challenge blunders. Budget: $4.
- [ ] Frontier row: verify the top Claude model's ID, price and tool-choice support
      (free 400 checks only). Liar's Dice vs bot-v2 (40 games) and Code Break vs
      sonnet-5 and haiku-5.5. Test game first. Budget: $12.
- [ ] [human] Optional if the reserve allows: larger Werewolf comparison.

### Phase 11: ML on collected data (no API spend except the CFR follow-up)
New dependencies pre-approved: numpy, scikit-learn. No torch.
- [ ] Connect 4 perfect-play evaluator (bitboard negamax, alpha-beta, transposition
      table; verify on known positions). Grade every recorded move: optimal /
      inaccuracy / blunder. Report move accuracy per model.
- [ ] Liar's Dice CFR: game id liars_dice_small (2 dice each). CFR+ until
      exploitability is below a stated threshold (report the curve); bot
      `liars-dice-cfr`. Then haiku-5.5 and sonnet-5 vs it, 40 games each. Budget: $3.
- [ ] Wolf detector: messages and speaker roles from all Werewolf tournaments;
      TF-IDF + logistic regression with cross-validation grouped by match. Report AUC,
      and whether the classifier's top suspect beats the LLM villagers' votes, day by day.
- [ ] README: an "ML analysis" section.

### Phase 12: Bring your own agent (backend)
- [ ] Remote HTTP player type: roster url + secret; HMAC-SHA256 signed POST (timestamp
      + body, 5-min replay window); 10s timeout; retries with backoff on 5xx/timeout
      (infrastructure errors, never the player's fault); per-agent circuit breaker;
      idempotency key per (match, turn, attempt); invalid action = invalid attempt.
- [ ] Example agent (FastAPI, ~50 lines) in examples/, and a chaos test with slow,
      flaky and garbage-returning agents. Invariants must hold.
- [ ] README: "Enter your own agent", with the protocol spec, signature code and a
      curl example.
- [ ] [human] Decide whether to host an open arena (monthly cost).

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

- Split the Deal (`games/split_deal.py`): scores aren't zero-sum and a no-deal is 0 for both, so
  `scores[0]` doesn't say who won. Elo, Glicko-2 and final ratings use `seat_zero_outcome(placements)`.
  On the 446 heads-up matches stored when that changed, it matched `scores[0]` every time. Tables
  come from `tables()`, because 3,3,3 and 4,4,4 can't total 10, and both value sets always differ.
  An offer gives what the offerer keeps. `reject` is legal only with no offers left. A forfeit uses
  up an offer and clears the standing offer, or ends the game if none are left. Value claims are
  literal (`classify_value_claims`): paraphrases are missed. Two bots deal 98.8% of the time.
  The tool lists only the actions legal this turn, because listing `reject` always cost Haiku's
  test games 2 of 16 attempts (12.5%, over the gate) in the first run, $0.0026 and $0.0753.
- Split the Deal tournament 14 (2026-11-02, 60 games per pair, $1.2309 against a predicted $2.30,
  588 model attempts matching recordings, all 180 replayed to their scores, 0 illegal, 0 forfeits,
  every `stop_reason` `tool_use`, output max 240 of 256): bot 55-3-2 over Haiku, 93.3% (84.1-97.4%),
  bot 33-17-10 over Sonnet 5, 63.3% (50.7-74.4%, p = 0.03), Sonnet 5 37-15-8 over Haiku, 68.3%
  (55.8-78.7%, p = 0.003). Mean scores 0.89 to 0.50, 0.73 to 0.68, 0.76 to 0.64. 178 of 180 deals;
  efficiency 0.90-0.94. 5.2 turns a game, most 13. Accepted by Haiku 90, Sonnet 5 42, bot 46.
  Value claims in 454 messages: Haiku 84 true, 0 false, Sonnet 5 25 true, 1 false, and that one is
  a conditional ("worth 0 to me unless I get it"), so the classifier counted a hypothetical, as its
  note warns. Moving second won 94-66 (p = 0.03), seen afterwards. Messages truncated 17 and 23.
  I predicted bot 80% over Haiku, 50% over Sonnet 5, Sonnet 5 60% over Haiku, 85% deals, 30% false
  claims. Only the third held; the others fell outside their intervals. All the misses were models
  settling faster and lying less than I guessed. Haiku $0.0477, Sonnet 5 $1.1833. Finals: bot 1658.4
  (1606.2-1710.6), Sonnet 5 1514.4 (1468.1-1560.7), Haiku 1327.2 (1273.4-1381.0). Test games, 5 per
  model, twice: $0.0781 failing, then $0.0818 passing. "Models settle too early" is the likely story,
  not tested.
- Code Break (`games/code_break.py`): one seeded secret, and each seat sees only its own guesses
  and feedback, plus the other's count and whether they solved it. Once a seat solves on guess k
  the other plays only to guess k, the last chance to tie, so seat 0 moving first costs seat 1
  nothing, and the game ends early. A cut-off seat shows as unsolved in `code_break_metrics`. A
  forfeit skips a guess, which still counts, and removes nobody, so `final_placements` ignores it.
  `minimax_guess` lives with the engine because metrics need it. Its ties must go to the earliest
  in `ALL_CODES`: sorting them alphabetically needed 6 guesses on some secrets. It takes 4.476 on
  average, Knuth's figure, and a test walks all 1,296. It plays codes that can't be the secret on
  purpose, to split the candidates, so `inconsistent` isn't always a mistake: read it against the
  solver's own rate. No `legal_actions_hint`, so `RandomPlayer` can't play; `code-break-random` can.
- Actions in Code Break are `peg1`-`peg4`, each an enum. `code_of` also accepts the older `code`
  string, so tournament 12 replays, and the API doesn't enforce the enum: Sonnet 5 sent `peg3: "OB"`.
- Cancelling (`arena tournament cancel`, migration 0013) keeps the rows and the real spend, leaves
  queued matches unclaimed, and rebuilds that game's ratings. Anything that ranks or totals results
  uses `ratings.NOT_CANCELLED_SQL`: the leaderboard, `read.seat_results`, Code Break metrics.
  `read.totals` doesn't, because the money was really spent. It refuses while matches are running.
- Tournament 12 is a format-failure note, not a finding (2026-11-02, cancelled at 30 of 180, $0.4226):
  Sonnet 5 sent 5 to 8 letter codes, 34 of 163 attempts illegal, 11 forfeits, nearly all on its first
  guess. Haiku's test game was the only gate, so nothing tested Sonnet 5. A gate has to run for every
  model in a tournament: one game won't do either, as the fault hit about 1 game in 4, so run several.
- Code Break tournament 13 (2026-11-02, 60 games per pair, $1.6726 against a predicted $1.80, 1,646
  model attempts matching recordings, all 180 replayed to their scores): solver 59-0-1 over Haiku and
  59-0-1 over Sonnet 5, 99.2% (92.5-99.9%). Sonnet 5 10-4-46 over Haiku, 55.0% (42.5-66.9%), p = 0.18.
  Solved with all 10 guesses, Sonnet-Haiku games only: 11/60 (18.3%, 10.6-29.9%), 5/60 (8.3%,
  3.6-18.1%), p = 0.18. Inconsistent guesses overall Haiku 600/813, 73.8% (70.7-76.7%), Sonnet 5
  626/813, 77.0% (74.0-79.8%), solver 95/516, 18.4% on purpose. I predicted 65% and 60%, both outside
  their intervals, and every other prediction held. In Sonnet-Haiku games 445/555 and 451/555.
  Illegal attempts Sonnet 5 22/831 (2.6%), 4 forfeits, Haiku 2/815, 0. Sonnet 5 $1.59, Haiku $0.08.
  Baseline: `code-break-random` solves all 1,296 secrets, mean 4.65, worst 7, solver 4.48. Prompt v2
  crossed Sonnet 5's cache minimum, so it had 934,044 cache reads, unlike v1. Test games, 5 per model:
  $0.0022 and $0.0453. Finals: solver 2054.0 (1826.1-2282.0), Sonnet 5 1240.1 (1118.5-1361.7), Haiku
  1205.8 (1083.2-1328.5). The worked example shows no gain: Sonnet 5's inconsistent rate rose from 65%
  in tournament 12, on a different mix of games, so don't credit or blame the prompt from this.
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
- `Game.fill_action` runs before validation, and Werewolf uses it to add a missing `action`:
  every turn allows one kind, and in the Stage 2 test game Sonnet 5.5 left it out on 10 of 19
  discussion attempts (0 of 6 votes), which cost it a forfeit. The schema lists it as required,
  but the API doesn't enforce that. The completed action enters the log with `turns.auto_filled`,
  separate from `truncated`, and `raw_response` keeps what the model sent. `validate_action` stays
  strict, so replays still check whole actions, and a wrong `action` is still rejected.
  Auto-fills are legal moves: `auto_fills` in player metrics counts them, apart from illegal ones.
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
  wallpaper. Each game page swaps in its own set (`--c4-wash-*`, `--dice-`, `--ww-`, `--df-`) from
  its `body.g-<game>` rule, so a new game needs a set in all three palette blocks. Text is tested
  against every wash with the glows and wallpaper composited on top, because the plain wash
  passed while the real pixels were 4.0:1. `npx impeccable detect` lists low contrast here as
  advisory, against colours like #cab99d that are darker than the test's composited worst case.
- Seats: `--seat-0` to `--seat-6` in each palette, set by `data-seat` (`said` in `replay.js`,
  `seatAttr`). The number always goes with the colour, so a seat never depends on colour alone.
  They sit behind a number, so they're held to 4.5:1 on `surface` as well as the dataviz checks.
- Player pages: ids hold slashes, so URLs and file names use `web.player_slug`, reversible and
  lower case so a case-insensitive disk can't merge two players. Partial exports still write
  every player, since they hold only totals. `read.seat_results` feeds player pages and Compare,
  so they share one definition of a win. Seats that never acted exist: on the original rules a
  villager killed on night 1 takes no turn (34 seats in tournaments 5 and 6). Their latency and
  illegal rate are None, and a page must render that. Only the real export found this.
- Compare groups by the id's prefix (`model_of`) and skips providers `bot` and `random`. Its win
  rates aren't head to head, and overall win rate is left off on purpose: it would mix games.
- `templates/site.js` (Markup global, inlined on every page) sorts any `table.sortable` and
  filters any `table.filterable` by the `data-game` and `data-result` on its rows. A sort value
  is a cell's leading number, so keep annotations after the number. No value (blank or `–`)
  stays last both ways. A facet shows only with 2 to 8 values.
- Replay timeline: `timeline()` works out bands and ticks from spectator frames, so it's spoiler
  data and lives inside `.spoiler`. The strip is inset 8px, half the slider's thumb, so a mark
  sits under its frame; Chrome measured 0px off. A new game gets an empty strip until it adds a
  case there.
- Page wallpaper: `body::before` masks a game's motif tile (`--tile-<game>`, SVG data URIs in
  `site.css`) over its colour; home mixes all four. A new game needs a tile and a body rule.
- `read.match_detail` puts `claims` on each Werewolf turn for the reveal's truth overlay.
  That's spectator data: `blindSteps` must never read it, and a test feeds it false claims to
  prove it can't reach the blind view. A false claim is a bluff from a wolf or about the
  speaker's own seat; a villager's false accusation is shown as wrong, not as a lie.
- `web.baselines` folds players named `random` or `random<n>` under each leaderboard. They
  stay on the page and in the JSON, and everyone else is still ranked by the same numbers.
- The home page says "Leads: x" only through `web.clear_leader`: the top two by ordinal, with the
  first's final interval wholly above the second's, or, if either has no final, an ordinal gap
  over both sigmas. A rival with no final is never skipped: finals come from the latest tournament
  alone, so skipping Sonnet 5.5 would clear Sonnet 5 against the bot. The tables still rank by
  ordinal. As of tournament 9 every game says "No clear leader yet".
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
  Haiku 5.5 hit 64%. Haiku 4.5 cost 19x as much: its 10x list price, roughly doubled by the
  missing cache hits. All 56 rejected attempts
  were "column 3 is full", though the prompt lists legal columns. `max_tokens = 256` was
  enough: every recorded reply had a tool call.
- First real Liar's Dice run (tournament 4, 30 matches, $0.2833): the probability bot went
  16-4, and the Haikus finished level at 7-13 (5-5 head to head, 2-8 each against the bot).
  No illegal moves in 351 attempts. Haiku 5.5 hit 71% cache, Haiku 4.5 0%. Haiku 4.5 cost 22x:
  its 10x list price, roughly doubled by the missing cache hits.
  From the turn log, Haiku 5.5 / Haiku 4.5 / the bot: bids impossible given their own dice
  1 of 100 / 0 of 102 / 0 of 95; challenges of a bid their own dice already proved true
  15 of 68 (22%) / 12 of 81 (15%) / 0 of 63; challenges won 18% / 14% / 35%. Losing
  challenges, not wild bids, are where the models give up dice.
- `liars-dice-bot-v2` is `LiarsDiceBot` with `challenge_below = 0.1`. It's roster
  `model = "liars_dice_v2"`, so v1 keeps its identity and its 0.5. v1 already picked the most
  likely legal raise, since a face's minimal raise is its likeliest, so the threshold is the only
  difference. `arena.dice_sim`: against v1, 0.05 and 0.10 both win about 94% on tuning seeds, and
  0.20 to 0.30 (no game plays differently) 81%. The field test chose 0.10: it beat every other
  threshold (59% against 0.05) and averaged 76.5% over 7 thresholds and 4 bluffers. On 1,000 fresh
  seeds v2 beat v1 940 times (94.0%, 92.4-95.3%). Tournament 11 is its test against models.
- Liar's Dice round-robin (tournament 11, 2026-11-01, 30 games per pair, $4.4364 against a
  predicted $4.90, 1,339 replies matching recordings, 0 rejected, 0 forfeits, every `stop_reason`
  `tool_use`): v2 21-9 over Sonnet 5.5 (70.0%, 52.1-83.3%), v2 26-4 over Sonnet 5 (86.7%, 70.3-94.7%),
  Sonnet 5 22-8 over Sonnet 5.5 (73.3%, 55.6-85.8%, p = 0.02 against even). I predicted the Sonnets
  near 50% against v2, and both intervals exclude that. Against v1 they went 28-12 and 30-10, so
  the challenge threshold alone moved them p = 0.002 and under 0.0001: tournaments 8 and 9 mostly
  measured v1's over-challenging. Sonnet 5's bids were true 56% against v2 and 70% against Sonnet
  5.5; v2 won 77% of its challenges on it, v1 33%. Sonnet 5.5's bids 88% true. It challenged 359
  times, won 35%, and 7 (2%) were bids its own dice proved true, against 12% in tournament 8.
  Sonnet 5 challenged 45 times, 0 proved true. Bids impossible 1 of 580 for Sonnet 5, 2 of 258 for
  v2 against Sonnet 5, since it picks the likeliest legal raise even when none can be true.
  Thinking 203.3 tokens a turn (p95 498, output max 839 of 1,024). Cost by pair $1.26, $1.29, $1.89.
  Finals: v2 1650.5 (1578.9-1722.1), Sonnet 5 1464.2 (1400.7-1527.8), Sonnet 5.5 1385.2
  (1317.7-1452.7). Model and reasoning are still confounded, and v2 was tuned on free games.
- Sonnet Liar's Dice runs (2026-10-31, 40 games each against the bot, 0 rejected attempts, 0
  forfeits, every `stop_reason` `tool_use`, spend reconciled with recordings): tournament 8,
  `sonnet-5.5/ld-v1-think`, 28-12, 70% (interval 54.6-81.9%), $1.2354, 200.6 thinking tokens a
  turn (p95 468, max 832, output max 924 of 1,024); tournament 9, `sonnet-5/ld-v1`, 30-10, 75%
  (59.8-85.8%), $1.1814. Haikus in tournament 4: 4-16, 20% (8.1-41.6%), p = 0.0003 and 0.0001.
  Sonnet 5.5 against Sonnet 5 is inconclusive (p = 0.80) and confounded: model and reasoning
  changed together, on different seeds. Label it "Sonnet 5.5 with reasoning vs Sonnet 5 without
  (different models)". Beating the bot partly reflects its policy: it challenged on 40-51% of its
  turns and lost 145 of 166 and 176 of 262, so don't read these as general skill. Sonnet 5.5's
  bids were true 92% of the time, Sonnet 5's 75%; Sonnet 5 challenged on 11 of 355 turns.
  Challenges of a bid their own dice proved true: Sonnet 5.5 14 of 118 (12%), each with a legal
  raise available, Sonnet 5 0 of 11; Haikus 15-22%. So reasoning didn't remove the blunder, and
  the lower rate is unproven (p = 0.18 against both Haikus) as well as a different model.
  Challenges won 22% / 27%, the bot 13% / 33%. Bids impossible 0 of 248 / 2 of 344.
- `between_tools` game, 12 calls, recorded with reply blocks: every reply was text then tool
  call, 49 to 1,015 characters, and text length explained output above the no-text baseline (r =
  0.995), 117-487 tokens against about 95 with no text. So it can't give a no-reasoning Sonnet 5.5.
- The challenge analysis isn't in the repo. Rebuild it by replaying each match with `iter_states`:
  a challenge counts as proved true if the challenger's own dice meet the bid. Check any rebuild
  against tournament 4's figures above.
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
  reached (largest estimate 1,974 tokens). Haiku 4.5 cost 10x Haiku 5.5 ($1.34 vs $0.14), mainly
  its 10x list price. Missing cache hits add to it: Haiku 5.5 would have cost $0.16 uncached.
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
- Mixed-model day-first run (tournament 10, 2026-11-01, 40 tables, 8 players, $6.5573 against a
  predicted $7.90): the village won 9 of 40, 22.5% (Wilson 12.3-37.5%), and the predicted 42.5% is
  outside that, though p = 0.09 against tournament 7. Sonnet 5.5 / Sonnet 5 / Haiku 5.5: overall
  38.6% / 44.3% / 35.0%; wolf 16/20, 18/20, 28/40; village side 11/50, 13/50, 21/100; seer 3/10,
  3/10, 3/20; village votes on a wolf 41.9% / 44.1% / 40.3%; per seat $0.0477 / $0.0427 / $0.0017.
  No pair separates (best p = 0.11, Sonnet 5 against Haiku as wolves, unadjusted across 12 tests),
  so the Sonnets are 25-28x the price of Haiku with nothing proven. Sonnet 5 plain went 10 of 10 as
  a wolf (p = 0.11 against the rest), seen after the fact. Votes on a wolf matched tournament 7 on
  days 1 and 2 (75/200, 65/135), then fell on day 3 to 15/36 from 28/36 (p = 0.004), also after
  the fact. Sonnet 5.5 thought for 241.5 tokens a turn (p95 651). 6 of its 439 attempts hit
  `max_tokens = 1024`, in 5 turns, all recovered on retry; `{"action": "say"}` with no message is
  what a cut-off reply looks like. That's why it had 6 rejected attempts in all, and 0 forfeits.
  It auto-filled 152 of 266 discussion turns, 0 for everyone else. Sonnet 5 truncated 12 messages,
  Haiku 19, Sonnet 5.5 none. Claims true/false: 28/7, 23/4, 154/99, so Sonnets rarely use the
  literal phrases the classifier needs. Sonnet 5 had no cache reads again.
  Killed for low memory at 37 of 40 tables; 3 matches resumed 42 minutes later on run attempt 2.
  Recordings equal the log at 1,745 attempts. In-memory test games: $0.30473 and $0.13083.
  Every model went 70-90% as a wolf and 21-26% on the village side, which is suggestive: more
  capable players may help the deceiving side more than the detecting side. That was noticed after
  seeing the data, and wolves winning most tables gives every model that shape anyway. The
  comparisons that test it are unproven: Sonnet wolves 34/40 against Haiku 28/40 (p = 0.18), village
  side 24/100 against 21/100 (p = 0.74), wolves 31/40 tables here against 23/40 in tournament 7
  (p = 0.09).
- Known issue: future Sonnet 5.5 Werewolf runs should use `max_tokens` 2048 (new player ids).
  Tournament 10 hit 1024 on 6 of 439 attempts. They recovered on retry, so it was left alone.
- Village votes on a wolf, by day, tournament 6 / tournament 7: day 1 86/160 (54%) / 73/200
  (36.5%); day 2 34/63 (54%) / 66/133 (49.6%); day 3 5/6 (83%) / 28/36 (77.8%). Chance on day 1
  is 40% / 33%, so a day-first first vote is close to chance, with nobody dead and nothing
  inspected. The first vote removed a wolf 40% / 30% of the time.
- Haiku 4.5 aggressive replicated: flagged after the fact in tournament 6 (5 of 10 as a wolf,
  everyone else 8 or 9), then fresh games in tournament 7 gave 2 of 10 as a wolf (p = 0.015) and
  8 of 35 overall (p = 0.003), last on ordinal both times (-0.41, -1.06). Cautious led
  aggressive 59% to 34% in tournament 7 but didn't stand out in tournament 6 (39%). Haiku 5.5
  against Haiku 4.5 is unproven: 51% to 43% (p = 0.23), teammates sharing results.
- Haiku cost gap, Haiku 4.5 over Haiku 5.5, as billed / with Haiku 5.5 repriced as if nothing
  cached: tournament 3 19.0 / 9.9, 4 21.8 / 10.7, 6 9.7 / 8.2, 7 10.1 / 8.6. List price is the
  main cause everywhere, and missing cache hits add to it. Haiku 4.5 never caches here because
  its minimum is 4,096 tokens; Haiku 5.5's is 512.
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
  signal is a hard stop. On Windows an external SIGTERM is a hard kill (TerminateProcess), so
  only Ctrl-C and Ctrl-Break are graceful, and another process can send only Ctrl-Break, to a
  child started with CREATE_NEW_PROCESS_GROUP. `arena chaos` does that, and waits for "ready"
  because Ctrl-Break ends a worker that hasn't installed its handlers yet. `pytest -m slow` holds
  it: with stops alone, matches change worker mid-game with 0 extra claims. Breaking the release
  in a scratch patch failed that test with 3 extra claims.
- `/api/metrics/{game}` definitions live in `store/metrics.py`'s docstring. Cost per
  game counts completed matches only; turn-level stats include running matches.
  API endpoints have no HTTP tests: `TestClient` needs `httpx`, which isn't a dependency
  (the SDKs use `httpx2`). Test the store function instead.
- Requests/minute across workers: `arena worker` seeds `rate_limits` from roster
  `[providers]` at startup and takes tokens with one atomic UPDATE (`store/ratelimit.py`).
  Re-seeding keeps the current fill (clamped), so restarts don't grant a fresh burst.
  `max_concurrency` and retry-after pauses stay per process; `arena play` stays fully
  in-process. A vendor with no row raises rather than waiting forever.
  `test_postgres_bucket_never_grants_more_than_capacity_plus_refill` times grants on Postgres's
  `now()`, the bucket's own clock. On the local clock it failed once by 2 ms in a full run.
- `arena bench` makes `arena_bench` on DATABASE_URL's server, fresh for each worker count, and
  drops it after, so it never touches real data. It refuses a URL that already names that
  database. Same seed each run: 5,000 matches were 123,997 turns at every worker count. Each
  worker is a real `run_worker` process. Run it on the disk-backed `db` (:5434): `testdb` is tmpfs.
- Scaling at 5,000 matches (2026-11-01, 8 bots, 20 logical CPUs, Postgres in Docker on WSL2,
  disk-backed `db`), one run per row, 0 re-runs, 0 deadlocks. Workers / matches/s / turns/s /
  queue wait p50-p95-p99 s / claim p50-p95-p99 ms / peak connections:
  1 / 4.9 / 121 / 455-975-1,014 / 5.8-11.5-15.5 / 3;
  2 / 10.1 / 251 / 239-469-489 / 6.1-9.0-11.5 / 5;
  4 / 11.3 / 280 / 219-420-438 / 6.5-9.7-12.5 / 9;
  8 / 10.8 / 267 / 232-441-460 / 6.8-10.1-13.6 / 17.
  The speed-up stops at 2 workers (2.08x, then 2.31x and 2.20x). Queue wait is backlog drain,
  since everything is queued up front. Each worker holds about 2 connections. The machine was
  busy with under 1 GB of RAM free, and 500-match runs ranged 6.0-8.1 matches/s with 1 worker.
- The scaling limit is a shared row: `append_turn` adds to `tournaments.spent_usd` in the turn's
  transaction, so every turn holds that row lock through its commit's fsync. At 8 workers and 500
  matches, 737 of 1,455 backend samples were waiting on row locks. Disabling only that UPDATE in a
  scratch patch moved one 8-worker run from 370 to 476 turns/s, and the 1-to-8 speed-up to 3.21x,
  so it isn't the only limit. Bot turns are instant, so this measures the store's ceiling. An LLM
  turn takes seconds, and tournaments here ran at concurrency 4 or below, far under about 270
  turns/s. Exact mode is still this way; sharded mode is the answer, below.
- Spend modes (`store/spend.py`, migration 0012): `tournaments.spend_mode`, `exact` by default.
  Sharded writes `tournament_spend_shards`, one row per worker slot, and leaves
  `tournaments.spent_usd` at 0, so read spend only through `TOTAL_SPEND_SQL`, which is right in
  both modes. Slot rows outlive restarts, so a restart adds a row; the sum stays small.
  Overshoot in sharded mode is at most worker slots x max turn cost, because `BudgetGuard` checks
  before a turn's first attempt and hands the match back. Claim-time checks alone can't give that
  bound: a running match would finish. Exact mode still finishes it, so sharded has the tighter
  cap. A later attempt doesn't check, or the record of earlier attempts would be lost. A match
  stopped at the cap stays queued, costs no run attempt, and resumes if the budget is raised.
- Spend modes compared (2026-11-01, 5,000 matches, `db`, one run per cell, alternating): exact /
  sharded matches/s at 1 worker 5.0 / 4.7, 2 8.1 / 6.8, 4 9.9 / 9.3, 8 8.9 / 12.6. At 1,000
  matches, 4 workers 14.8 / 12.2, 8 13.2 / 15.9. Bench tournaments now carry a budget that's never
  reached, so sharded pays for its checks; that extra round trip is the likely cost at 1 to 4
  workers. It removed the lock waits: at 8 workers, of 1,000-match samples, `Lock:*` went from 855
  to 0, leaving mostly Postgres idle waiting on workers (1,285), then `IO:WALSync` and
  `LWLock:WALWrite` (198). Exact stays the default: nothing here runs above concurrency 4.
- Telemetry (`telemetry.py`): spans are match -> turn -> provider.call, and a `turn` span is one
  attempt, so a rejected move shows twice. Core modules stay free of it: the worker wraps players
  in `TracedPlayer`, and `RetryingProvider` opens the call span inside the rate limiter. Traces
  are off without `OTEL_EXPORTER_OTLP_ENDPOINT` (use `http://localhost:4318`), and the OTLP exporter
  is imported only then; a test holds that. Metrics live per worker process, so each needs its own
  `--metrics-port`; `ops/prometheus.yml` scrapes one, 9100. The API server has none, because it
  plays no matches, and `/api/metrics/{game}` is a different thing: per-player stats from the log.
  `arena_spend_usd` moves after commit, so a fenced write adds nothing. Failed setup counts as
  claimed and failed, so claimed can exceed completed with no match running.
- `arena chaos` runs in `arena_bench` too, and pauses only a compose `db` or `testdb` whose
  published port matches DATABASE_URL, so TrackWise's Postgres can't be hit. If the process dies
  mid-pause, run `docker unpause gauntlet-db-1`. Workers seat `PricedBot` ($0.0015 of made-up money
  a turn) because free bots would make the spend check 0 = 0. Leases are 4 s, so a pause outlives
  them and fencing gets exercised. `check_invariants` must catch damage: `tests/test_chaos.py`
  tampers with a clean run 12 ways. A second rating row can't be tested that way, because the
  UNIQUE constraint refuses it, and a test holds that. The short run is `pytest -m slow`.
- Chaos at full size (2026-11-01, seed 0, on `db`): 2,000 matches, 49,448 turns, 53 workers killed,
  15 pauses (60.9 s), 65 matches re-claimed, 22 turn writes fenced, 0 other worker deaths, spend
  exactly 49,448 x $0.0015 = $74.1720, all invariants held. That run had no graceful stops. The
  2,000-match mix with them was killed by Claude Code for low memory and has no result. The largest
  finished mix is the slow test: 500 matches on tmpfs `testdb`, 12 kills, 2 pauses (7.5 s), 7 stops,
  14 extra claims, all invariants held. It asserts each kind happened, because 120 matches at the
  first settings drained in seconds with 0 pauses and still passed. In sharded mode the same run
  had 19 kills, 7 pauses (23.3 s), 8 stops, 23 extra claims, 12,340 turns, $18.5100, all invariants
  held, and `check_invariants` also holds each shard to its slot's turn costs. Chaos workers start
  in their
  own process group, so a killed parent can leave them running: look for `arena.chaos` processes.
