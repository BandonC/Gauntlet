"""Tournament scheduling. Produces seatings only; the queue/worker runs them."""

from __future__ import annotations

import math
import random
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from itertools import combinations


def round_robin(player_ids: list[str], games_per_pair: int) -> list[list[str]]:
    """Every pair plays `games_per_pair` games, alternating who takes seat 0
    (moves first), so first-move advantage cancels out."""
    if len(set(player_ids)) != len(player_ids):
        raise ValueError("duplicate player ids")
    seatings: list[list[str]] = []
    for a, b in combinations(player_ids, 2):
        for i in range(games_per_pair):
            seatings.append([a, b] if i % 2 == 0 else [b, a])
    return seatings


def shuffle_matches(seatings: list[list[str]], seed: int) -> list[list[str]]:
    """The same matches, in a seeded random order. Live ratings depend on play order: if
    each pairing plays as a block, a player meeting a strong opponent after its rating
    has risen pays less for the same result (see First real run in CLAUDE.md)."""
    shuffled = list(seatings)
    random.Random(f"{seed}:order").shuffle(shuffled)
    return shuffled


# --- Swiss ------------------------------------------------------------------------
# Each pairing plays GAMES_PER_PAIRING games with seats swapped (first-move advantage
# cancels, as in round_robin). A bye is worth BYE_POINTS: the same as winning both games
# of a pairing. Byes are not matches, so they never touch Elo or Glicko-2.
GAMES_PER_PAIRING = 2
BYE_POINTS = 2.0


@dataclass(frozen=True)
class SwissEntrant:
    player_id: str
    points: float  # tournament points so far, byes included
    rating: float  # tie-break and round-1 seed: Weng-Lin ordinal (mu - 3*sigma)
    had_bye: bool = False


def default_swiss_rounds(n_players: int) -> int:
    return max(1, math.ceil(math.log2(n_players)))


def standings(entrants: list[SwissEntrant]) -> list[SwissEntrant]:
    """Points, then rating, then id: a total order, so pairing is deterministic."""
    return sorted(entrants, key=lambda e: (-e.points, -e.rating, e.player_id))


def swiss_pairings(
    entrants: list[SwissEntrant],
    played: Collection[frozenset[str]],
    first_round: bool = False,
) -> tuple[list[tuple[str, str]], str | None]:
    """Pair one round. Returns ((higher, lower) pairs in standings order, bye or None).

    - Odd field: the lowest-ranked entrant without a bye sits out (lowest overall if
      everyone has had one).
    - Round 1: top half of the seeding plays the bottom half (1 v N/2+1, ...).
    - Later rounds: each entrant plays the nearest-ranked opponent it hasn't met;
      backtracks if that strands someone. Rematches only if no rematch-free pairing
      of the whole field exists.
    """
    if len({e.player_id for e in entrants}) != len(entrants):
        raise ValueError("duplicate player ids")
    order = standings(entrants)
    bye = None
    if len(order) % 2:
        fresh = [e for e in order if not e.had_bye]
        bye = (fresh or order)[-1].player_id
        order = [e for e in order if e.player_id != bye]
    ids = [e.player_id for e in order]
    if first_round:
        half = len(ids) // 2
        return list(zip(ids[:half], ids[half:], strict=True)), bye
    pairs = _pair_avoiding(ids, set(played)) or _pair_avoiding(ids, set())
    assert pairs is not None  # with no constraints, adjacent pairing always works
    return pairs, bye


def _pair_avoiding(ids: list[str], played: set[frozenset[str]]) -> list[tuple[str, str]] | None:
    if not ids:
        return []
    first, rest = ids[0], ids[1:]
    for i, opp in enumerate(rest):
        if frozenset((first, opp)) in played:
            continue
        tail = _pair_avoiding(rest[:i] + rest[i + 1 :], played)
        if tail is not None:
            return [(first, opp), *tail]
    return None


def swiss_seatings(pairs: list[tuple[str, str]]) -> list[list[str]]:
    """GAMES_PER_PAIRING games per pairing, alternating who takes seat 0."""
    return [[a, b] if i % 2 == 0 else [b, a] for a, b in pairs for i in range(GAMES_PER_PAIRING)]


# --- Tables -----------------------------------------------------------------------
# Multi-seat tournaments: a fixed number of tables, all queued at once. No multiplayer
# Swiss. Seat counts are balanced exactly; pair balance is a search, stopped early once
# no pair is more than one meeting apart.
_SEARCH_STEPS = 100_000
_COOLING_CYCLE = 20_000
_HOT, _COLD = 1.0, 0.05


