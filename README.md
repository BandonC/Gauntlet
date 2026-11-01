# LLM Arena

Tournaments between LLM players across turn-based games, with every move logged, every token
priced, and every game replayable. The total real spend behind everything below is about
**$7.08**: $6.93 across 211 recorded games and 8.7 million tokens, plus $0.15 on four in-memory
test games. The site counts the recorded games only.

**[Leaderboards and replays](https://bandonc.github.io/Gauntlet/)**, including one
[village win](https://bandonc.github.io/Gauntlet/match-142.html) and one
[bluff-heavy wolf win](https://bandonc.github.io/Gauntlet/match-116.html).

Or [play one blind](https://bandonc.github.io/Gauntlet/match-116.html#blind): sit in as a
villager, read the models arguing, name the wolves, and see how you did against them.

These are small samples of a few models, so the point here is what they show and how
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

### Liar's Dice again: two Sonnets beat the bot, with a large caveat

Two stronger models then played 40 games each against the same bot:

| Against the bot | Wins | Interval | Thinking tokens per turn | Cost |
|---|---:|---:|---:|---:|
| Sonnet 5.5 with reasoning | 28 of 40, 70% | 54.6-81.9% | 200.6 | $1.2354 |
| Sonnet 5 without | 30 of 40, 75% | 59.8-85.8% | 0 | $1.1814 |
| Both Haikus, from above | 4 of 20, 20% | 8.1-41.6% | 0 | |

Each Sonnet did better than the Haikus (p = 0.0003 and 0.0001). **Sonnet 5.5 against Sonnet 5 is
inconclusive (p = 0.80) and confounded:** the model and the reasoning changed together, so this
can't say what reasoning adds. The two tournaments also used different dice seeds.

**"Beats the bot" partly reflects the bot's policy.** It challenged on 40-51% of its turns and
lost most of those challenges: 145 of 166 against Sonnet 5.5 and 176 of 262 against Sonnet 5. The
Sonnets got there in very different ways, too. Sonnet 5.5's bids were true 92% of the time and
Sonnet 5's 75% (p < 0.001), and Sonnet 5 challenged on only 11 of its 355 turns. A bot that
challenged less might well score differently, so these numbers rank players against this bot
only.

| | Sonnet 5.5 | Sonnet 5 | Bot vs Sonnet 5.5 | Bot vs Sonnet 5 |
|---|---:|---:|---:|---:|
| Bids impossible given own dice | 0 of 248 | 2 of 344 | 0 of 245 | 0 of 251 |
| Challenges of a bid their own dice already proved true | 14 of 118 (12%) | 0 of 11 | 0 of 166 | 0 of 262 |
| Challenges won | 22% | 27% | 13% | 33% |

Reasoning didn't remove the blunder. All 14 of Sonnet 5.5's challenges against a bid its own dice
proved true had a legal raise available, so each one gave up a die for certain; in match 148 it
held 4, 4, 6 and challenged "1×4". That 12% is below the Haikus' 15-22%, but the drop is unproven
(p = 0.18 against both Haikus together), and a different model made it.

Sonnet 5.5 has no true off switch for reasoning. `disabled` is rejected, and its lowest setting,
`between_tools`, reported zero thinking tokens in two test games while averaging 252 and 211 output
tokens per call, against 282 with default thinking and 76 for Sonnet 5. In the second game, which
recorded every reply, each one opened with reasoning in visible text, and that text's length
explained the extra output (r = 0.995). The setting moves the reasoning rather than removing it,
which is why the comparison above uses Sonnet 5.

### A stronger bot, tuned for free

The bot's weakness above suggested a simple change, so `liars-dice-bot-v2` keeps its rule and
challenges only when the current bid is under 10% likely, not 50%. v1 stays as it was, so
earlier results stay comparable.

**v2 beat v1 in 940 of 1,000 games (94.0%, interval 92.4-95.3%)**, on seeds tuning never used. A
threshold of 0.05 did as well against v1, but 0.10 beat it head to head and every other
threshold too, and it also beat four bluffing bots. That says it's strong against this family of
bots, not against a good human or a model, which real games will have to show.

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
| Liar's Dice, Sonnet 5.5 with reasoning | 40 | 502,812 | $1.2354 |
| Liar's Dice, Sonnet 5 | 40 | 483,262 | $1.1814 |
| **Total** | **211** | **8,686,291** | **$6.93** |

Four in-memory test games cost another $0.15. They never reach the turn log, so the project total
is about $7.08.

Haiku 4.5 cost about 10 times as much as Haiku 5.5, mainly because its list price is 10 times
higher. Missing cache hits add to that: Haiku 4.5 never cached at these prompt lengths and Haiku
5.5 did, which took the gap to 19 and 22 times in Connect 4 and Liar's Dice, where most of each
prompt was cached. In Werewolf it stayed near 10 times, with caching adding under a fifth. Skill
didn't follow price: the two were never clearly separated overall.

The two Sonnets cost about the same for different reasons. Thinking was 59% of Sonnet 5.5's bill,
offset by cached prompts, while Sonnet 5 had no cache reads at all.

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

## With real models

Add `ANTHROPIC_API_KEY` to `.env`, and give every tournament a `--budget`:

```bash
arena tournament --game connect4 --players random,haiku-5.5/c4-v1 --games-per-pair 10 --budget 2
arena worker --concurrency 4
arena export --out docs --tournaments 6,7,8,9 --matches 116,142   # the static site
```
