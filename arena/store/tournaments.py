"""Creating tournaments, advancing them, and reading results from Postgres.

Lifecycle (both formats):  running --(last match done)--> completed
                              |  ^
          (a match fails) --> paused --(arena tournament resume)
Swiss: running also queues round n+1 when round n's last match completes.
"""

from __future__ import annotations

import logging
import random

import asyncpg

from arena.games.registry import get_game
from arena.ratings.weng_lin import WengLin
from arena.roster import PlayerConfig, config_hash
from arena.store import ratings
from arena.store.spend import EXACT
from arena.tournaments import (
    BYE_POINTS,
    SwissEntrant,
    assign_roles,
    default_swiss_rounds,
    pair_counts,
    role_counts,
    round_robin,
    shuffle_matches,
    swiss_pairings,
    swiss_seatings,
    table_schedule,
)

log = logging.getLogger(__name__)

UPSERT_PLAYER_SQL = """
INSERT INTO players (id, provider, model, prompt_key, config, config_hash)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (id) DO NOTHING
RETURNING config_hash
"""

INSERT_MATCH_SQL = """
INSERT INTO matches (tournament_id, game, seed, round, options)
VALUES ($1, $2, $3, $4, $5) RETURNING id
"""

# Two workers finishing a tournament's last two matches (of a round) at once would each
# see the other's match as still running and neither would advance it. Locking the
# tournament row serialises them; the pending check is a separate statement so it sees
# the other's commit. Taken last in the completion transaction (after match and ratings
# rows), the same order as turns.append_turn, so it can't deadlock with it.
# NO KEY UPDATE, not UPDATE: queue.complete's completed_at re-stamp updates the match row
# a second time, which re-runs its tournament_id FK check and takes KEY SHARE here; two
# completions each holding KEY SHARE and both upgrading to FOR UPDATE would deadlock.
# NO KEY UPDATE doesn't conflict with KEY SHARE but still conflicts with itself.
LOCK_TOURNAMENT_SQL = """
SELECT game, format, status, rounds, current_round FROM tournaments WHERE id = $1
FOR NO KEY UPDATE
"""

PENDING_SQL = """
SELECT EXISTS (
    SELECT 1 FROM matches
    WHERE tournament_id = $1 AND status IN ('queued', 'running', 'failed')
)
"""

# The second guard against double round creation: only one transaction can move
# current_round from n to n+1, even if the row lock were ever dropped.
NEXT_ROUND_SQL = """
UPDATE tournaments SET current_round = $2 + 1
WHERE id = $1 AND current_round = $2 AND status = 'running'
RETURNING current_round
"""

COMPLETE_TOURNAMENT_SQL = (
    "UPDATE tournaments SET status = 'completed' WHERE id = $1 AND status = 'running'"
)

PAUSE_SQL = """
UPDATE tournaments SET status = 'paused', last_error = $2
WHERE id = $1 AND status = 'running'
"""

POINTS_SQL = """
SELECT s.player_id, sum((m.scores ->> s.seat::int)::float) AS points
FROM matches m JOIN match_seats s ON s.match_id = m.id
WHERE m.tournament_id = $1 AND m.status = 'completed'
GROUP BY s.player_id
"""

MEETINGS_SQL = """
SELECT array_agg(s.player_id) AS seats
FROM matches m JOIN match_seats s ON s.match_id = m.id
WHERE m.tournament_id = $1
GROUP BY m.id
"""


async def upsert_players(conn: asyncpg.Connection, players: list[PlayerConfig]) -> None:
    for p in players:
        h = config_hash(p)
        inserted = await conn.fetchval(
            UPSERT_PLAYER_SQL,
            p.id,
            p.provider,
            p.model,
            p.prompt,
            {
                "temperature": p.temperature,
                "max_tokens": p.max_tokens,
                "seed": p.seed,
                "tool_choice": p.tool_choice,
                "thinking_budget": p.thinking_budget,
                "thinking_mode": p.thinking_mode,
                "effort": p.effort,
            },
            h,
        )
        if inserted is None:
            existing = await conn.fetchval("SELECT config_hash FROM players WHERE id = $1", p.id)
            if existing != h:
                raise ValueError(
                    f"player {p.id!r} already exists with a different config "
                    "(model/prompt/settings changed). Give the new config a new id."
                )