def pair_counts(player_ids: list[str], seatings: list[list[str]]) -> dict[frozenset[str], int]:
    """How many tables each pair of players shares, including pairs that never meet."""
    counts = {frozenset(pair): 0 for pair in combinations(player_ids, 2)}
    for seats in seatings:
        for pair in combinations(seats, 2):
            counts[frozenset(pair)] += 1
    return counts


def role_counts(
    player_ids: list[str], seatings: list[list[str]], roles: list[list[str]]
) -> dict[str, dict[str, int]]:
    """role -> player -> tables played in that role, including zeros."""
    counts = {r: dict.fromkeys(player_ids, 0) for table in roles for r in table}
    for seats, table in zip(seatings, roles, strict=True):
        for pid, role in zip(seats, table, strict=True):
            counts[role][pid] += 1
    return counts


def assign_roles(
    player_ids: list[str], seatings: list[list[str]], role_pool: list[str], seed: int
) -> list[list[str]]:
    """Deal role_pool at every table, in seat order, so that for each role players'
    counts differ by at most 1. Pure. Like pair balance this is a search: it stops as soon
    as that holds, otherwise returns the most even deal found. Swaps within a table keep
    each table's roles intact, so the sum of squared counts reaches its minimum exactly
    when the deal is as even as the tables allow."""
    rng = random.Random(seed)
    roles = [rng.sample(role_pool, len(role_pool)) for _ in seatings]
    counts = {(p, r): 0 for p in player_ids for r in role_pool}
    for seats, table in zip(seatings, roles, strict=True):
        for pid, role in zip(seats, table, strict=True):
            counts[pid, role] += 1
    cost = sum(c * c for c in counts.values())

    def even() -> bool:
        return all(
            max(by_player.values()) - min(by_player.values()) <= 1
            for by_player in role_counts(player_ids, seatings, roles).values()
        )

    def swap(table: int, i: int, j: int) -> None:
        nonlocal cost
        p, q = seatings[table][i], seatings[table][j]
        a, b = roles[table][i], roles[table][j]
        for key, by in (((p, a), -1), ((q, b), -1), ((p, b), 1), ((q, a), 1)):
            cost += by * (2 * counts[key] + by)
            counts[key] += by
        roles[table][i], roles[table][j] = b, a

    best, best_cost = [list(t) for t in roles], cost
    if len(role_pool) < 2 or even():
        return best
    for step in range(_SEARCH_STEPS):
        temperature = _HOT * (_COLD / _HOT) ** ((step % _COOLING_CYCLE) / _COOLING_CYCLE)
        t = rng.randrange(len(roles))
        i, j = rng.sample(range(len(role_pool)), 2)
        if roles[t][i] == roles[t][j]:
            continue
        before = cost
        swap(t, i, j)
        delta = cost - before
        if delta > 0 and rng.random() >= math.exp(-delta / temperature):
            swap(t, i, j)  # undo
        elif cost < best_cost:
            best, best_cost = [list(t) for t in roles], cost
            if even():
                break
    return best


def table_schedule(
    player_ids: list[str], table_size: int, tables: int, seed: int
) -> list[list[str]]:
    """Seatings for `tables` matches of `table_size` seats. Pure: the same arguments
    always give the same schedule.

    Always holds: players' game counts differ by at most 1, and so do each player's
    counts across seats. Pair meetings differ by at most 1 whenever the search finds
    such a schedule, otherwise the most even one found. One may not exist: 5 tables of
    3 from 6 players would need every pair to meet exactly once, which is impossible.
    """
    if len(set(player_ids)) != len(player_ids):
        raise ValueError("duplicate player ids")
    if not 2 <= table_size <= len(player_ids):
        raise ValueError(f"table size must be 2-{len(player_ids)}")
    if tables < 1:
        raise ValueError("a tournament needs at least 1 table")
    rng = random.Random(seed)
    n = len(player_ids)
    groups = _balance_pairs(_deal_tables(n, table_size, tables, rng), n, rng)
    return [[player_ids[p] for p in seats] for seats in _assign_seats(groups, table_size)]


