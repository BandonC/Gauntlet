# Design decisions

Six architecture decision records. Each gives the context, the decision, the alternatives
considered, the consequences, and evidence from this repository. Numbers come from the
runs documented in [CLAUDE.md](../CLAUDE.md), [Scale & reliability](scale-and-reliability.md)
and [Findings](findings.md); caveats stay with them.

## ADR 1: Event-sourced turn log and replay

**Context.** Matches last minutes and cost real money per turn. Workers get killed, sleep,
or lose their lease, and another worker must pick the match up without losing or re-paying
for finished turns.

**Decision.** The append-only `turns` table is the only source of truth. Match state is never
stored: resuming is `iter_states(game, spec, turns)`, which re-applies every recorded action
through the pure game engine and re-validates it (`CorruptTurnLog` on mismatch). Engines are
pure and deterministic; all randomness derives from the match seed. A turn is persisted
before in-memory state advances. Players are stateless between turns, so a new worker needs
nothing but the log.

**Alternatives.** Snapshot the state per turn (smaller replays, but two sources of truth that
can disagree, and engine changes would invalidate snapshots); keep state in worker memory
(a crash loses the match).

**Consequences.** Replay pages, the live view and resume all use one code path. Replay costs
time proportional to match length, which is small here. A turn played but not persisted before
a crash is asked again and paid again; this is accepted. Engines must make `current_player` a
pure function of state, and any read of a stored match must pass its `options`.

**Evidence.** Tournament 7: a worker was killed mid-game and a new process resumed two matches
at turns 34 and 12. All 40 matches ended with continuous turn numbers and no duplicates. The
re-asked turns cost $0.0006 of $2.49 (0.02%). Tournaments 13, 14, 15 and 30-32
(570 matches) were each replayed to their stored scores, and all matched.

## ADR 2: Lease fencing

**Context.** Leases expire when a worker stalls (a GC pause, a sleeping laptop, a frozen
database). The old worker may wake up and write after another worker has taken the match.

**Decision.** A claim sets a lease with an expiry. `PostgresTurnStore.append_turn` inserts a
turn only if this worker holds an unexpired lease (`FOR SHARE` on the match row), and the
`(match_id, turn_no)` primary key rejects duplicates. Both failures raise `LeaseLost`; the
worker stops at once and writes nothing else. Completion locks rows in a fixed order (match,
ratings by `player_id`, tournament last) to avoid deadlocks.

**Alternatives.** Trust heartbeats alone (a stalled writer still writes); a fencing token
compared in application code (a race between check and write); an external lock service
(another system to run).

**Consequences.** A stale writer can waste a model call but cannot corrupt the log. Lease
length trades recovery speed against false expiry. Rare duplicate spend from fenced turns is
not counted in `turns`.

**Evidence.** `test_expired_lease_is_reclaimed_and_old_writer_is_fenced` exercises the race.
In tournament 7, 2 stale writes were fenced after a 32-minute stall. Chaos at full size
(2,000 matches, 53 workers killed, 15 Postgres pauses, 22 turn writes fenced) held every
invariant: each match completed once, logs continuous, nothing rated twice, spend equal to
the sum of turn costs.

## ADR 3: Postgres job queue, not Redis or Kafka

**Context.** The queue needs claim, heartbeat, retry with backoff and completion, and
completion must be atomic with the turn log, ratings and tournament advance.

**Decision.** The queue is the `matches` table, claimed with row locks and leases, in the same
database as everything else. Completing a match writes the result, ratings, `rating_history`
and tournament progress in one transaction.

**Alternatives.** Redis (fast, but the queue and the results live in different stores, so
"completed exactly once" needs cross-system care); Kafka (built for streams, heavy for a few
thousand long-running jobs).

**Consequences.** One system to operate and one transaction boundary. Throughput is bounded by
Postgres, and the measured ceiling is a lock (see ADR 4). LLM turns take seconds, so the ceiling
is far above real load.

