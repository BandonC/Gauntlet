"""Per-player operational metrics, computed from the turn log.

Definitions (all per player, for one game, optionally one tournament):
- illegal_move_rate: invalid attempts / all attempts. A turn that succeeded on attempt k
  had k-1 invalid attempts; a forfeited turn's attempts were all invalid.
- forfeits: forfeited turns.
- auto_fills: turns the engine completed (Game.fill_action). Legal, so not illegal attempts.
- latency_p50_ms / latency_p95_ms: per turn, including retries within the turn.
- cost_per_game_usd: spend in completed matches / completed matches. Turns of matches
  still running count towards everything else.
- cache_hit_rate: cache reads / (uncached input + cache reads + cache writes).
Rates are None when their denominator is zero.

werewolf_metrics (per player, every Werewolf ruleset, computed in Python from the match
seeds and turn log):
- <role>_games / <role>_win_rate: completed matches played in that role and the share
  won. Roles strongly affect results, so read ratings beside these.
- true_claims / false_claims / leaks: explicit role claims in messages, classified by
  werewolf.classify_claims. Literal matching only, so paraphrases are missed.
- truncated_messages: messages cut at the length limit.
- forfeits: skipped actions. A seat stays alive after one, so it can forfeit repeatedly,
  and placements don't show it.
"""

from __future__ import annotations

from collections import Counter

import asyncpg

from arena.engine.match_runner import MatchSpec, iter_states
from arena.games.code_break import seat_stats
from arena.games.registry import get_game
from arena.games.werewolf import SEER, VILLAGER, WOLF, classify_claims
from arena.store.ratings import NOT_CANCELLED_SQL
from arena.store.turns import PostgresTurnStore

METRICS_SQL = """
WITH t AS (
    SELECT s.player_id, t.match_id, m.status AS match_status, t.attempts, t.forfeited,
           t.latency_ms, t.cost_usd, t.input_tokens, t.cache_read_tokens,
           t.cache_write_tokens, t.auto_filled
    FROM turns t
    JOIN matches m ON m.id = t.match_id
    JOIN match_seats s ON s.match_id = t.match_id AND s.seat = t.seat
    WHERE m.game = $1 AND ($2::bigint IS NULL OR m.tournament_id = $2)
)
SELECT player_id,
       count(*) AS turns,
       sum(attempts) AS attempts,
       sum(attempts) - count(*) FILTER (WHERE NOT forfeited) AS illegal_attempts,
       count(*) FILTER (WHERE forfeited) AS forfeits,
       count(*) FILTER (WHERE auto_filled) AS auto_fills,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS latency_p50_ms,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS latency_p95_ms,
       count(DISTINCT match_id) FILTER (WHERE match_status = 'completed') AS completed_games,
       coalesce(sum(cost_usd) FILTER (WHERE match_status = 'completed'), 0)::float
           AS completed_cost_usd,
       sum(input_tokens) AS input_tokens,
       sum(cache_read_tokens) AS cache_read_tokens,
       sum(cache_write_tokens) AS cache_write_tokens
FROM t
GROUP BY player_id
ORDER BY player_id
"""


WEREWOLF_GAMES = ["werewolf", "werewolf_dayfirst"]  # rulesets are told apart by player id

WEREWOLF_MATCHES_SQL = """
SELECT m.id, m.game, m.seed, m.options, m.status, m.scores,
       array_agg(s.player_id ORDER BY s.seat) AS seats
FROM matches m JOIN match_seats s ON s.match_id = m.id
WHERE m.game = ANY($2::text[]) AND ($1::bigint IS NULL OR m.tournament_id = $1)
GROUP BY m.id
"""

WEREWOLF_MESSAGES_SQL = """
SELECT t.match_id, t.seat, t.action ->> 'message' AS message, t.truncated
FROM turns t JOIN matches m ON m.id = t.match_id
WHERE m.game = ANY($2::text[]) AND ($1::bigint IS NULL OR m.tournament_id = $1)
  AND t.action ->> 'action' = 'say'
"""

WEREWOLF_FORFEITS_SQL = """
SELECT t.match_id, t.seat, count(*) AS forfeits
FROM turns t JOIN matches m ON m.id = t.match_id
WHERE m.game = ANY($2::text[]) AND ($1::bigint IS NULL OR m.tournament_id = $1) AND t.forfeited
GROUP BY t.match_id, t.seat
"""


def _ratio(num: float, den: float, digits: int = 4) -> float | None:
    return round(num / den, digits) if den else None


