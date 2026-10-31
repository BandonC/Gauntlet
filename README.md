# LLM Arena

Tournaments between LLM players across turn-based games, with every move logged, every token
priced, and every game replayable. The total real spend behind everything below is **$4.51**,
across 131 games and 7.7 million tokens.

**[Leaderboards and replays](https://bandonc.github.io/Gauntlet/)**, including one
[village win](https://bandonc.github.io/Gauntlet/match-142.html) and one
[bluff-heavy wolf win](https://bandonc.github.io/Gauntlet/match-116.html).

These are small samples of two cheap models, so the point here is what they show and how
far each result can be trusted. Intervals are 95% Wilson intervals, and p-values are Fisher
exact tests.

## What the games showed

### A tuned Werewolf game, and a simulator that was right for partly the wrong reason

Eight players (Haiku 5.5 and Haiku 4.5, each with four play styles) played 40 games of 7-seat
Werewolf: 2 wolves, a seer and 4 villagers. **Wolves won 33 of 40, 82.5%.**

The rules explain most of that. The game opens with a night kill, so if the first day's vote
removes a non-wolf, the next kill reaches parity and the wolves win. To test fixes without
paying for games, I built a simulator and checked it against the real engine. It needed
one correction first: independent voters gave the village 5%, because their votes scatter into
ties. Real tables pile on, with one seat taking 5 of 6 votes on 23 of 40 first days. Fitting
how much a table votes together to first-vote outcomes alone gave 22%, in line with the real
7 of 40. Each variant then got 2,000 simulated games at 54% voter accuracy:

| Variant | Village wins | Turns per game |
|---|---:|---:|
| Current rules | 22.4% | 30.7 |
| Current rules + doctor | 27.8% | 36.7 |
| **Day-first (no night 1 kill)** | **41.4%** | **43.3** |
| Day-first + doctor | 44.5% | 46.5 |
| 9 seats, 2 wolves | 51.2% | 56.0 |

Day-first went live as a separate game, so its ratings never mix with the original. Over 40
real games the **village won 17, 42.5%** (interval 28.5-57.8%), against the predicted 41.4%. It's
a real improvement on the original rules (p = 0.027), and game length matched exactly at 43.3
turns.

The match flatters the simulator, though. Share of village votes that landed on a wolf:

| Day | Original rules | Day-first | Simulator assumed |
|---|---:|---:|---:|
| Day 1 | 54% (86/160), chance 40% | **36.5%** (73/200), chance 33% | 54% |
| Day 2 | 54% (34/63) | 49.6% (66/133) | 54% |
| Day 3 | 83% (5/6) | 77.8% (28/36) | 54% |

With nobody dead and nothing inspected, a day-first first vote is close to a guess, and the
first vote removed a wolf 30% of the time rather than the predicted 46%. Later days made up the
difference. The fixed-accuracy model got the total and missed the path, and with 40 games the
interval is wide enough to fit many other values, so treat 41% against 42.5% as encouraging, not
as confirmation. Replacing fixed accuracy with per-day accuracy is the next item on the roadmap.

Three things about the players, in falling order of confidence:

- **Haiku 4.5 with the aggressive style is weak.** It looked bad in the first tournament (5 of
  10 wins as a wolf, everyone else 8 or 9), but I only noticed that after seeing the data. Fresh
  games repeated it: 2 of 10 as a wolf (p = 0.015) and 8 of 35 overall (p = 0.003). Haiku 5.5 with
  the same style was unremarkable, so this is a model and style pairing, not the style alone.
- **Cautious beat aggressive, 59% to 34%, in the day-first games** (p = 0.006, optimistic because
  teammates share results). Cautious didn't stand out in the first tournament, at 39%.
- **Haiku 5.5 against Haiku 4.5 is unproven:** 51% to 43% on day-first (p = 0.23). Its widest gap
  is at seer, 60% to 25%, in only 20 games each (p = 0.054).

Bluffing was nearly all one move. Wolves posed as villagers 65 times in day-first and no wolf
made a matched seer claim. No wolf deliberately leaked its role in either tournament. Claims
are literal text matches, so paraphrases are missed.

### Liar's Dice: a short probability bot beat both Haikus 16-4

Over 30 matches, the bot went **16-4** (interval 58-92%). The Haikus finished level at 7-13, 5-5
head to head and 2-8 each against the bot. There were no illegal moves in 351 attempts, so this
isn't a rules problem. The turn log suggests where the dice went:

| | Haiku 5.5 | Haiku 4.5 | Bot |
|---|---:|---:|---:|
| Bids impossible given own dice | 1 of 100 | 0 of 102 | 0 of 95 |
| Challenges of a bid their own dice already proved true | 15 of 68 (22%) | 12 of 81 (15%) | 0 of 63 |
| Challenges won | 18% | 14% | 35% |

The bids were fine. The models lost dice by challenging, sometimes against a bid their own hand
showed was true. That's a description of how the games went, and 20 games per player doesn't
separate the two Haikus from each other.

### Connect 4: the illegal-move gap

In 20 games, **Haiku 5.5 made 41 illegal attempts in 220 (18.6%) and Haiku 4.5 made 15 in 196
(7.7%)** (p = 0.001, optimistic because retries repeat the same mistake). Haiku 5.5 forfeited twice
and Haiku 4.5 never did. All 56 rejections were the same one, "column 3 is full", even though
the prompt lists the legal columns.

The results themselves say little. Haiku 4.5 won 12-8 (p = 0.34), and the first player won 16 of
20, so seat order drowns out the models. Separating them would take hundreds of games.

### Cost

| Tournament | Games | Tokens | Cost |
|---|---:|---:|---:|
| Connect 4 | 20 | 381,429 | $0.2277 |
| Liar's Dice | 30 | 446,641 | $0.2833 |
| Werewolf, first test game | 1 | 55,417 | $0.0351 |
| Werewolf | 40 | 2,547,754 | $1.4761 |
| Werewolf, day-first | 40 | 4,268,976 | $2.4901 |
| **Total** | **131** | **7,700,217** | **$4.51** |

At listed prices Haiku 5.5 costs a tenth of Haiku 4.5. In every game here the real gap was 10 to
22 times, because Haiku 4.5 never cached at these prompt lengths and Haiku 5.5 did. Skill didn't
follow price: the two were never clearly separated overall.

## Crash recovery in practice

The design says a match can die at any point and resume elsewhere. The day-first tournament
tested that by accident, twice:

- **23:17, a 32-minute stop.** Everything on the machine paused, most likely sleep. Leases
  expired, and on waking 2 stale writes were fenced off, so those turns were asked again.
- **23:50, the worker was killed for low memory** with 2 matches mid-game. A new process
  resumed them 9 minutes later at turns 34 and 12.

Afterwards, every one of the 40 matches had continuous turn numbers with no duplicates, the
affected matches replayed exactly, and none failed. The recordings hold 1,737 replies against
1,735 persisted attempts. Matched line by line, the 2 extra are exactly the fenced turns: **$0.0006
of $2.49 paid twice**. That money isn't in the turn log, so reported spend is short by it. The
kill lost no recorded reply, though a request in flight at that moment can't be seen in
recordings.

## How it works

An event-sourced turn log is the only source of truth, so resuming a match means replaying it.
Workers claim matches from a Postgres queue (`FOR UPDATE SKIP LOCKED`, no Redis) under leases,
turn writes are fenced by those leases, provider calls share a rate limiter with retry-after
backoff, and tournaments have budget caps. Game engines are pure and deterministic. Live ratings
use Weng-Lin, with Bradley-Terry final ratings for heads-up tournaments.

[CLAUDE.md](CLAUDE.md) has the full architecture, the invariants, the roadmap and every result
above in more detail.

## Quick start

```bash
pip install -e ".[dev]"
docker compose up -d db
cp .env.example .env && cp roster.example.toml roster.toml

arena play connect4 --p1 random:1 --p2 random:2      # free, no setup needed
arena migrate
arena tournament --game connect4 --players random,haiku-5.5/c4-v1 --games-per-pair 10 --budget 2
arena worker --concurrency 4
arena serve                                          # http://localhost:8000
arena export --out docs --tournaments 6,7 --matches 116,142   # the static site
```