def _deal_tables(n: int, k: int, tables: int, rng: random.Random) -> list[list[int]]:
    """Each table takes the k players with the fewest games so far, so game counts
    never differ by more than 1. Later swaps keep every player's count."""
    games = [0] * n
    groups = []
    for _ in range(tables):
        group = sorted(range(n), key=lambda p: (games[p], rng.random()))[:k]
        for p in group:
            games[p] += 1
        groups.append(group)
    return groups


def _balance_pairs(groups: list[list[int]], n: int, rng: random.Random) -> list[list[int]]:
    """Simulated annealing over swaps between tables, minimising the sum of squared pair
    meetings. That sum reaches its lower bound exactly when meetings differ by at most 1."""
    k = len(groups[0])
    meets = [[0] * n for _ in range(n)]
    cost = 0

    def bump(a: int, b: int, by: int) -> None:
        nonlocal cost
        cost += by * (2 * meets[a][b] + by)
        meets[a][b] += by
        meets[b][a] += by

    def swap(g1: list[int], i: int, g2: list[int], j: int) -> None:
        x, y = g1[i], g2[j]
        for z in g1:
            if z != x:
                bump(x, z, -1)
                bump(y, z, 1)
        g1[i] = y
        for z in g2:
            if z != y:
                bump(y, z, -1)
                bump(x, z, 1)
        g2[j] = x

    for g in groups:
        for a, b in combinations(g, 2):
            bump(a, b, 1)

    pair_slots, pairs = len(groups) * k * (k - 1) // 2, n * (n - 1) // 2
    per_pair, extra = divmod(pair_slots, pairs)
    target = extra * (per_pair + 1) ** 2 + (pairs - extra) * per_pair**2

    best, best_cost = [list(g) for g in groups], cost
    if len(groups) < 2:
        return best
    for step in range(_SEARCH_STEPS):
        if best_cost == target:
            break
        temperature = _HOT * (_COLD / _HOT) ** ((step % _COOLING_CYCLE) / _COOLING_CYCLE)
        g1, g2 = rng.sample(groups, 2)
        i, j = rng.randrange(k), rng.randrange(k)
        if g1[i] in g2 or g2[j] in g1:
            continue
        before = cost
        swap(g1, i, g2, j)
        delta = cost - before
        if delta > 0 and rng.random() >= math.exp(-delta / temperature):
            swap(g1, i, g2, j)  # undo
        elif cost < best_cost:
            best, best_cost = [list(g) for g in groups], cost
    return best


def _assign_seats(groups: list[list[int]], k: int) -> list[list[int]]:
    """Order each table so every player's seat counts differ by at most 1.

    An edge colouring: tables on one side, players on the other, colours are seats. Each
    player is split into blocks of k appearances, which makes every node's degree at most
    k, so a proper k-colouring exists (Konig) and alternating paths always find one. A
    table's k edges then use each seat once, a full block uses each seat once, and only a
    player's last partial block can leave its seat counts one apart.
    """
    at: dict[tuple[int, ...], list[tuple[int, ...] | None]] = {}
    appearances: dict[int, int] = {}
    for t, group in enumerate(groups):
        table = (t,)
        at[table] = [None] * k
        for p in group:
            seen = appearances.get(p, 0)
            appearances[p] = seen + 1
            block = (p, seen // k)
            at.setdefault(block, [None] * k)
            seat, free = at[table].index(None), at[block].index(None)
            if seat != free:
                # `seat` is taken at the block: flip the seat/free path starting there.
                # It can't reach this table, where `seat` is unused.
                path = []
                node: tuple[int, ...] = block
                colour = seat
                while (next_node := at[node][colour]) is not None:
                    path.append((node, next_node, colour))
                    node, colour = next_node, seat + free - colour
                for u, v, c in path:
                    at[u][c] = at[v][c] = None
                for u, v, c in path:
                    at[u][seat + free - c] = v
                    at[v][seat + free - c] = u
            at[table][seat], at[block][seat] = block, table
    return [[at[(t,)][seat][0] for seat in range(k)] for t in range(len(groups))]  # type: ignore[index]


def team_round_robin(teams: Sequence[Sequence[str]], games_per_pair: int) -> list[list[str]]:
    """Every team against every other, each going first in half the games. An odd count
    gives the earlier team the extra one."""
    seatings = []
    for a, b in combinations(teams, 2):
        for game in range(games_per_pair):
            first, second = (a, b) if game % 2 == 0 else (b, a)
            seatings.append([*first, *second])
    return seatings