**Evidence.** At 5,000 bot matches, throughput rose from 4.9 matches/s with 1 worker to about
10-11 with 2 or more, with 0 deadlocks (`arena bench`; one run per row, so treat the shape as
indicative). Tournaments here ran at concurrency 4 or below, far under the roughly 270
turns/s ceiling.

## ADR 4: Exact versus sharded spend counters

**Context.** A budget cap needs a running total. `append_turn` added each turn's cost to
`tournaments.spent_usd` in the turn's transaction, so every turn held that row lock through its
commit.

**Decision.** Keep both. `exact` (the default) is the single row. `sharded` writes one row per
worker slot (`tournament_spend_shards`), summed for the check; read spend only through
`TOTAL_SPEND_SQL`. In sharded mode `BudgetGuard` checks before each turn's first attempt, so
overshoot is at most worker slots times the maximum turn cost. Exact mode lets a running match
finish, so sharded has the tighter cap.

**Alternatives.** Batch updates in memory (loses spend on a crash); drop the check (unsafe for
paid runs).

**Consequences.** Sharded removes the lock but pays an extra round trip per turn. Exact stays
the default because nothing here runs above concurrency 4.

**Evidence.** At 8 workers and 500 matches, 737 of 1,455 backend samples waited on row locks.
Over 1,000 matches, `Lock:*` samples fell from 855 to 0 in sharded mode. At 5,000 matches,
exact versus sharded was 8.9 versus 12.6 matches/s at 8 workers, but 5.0 versus 4.7 at 1 worker
(one run per cell). A chaos run in sharded mode held all invariants, including each shard's
cost.

## ADR 5: Gates and budgets for paid runs

**Context.** Real model calls cost money from a shared account, and a format fault can burn a
budget without producing data.

**Decision.** Every paid run passes `--budget`, starts with the cheapest model, and goes through
a gate first: test games for every model in the tournament, several rather than one. Prices come
only from `roster.toml`, and a paid player with no prices is refused at load. Tests never call
real APIs. The project has a total cap and a stop line, and predictions are written before runs
where possible. A non-retryable provider error (such as insufficient credit) fails the match at
once and pauses the tournament.

**Alternatives.** Run the full tournament directly (cheaper in effort, exposed to format
faults); rely on the budget alone (it limits loss but doesn't prevent a useless run).

**Consequences.** Test games cost money too, and a gate can still miss a rare fault. Budgets
bound spend only to within the documented overshoot.

**Evidence.** Tournament 12 was cancelled at 30 of 180 matches ($0.4226): Sonnet 5 sent
malformed codes on 34 of 163 attempts, and the only gate was a Haiku game. That is why every
model now gets its own gate. The first CFR test game ran a paid player at $0 because prices
were missing, which led to the load-time refusal. Of the nine runs with a recorded cost
prediction (tournaments 10, 11, 13, 14, 15, 16-18, 19-22, 30-32, 33), eight finished under it.
The effort sweep (16-18) cost $3.11 against $2.50, and tournament 26 hit its cap and needed it
raised.

## ADR 6: Bradley-Terry final ratings versus live ratings

**Context.** Live ratings update after each match and so depend on play order. Weng-Lin ranks
every table size, while Elo and Glicko-2 apply to heads-up games only.

**Decision.** Keep live ratings (Weng-Lin on every match, Elo and Glicko-2 heads-up) written in
the completion transaction, and add final ratings: a maximum-likelihood Bradley-Terry fit to one
finished heads-up tournament, computed on read with no table. Where the win graph isn't strongly
connected there is no final rating rather than one invented by a prior. The leaderboard still
ranks by live ordinal, because not every player has a final rating.

**Alternatives.** Live ratings only (order-dependent); final ratings only (nothing while a
tournament runs, no table-game support).

**Consequences.** Two numbers per player that can disagree. Finals average 1500 within one
tournament, so they never compare across tournaments. Round-robin matches are now shuffled
with a seed so block order can't bias live ratings.

**Evidence.** With pairings played as blocks, Haiku 4.5 sat 148 Glicko points above Haiku 5.5
on identical records. Over 200 random orders of the same 30 results the gap averaged -0.2 (sd
64), and the real order beat 99% of them.
