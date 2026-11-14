# LLM Arena

[![CI](https://github.com/BandonC/Gauntlet/actions/workflows/ci.yml/badge.svg)](https://github.com/BandonC/Gauntlet/actions/workflows/ci.yml)

Tournaments between LLM players across turn-based games, with every move logged, every token
priced, and every game replayable. The total real spend behind everything below is about
**$79.50**: $73.62 across 1,705 recorded games and 34.7 million tokens, plus $5.88 on 114 in-memory
test games. The site counts the recorded games only.

**[Leaderboards and replays](https://bandonc.github.io/Gauntlet/)**, including one
[village win](https://bandonc.github.io/Gauntlet/match-142.html) and one
[bluff-heavy wolf win](https://bandonc.github.io/Gauntlet/match-116.html).

Or [play one blind](https://bandonc.github.io/Gauntlet/match-116.html#blind): sit in as a
villager, read the models arguing, name the wolves, and see how you did against them.

These are small samples of a few models, so the point here is what they show and how
far each result can be trusted. Intervals are 95% Wilson intervals, and p-values are Fisher
exact tests.

## In 30 seconds

LLM Arena runs tournaments between LLM players (provider, model, versioned prompt, settings) on
turn-based games. It records every move in an append-only log, so any match can be replayed or
resumed on another worker, prices every token, and ranks players with Weng-Lin, Elo and
Glicko-2. It's a backend project: the games are simple on purpose, and the work is in
resumable matches, lease fencing, a Postgres job queue, rate limiting and cost accounting.

Headline findings (small samples; details and caveats in [Findings](docs/findings.md)):

- **Liar's Dice:** a one-threshold bot beat Sonnet 5 26-4 (86.7%, 70.3-94.7%). Opus 5.5 beat it
  97 of 160 (60.6%, 52.9-67.9%) in a pre-registered extension.
- **Werewolf:** stronger models didn't help the village, which won 9 of 40 (22.5%, 12.3-37.5%).
  A word-counting classifier named a wolf more often than the village's vote did.
- **Code Break:** Sonnet 5 solved 11 of 60 (18.3%, 10.6-29.9%); a bot that never contradicts its
  clues solved all 1,296 secrets.
- **Split the Deal:** a bot that concedes in steps beat Haiku 5.5 55-3-2 (93.3%, 84.1-97.4%).
- **Reliability:** 2,000 bot matches survived 53 worker kills and 15 Postgres freezes with every
  invariant intact ([details](docs/scale-and-reliability.md)).

```
CLI / API --> Postgres <-- worker(s) --> MatchRunner --> Game engine (pure)
                 ^                            |
   turns (append-only log)                    +--> Player --> RetryingProvider --> vendor adapter
   matches (queue + leases)                          (shared RateLimiter per vendor)
```

More: [Findings](docs/findings.md) · [Scale & reliability](docs/scale-and-reliability.md) ·
[Enter your own agent](docs/enter-your-agent.md) · [Design notes and roadmap](CLAUDE.md)

## What the games showed

Each game tests something different, and the details follow below.

| Game | What it tests | Main finding (95% interval) |
|---|---|---|
| Connect 4 | Reading a simple board and following its rules | Too few games to rank models: the first player won 16 of 20 (80%, 58.4-91.9%). Graded by a perfect-play solver, both Haikus found the best move 43-44% of the time and blundered on 27-29% |
| Liar's Dice | Reasoning about odds with hidden information | A one-threshold bot beat Sonnet 5 26-4 (86.7%, 70.3-94.7%) and Sonnet 5.5 21-9 (70.0%, 52.1-83.3%). Fable 5.1 won 24 of 40 against it (60.0%, 44.6-73.7%) and Opus 5.5 25 of 40 (62.5%, 47.0-75.8%), neither establishing more than half the time, with no blunders in 194 and 184 challenges. A pre-registered 120 more games took Opus to 97 of 160 (60.6%, 52.9-67.9%): above half, established. In a reduced variant solved exactly, the equilibrium bot won 72 of 120 against three models (60.0%, 51.1-68.3%) |
| Werewolf | Deceiving others, and detecting deception | Stronger models didn't help the village, which won 9 of 40 (22.5%, 12.3-37.5%), and no model separated. A text classifier's top suspect was a wolf on 58.7% of first days, where the village's most-voted seat was one 36.4% of the time |
| Code Break | Deduction: holding every clue in mind | With 10 guesses, Sonnet 5 solved 11 of 60 (18.3%, 10.6-29.9%) and Haiku 5.5 5 of 60 (8.3%, 3.6-18.1%). A bot that only avoids contradicting itself solved all 1,296 secrets, and Fable 5.1 solved all 10 of its games |
| Split the Deal | Negotiating with private values | A bot that simply concedes in steps beat Haiku 5.5 55-3-2 (93.3%, 84.1-97.4%) and Sonnet 5 33-17-10 (63.3%, 50.7-74.4%) |
| Word Link | Shared meaning between two partners | Three teams came out level. Models guessed right 64.5% of the time (60.8-68.0%), and the bots 85.6% (80.5-89.6%), with a shared table |

The pattern across the newer games is that simple bots do well on anything calculable, and models
are at their weakest when a game needs them to keep track of constraints. These are small samples
of a few models on one prompt each, so that's a description of these runs, not a law.

Full write-ups: [Findings](docs/findings.md), including [Cost](docs/findings.md#cost), the
[ML analysis](docs/findings.md#ml-analysis) and [crash recovery](docs/findings.md#crash-recovery-in-practice).


## How it works

An event-sourced turn log is the only source of truth, so resuming a match means replaying it.
Workers claim matches from a Postgres queue (`FOR UPDATE SKIP LOCKED`, no Redis) under leases,
turn writes are fenced by those leases, provider calls share a rate limiter with retry-after
backoff, and tournaments have budget caps. Game engines are pure and deterministic. Live ratings
use Weng-Lin, with Bradley-Terry final ratings for heads-up tournaments.

All real runs used Anthropic models. The OpenAI adapter exists, but it's tested against canned
responses only and has never made a real call.

[CLAUDE.md](CLAUDE.md) has the full architecture, the invariants, the roadmap and every result
above in more detail.

## Quick start: a free tournament, no API key

You need git, Python 3.12+ and Docker. Bots play here, so nothing is billed.

```bash
git clone https://github.com/BandonC/Gauntlet.git && cd Gauntlet
python -m venv .venv && source .venv/bin/activate   # Git Bash on Windows: .venv/Scripts/activate
pip install -e .
cp .env.example .env && cp roster.example.toml roster.toml
docker compose up -d --wait db

arena migrate
arena tournament --game liars_dice --players liars-dice-bot,liars-dice-bot-v2 --games-per-pair 20
arena worker --exit-when-idle
arena serve
```

Open http://localhost:8000/matches and pick a match to step through its replay, or see the
leaderboard at http://localhost:8000.

Scale and failure tests are free too, and leave your tournaments alone:

```bash
arena bench --matches 500 --workers 1,2,4
arena chaos --matches 200 --workers 4
docker compose --profile observability up -d   # Jaeger on :16686, Prometheus on :9090
```

## With real models

Add `ANTHROPIC_API_KEY` to `.env`, and give every tournament a `--budget`:

```bash
arena tournament --game connect4 --players random,haiku-5.5/c4-v1 --games-per-pair 10 --budget 2
arena worker --concurrency 4
arena export --out docs --tournaments 6,7,8,9,10,11,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33,34 --matches 116,142   # the static site
```

## Enter your own agent

Any HTTP service can take a seat: the arena POSTs the seat's view to your URL and you answer with a
move. The protocol, signature code, a curl example and what counts against you are in
[docs/enter-your-agent.md](docs/enter-your-agent.md).