async def _insert_tournament(
    conn: asyncpg.Connection,
    game: str,
    fmt: str,
    players: list[PlayerConfig],
    budget_usd: float | None,
    rounds: int | None = None,
    spend_mode: str = EXACT,
) -> int:
    await upsert_players(conn, players)
    tid = await conn.fetchval(
        "INSERT INTO tournaments (game, format, budget_usd, rounds, spend_mode) "
        "VALUES ($1, $2, $3, $4, $5) RETURNING id",
        game,
        fmt,
        budget_usd,
        rounds,
        spend_mode,
    )
    await conn.executemany(
        "INSERT INTO tournament_players (tournament_id, player_id) VALUES ($1, $2)",
        [(tid, p.id) for p in players],
    )
    return tid


async def _insert_matches(
    conn: asyncpg.Connection,
    tid: int,
    game: str,
    seatings: list[list[str]],
    rng: random.Random,
    round_no: int | None = None,
    options: list[dict] | None = None,  # per seating
) -> None:
    for i, seats in enumerate(seatings):
        match_options = options[i] if options else None
        mid = await conn.fetchval(
            INSERT_MATCH_SQL, tid, game, rng.randrange(2**31), round_no, match_options
        )
        await conn.executemany(
            "INSERT INTO match_seats (match_id, seat, player_id) VALUES ($1, $2, $3)",
            [(mid, i, pid) for i, pid in enumerate(seats)],
        )


async def create_round_robin(
    pool: asyncpg.Pool,
    game: str,
    players: list[PlayerConfig],
    games_per_pair: int,
    budget_usd: float | None,
    seed: int | None = None,
    spend_mode: str = EXACT,
) -> tuple[int, int]:
    """Returns (tournament_id, number_of_matches)."""
    seed = random.randrange(2**31) if seed is None else seed
    seatings = shuffle_matches(round_robin([p.id for p in players], games_per_pair), seed)
    async with pool.acquire() as conn, conn.transaction():
        tid = await _insert_tournament(
            conn, game, "round_robin", players, budget_usd, spend_mode=spend_mode
        )
        await _insert_matches(conn, tid, game, seatings, random.Random(seed))
    return tid, len(seatings)


async def create_tables(
    pool: asyncpg.Pool,
    game: str,
    players: list[PlayerConfig],
    table_size: int,
    tables: int,
    budget_usd: float | None,
    seed: int | None = None,
    spend_mode: str = EXACT,
) -> tuple[int, int]:
    """Queue every table at once (see tournaments.table_schedule). Returns
    (tournament_id, number_of_matches)."""
    g = get_game(game)
    if not g.min_players <= table_size <= g.max_players:
        raise ValueError(f"{game} takes {g.min_players}-{g.max_players} players per table")
    seed = random.randrange(2**31) if seed is None else seed
    ids = [p.id for p in players]
    seatings = table_schedule(ids, table_size, tables, seed)
    meetings = pair_counts(ids, seatings).values()
    if max(meetings) - min(meetings) > 1:
        log.warning(
            "no evenly balanced schedule found for %d players, %d tables of %d: pairs meet "
            "%d-%d times",
            len(ids),
            tables,
            table_size,
            min(meetings),
            max(meetings),
        )
    options = None
    role_pool = g.role_pool(table_size)
    if role_pool:
        roles = assign_roles(ids, seatings, role_pool, seed)
        options = [{"roles": table} for table in roles]
        for role, by_player in role_counts(ids, seatings, roles).items():
            if max(by_player.values()) - min(by_player.values()) > 1:
                log.warning("no even deal found for role %s: %s", role, by_player)
    async with pool.acquire() as conn, conn.transaction():
        tid = await _insert_tournament(
            conn, game, "tables", players, budget_usd, spend_mode=spend_mode
        )
        await _insert_matches(conn, tid, game, seatings, random.Random(seed), options=options)
    return tid, len(seatings)


async def create_swiss(
    pool: asyncpg.Pool,
    game: str,
    players: list[PlayerConfig],
    budget_usd: float | None,
    rounds: int | None = None,
    seed: int | None = None,
    spend_mode: str = EXACT,
) -> tuple[int, int]:
    """Queue round 1 (seeded by Weng-Lin ordinal); later rounds are queued as
    rounds finish. Returns (tournament_id, matches in round 1)."""
    if len(players) < 2:
        raise ValueError("a Swiss tournament needs at least 2 players")
    rounds = rounds or default_swiss_rounds(len(players))
    async with pool.acquire() as conn, conn.transaction():
        tid = await _insert_tournament(conn, game, "swiss", players, budget_usd, rounds, spend_mode)
        await conn.execute("UPDATE tournaments SET current_round = 1 WHERE id = $1", tid)
        n = await _queue_swiss_round(conn, tid, game, 1, random.Random(seed))
    return tid, n