async def player_metrics(
    pool: asyncpg.Pool, game: str, tournament_id: int | None = None
) -> list[dict]:
    rows = await pool.fetch(METRICS_SQL, game, tournament_id)
    out = []
    for r in rows:
        prompt_tokens = r["input_tokens"] + r["cache_read_tokens"] + r["cache_write_tokens"]
        out.append(
            {
                "player_id": r["player_id"],
                "turns": r["turns"],
                "attempts": r["attempts"],
                "illegal_attempts": r["illegal_attempts"],
                "illegal_move_rate": _ratio(r["illegal_attempts"], r["attempts"]),
                "forfeits": r["forfeits"],
                "auto_fills": r["auto_fills"],
                "latency_p50_ms": round(r["latency_p50_ms"], 1),
                "latency_p95_ms": round(r["latency_p95_ms"], 1),
                "completed_games": r["completed_games"],
                "cost_per_game_usd": _ratio(r["completed_cost_usd"], r["completed_games"], 6),
                "cache_hit_rate": _ratio(r["cache_read_tokens"], prompt_tokens),
            }
        )
    return out


CODE_BREAK_MATCHES_SQL = f"""
SELECT m.id, m.game, m.seed, m.options, array_agg(s.player_id ORDER BY s.seat) AS seats
FROM matches m JOIN match_seats s ON s.match_id = m.id
WHERE m.game = 'code_break' AND m.status = 'completed'
  AND ($1::bigint IS NULL OR m.tournament_id = $1) AND {NOT_CANCELLED_SQL}
GROUP BY m.id
"""


async def code_break_metrics(pool: asyncpg.Pool, tournament_id: int | None = None) -> list[dict]:
    """Per player, from replaying completed matches. `above_solver` is guesses over what the
    minimax solver needs on the same secret, for games the player solved. `inconsistent` is
    guesses that contradict the player's own earlier feedback: legal, but they waste a turn.
    A player cut off by the opponent's solve shows as unsolved, so read `solved` with `games`.
    """
    tally: dict[str, Counter] = {}
    store = PostgresTurnStore(pool, "metrics")
    for m in await pool.fetch(CODE_BREAK_MATCHES_SQL, tournament_id):
        seats = list(m["seats"])
        spec = MatchSpec(str(m["id"]), m["game"], m["seed"], seats, m["options"])
        *_, final = iter_states(get_game(m["game"]), spec, await store.load_turns(str(m["id"])))
        for seat, pid in enumerate(seats):
            stats = seat_stats(final, seat)
            c = tally.setdefault(pid, Counter())
            c["games"] += 1
            c["guesses"] += stats["guesses"]
            c["inconsistent"] += stats["inconsistent"]
            if stats["solved"]:
                c["solved"] += 1
                c["above_solver"] += stats["above_solver"]
    return [
        {
            "player_id": pid,
            "games": c["games"],
            "solved": c["solved"],
            "guesses": c["guesses"],
            "inconsistent": c["inconsistent"],
            "inconsistent_rate": _ratio(c["inconsistent"], c["guesses"]),
            "mean_above_solver": _ratio(c["above_solver"], c["solved"]),
        }
        for pid, c in sorted(tally.items())
    ]


async def werewolf_metrics(pool: asyncpg.Pool, tournament_id: int | None = None) -> list[dict]:
    args = (tournament_id, WEREWOLF_GAMES)
    matches = {r["id"]: r for r in await pool.fetch(WEREWOLF_MATCHES_SQL, *args)}
    tally: dict[str, Counter] = {}
    roles_of = {}
    for mid, m in matches.items():
        seats = list(m["seats"])
        # Scheduled roles are in options; otherwise the seed deals them, as in the match.
        game = get_game(m["game"])
        roles = list(game.initial_state(len(seats), m["seed"], m["options"]).roles)
        roles_of[mid] = roles
        for seat, pid in enumerate(seats):
            c = tally.setdefault(pid, Counter())
            if m["status"] == "completed":
                c[f"{roles[seat]}_games"] += 1
                c[f"{roles[seat]}_wins"] += m["scores"][seat] == 1.0
    for r in await pool.fetch(WEREWOLF_MESSAGES_SQL, *args):
        c = tally[matches[r["match_id"]]["seats"][r["seat"]]]
        c["truncated_messages"] += r["truncated"]
        for claim in classify_claims(r["message"], r["seat"], roles_of[r["match_id"]]):
            c[{"true": "true_claims", "false": "false_claims", "leak": "leaks"}[claim.kind]] += 1
    for r in await pool.fetch(WEREWOLF_FORFEITS_SQL, *args):
        tally[matches[r["match_id"]]["seats"][r["seat"]]]["forfeits"] += r["forfeits"]
    out = []
    for pid, c in sorted(tally.items()):
        row: dict = {"player_id": pid}
        for role in (WOLF, SEER, VILLAGER):
            row[f"{role}_games"] = c[f"{role}_games"]
            row[f"{role}_win_rate"] = _ratio(c[f"{role}_wins"], c[f"{role}_games"])
        for key in ("true_claims", "false_claims", "leaks", "truncated_messages", "forfeits"):
            row[key] = c[key]
        out.append(row)
    return out
