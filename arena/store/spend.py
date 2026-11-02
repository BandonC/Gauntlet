"""Tournament spend, in one of two modes, chosen when a tournament is created.

exact (default): every turn adds to `tournaments.spent_usd`. One row, so the total is always
a plain read, but every worker queues on that row through each turn's commit. The budget is
checked only at claim time, so a running match finishes even past the cap, which allows an
overshoot of up to the rest of a match per worker slot.

sharded: every turn adds to that worker slot's own row in `tournament_spend_shards`, so
slots don't queue on each other, and the total is a sum. Each write still commits with its
turn, so the sum is as current as the single row would be. To keep the cap tight, a match
also checks the budget before each turn, and hands itself back if the cap has been reached
(`BudgetGuard`). Overshoot bound: when a slot passes the check the total was under budget,
and a slot is in at most one turn at a time, so

    overshoot <= worker slots x max turn cost

where a slot is one unit of `--concurrency` and a turn's cost includes all its attempts. A
fenced write spends money that no counter sees, in either mode.
"""

from __future__ import annotations

import asyncpg

from arena.players.base import Decision, Player, TurnContext

EXACT = "exact"
SHARDED = "sharded"
SPEND_MODES = (EXACT, SHARDED)

# Use wherever a tournament's spend is read, with the tournament aliased `t`. A tournament
# only ever writes to one side, so the sum is right in both modes.
TOTAL_SPEND_SQL = """(t.spent_usd + COALESCE(
    (SELECT sum(s.spent_usd) FROM tournament_spend_shards s WHERE s.tournament_id = t.id), 0))"""

ADD_EXACT_SQL = """
UPDATE tournaments SET spent_usd = spent_usd + $2
WHERE id = (SELECT tournament_id FROM matches WHERE id = $1)
"""

ADD_SHARD_SQL = """
INSERT INTO tournament_spend_shards (tournament_id, worker_id, spent_usd)
SELECT tournament_id, $3, $2::numeric FROM matches WHERE id = $1 AND tournament_id IS NOT NULL
ON CONFLICT (tournament_id, worker_id)
DO UPDATE SET spent_usd = tournament_spend_shards.spent_usd + EXCLUDED.spent_usd
"""

OVER_BUDGET_SQL = f"""
SELECT t.budget_usd IS NOT NULL AND {TOTAL_SPEND_SQL} >= t.budget_usd
FROM tournaments t WHERE t.id = $1
"""


class BudgetReached(Exception):
    """A sharded tournament hit its cap between turns. Every played turn is persisted, so
    the match can resume if the budget is raised."""


async def over_budget(pool: asyncpg.Pool, tournament_id: int) -> bool:
    return bool(await pool.fetchval(OVER_BUDGET_SQL, tournament_id))


class BudgetGuard:
    """Refuses to start a turn once the cap is reached. Only the first attempt checks: a
    later attempt is part of a turn already under way, and stopping there would lose the
    record of what the earlier attempts cost."""

    def __init__(self, inner: Player, pool: asyncpg.Pool, tournament_id: int):
        self.player_id = inner.player_id
        self._inner = inner
        self._pool = pool
        self._tournament_id = tournament_id

    async def act(self, ctx: TurnContext) -> Decision:
        if ctx.attempt == 1 and await over_budget(self._pool, self._tournament_id):
            raise BudgetReached(f"tournament {self._tournament_id}")
        return await self._inner.act(ctx)