async def swiss_entrants(conn: asyncpg.Connection, tid: int, game: str) -> list[SwissEntrant]:
    """Current Swiss standings inputs: points (byes count BYE_POINTS), Weng-Lin ordinal,
    and whether each player has had a bye."""
    ids = [
        r["player_id"]
        for r in await conn.fetch(
            "SELECT player_id FROM tournament_players WHERE tournament_id = $1", tid
        )
    ]
    points = {r["player_id"]: r["points"] for r in await conn.fetch(POINTS_SQL, tid)}
    byes = [
        r["player_id"]
        for r in await conn.fetch(
            "SELECT player_id FROM tournament_byes WHERE tournament_id = $1", tid
        )
    ]
    ordinal = {
        r["player_id"]: r["ordinal"]
        for r in await conn.fetch(
            "SELECT player_id, wl_mu - 3 * wl_sigma AS ordinal FROM ratings "
            "WHERE game = $1 AND player_id = ANY($2::text[])",
            game,
            ids,
        )
    }
    unrated = WengLin().ordinal
    return [
        SwissEntrant(
            pid,
            points.get(pid, 0.0) + BYE_POINTS * byes.count(pid),
            ordinal.get(pid, unrated),
            pid in byes,
        )
        for pid in ids
    ]


async def _queue_swiss_round(
    conn: asyncpg.Connection, tid: int, game: str, round_no: int, rng: random.Random
) -> int:
    """Pair and queue one round from the results so far. Returns matches queued."""
    entrants = await swiss_entrants(conn, tid, game)
    played = {frozenset(r["seats"]) for r in await conn.fetch(MEETINGS_SQL, tid)}
    pairs, bye = swiss_pairings(entrants, played, first_round=round_no == 1)
    seatings = swiss_seatings(pairs)
    await _insert_matches(conn, tid, game, seatings, rng, round_no)
    if bye is not None:
        await conn.execute(
            "INSERT INTO tournament_byes (tournament_id, round, player_id) VALUES ($1, $2, $3)",
            tid,
            round_no,
            bye,
        )
    return len(seatings)


async def advance_tournament(conn: asyncpg.Connection, tournament_id: int | None) -> None:
    """Once nothing is queued, running or failed: queue the next Swiss round, or mark
    the tournament completed. No-op unless the tournament is running. Call inside the
    transaction that moved a match to a final state."""
    if tournament_id is None:
        return
    t = await conn.fetchrow(LOCK_TOURNAMENT_SQL, tournament_id)
    if t is None or t["status"] != "running":
        return
    if await conn.fetchval(PENDING_SQL, tournament_id):
        return
    if t["format"] == "swiss" and t["current_round"] < t["rounds"]:
        n = t["current_round"]
        if await conn.fetchval(NEXT_ROUND_SQL, tournament_id, n) is None:
            return  # someone else already queued round n+1
        await _queue_swiss_round(conn, tournament_id, t["game"], n + 1, random.Random())
        return
    await conn.execute(COMPLETE_TOURNAMENT_SQL, tournament_id)


async def pause_tournament(conn: asyncpg.Connection, tournament_id: int | None, error: str) -> None:
    """Stop claiming and pairing until resumed. Matches already running finish."""
    if tournament_id is not None:
        await conn.execute(PAUSE_SQL, tournament_id, error[:2000])


async def resume_tournament(pool: asyncpg.Pool, tournament_id: int) -> int:
    """Re-queue failed matches (fresh run attempts) and set the tournament running.
    Returns the number of matches re-queued."""
    async with pool.acquire() as conn, conn.transaction():
        status = await conn.fetchval(
            "SELECT status FROM tournaments WHERE id = $1 FOR NO KEY UPDATE", tournament_id
        )
        if status is None:
            raise ValueError(f"no tournament {tournament_id}")
        requeued = await conn.fetchval(
            """
            WITH r AS (
                UPDATE matches
                SET status = 'queued', run_attempts = 0, available_at = now(),
                    last_error = NULL
                WHERE tournament_id = $1 AND status = 'failed'
                RETURNING 1
            )
            SELECT count(*) FROM r
            """,
            tournament_id,
        )
        await conn.execute(
            "UPDATE tournaments SET status = 'running', last_error = NULL "
            "WHERE id = $1 AND status = 'paused'",
            tournament_id,
        )
        # Nothing left to re-run (e.g. the failed match was the round's last): move on.
        await advance_tournament(conn, tournament_id)
    return requeued


async def leaderboard(pool: asyncpg.Pool, game: str) -> list[dict]:
    """Reads the materialised ratings table (written by queue.complete)."""
    return await ratings.leaderboard(pool, game)
