"""Grade recorded Connect 4 moves against perfect play.

Each move is graded by two exact solves: the position before it (the best score available) and
the position after it (the score of the move played). `grade` turns the pair into optimal,
inaccuracy or blunder. A solve that runs past the time limit makes the move "unsolved": it is
reported, never guessed. Early positions are where that happens, since Python searches about a
hundred times slower than Pascal Pons's C++ solver.

The empty board is graded from known theory instead: Pons's solver scores the seven first moves
-2, -1, 0, 1, 0, -1, -2 (Allis 1988: only the centre column wins for the first player).

Run: `python -m arena.solvers.c4_grade --tournament 3` (reads the database, writes nothing).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from arena.solvers.connect4 import Solver, SolveTimeout, grade

OPENING_SCORES = (-2, -1, 0, 1, 0, -1, -2)
TABLE_CAP = 1_000_000  # entries; about 200 bytes each, so the table stays near 200 MB


@dataclass(frozen=True)
class MoveGrade:
    ply: int
    seat: int
    column: int
    grade: str  # optimal | inaccuracy | blunder | unsolved
    best: int | None
    played: int | None
    source: str  # theory | search | timeout


def grade_moves(
    columns_played: Sequence[int], solver: Solver | None = None, time_limit: float = 5.0
) -> list[MoveGrade]:
    """Grade each move of one 7 by 6 game, given the columns played in order."""
    solver = solver or Solver()
    board: list[list[int]] = [[] for _ in range(solver.width)]
    grades = []
    for ply, col in enumerate(columns_played):
        seat = ply % 2
        if ply == 0:
            best, played = max(OPENING_SCORES), OPENING_SCORES[col]
            grades.append(MoveGrade(ply, seat, col, grade(best, played), best, played, "theory"))
        else:
            if len(solver.table) > TABLE_CAP:
                solver.table.clear()
            current, mask, moves = solver.from_columns(board)
            move = (mask + solver.bottom_of(col)) & solver.column_mask(col)
            try:
                best = solver.solve(current, mask, moves, time_limit)
                if solver.is_win(current | move):
                    played = (solver.cells + 1 - moves) // 2
                else:
                    played = -solver.solve(current ^ mask, mask | move, moves + 1, time_limit)
            except SolveTimeout:
                grades.append(MoveGrade(ply, seat, col, "unsolved", None, None, "timeout"))
            else:
                g = grade(best, played)
                grades.append(MoveGrade(ply, seat, col, g, best, played, "search"))
        board[col].append(seat)
    return grades


def summarize(graded: Sequence[tuple[str, MoveGrade]]) -> dict[str, Counter]:
    """Counts of each grade per player, from (player_id, grade) pairs."""
    out: dict[str, Counter] = defaultdict(Counter)
    for player, g in graded:
        out[player][g.grade] += 1
    return dict(out)


async def _grade_tournament(
    tournament_id: int, time_limit: float
) -> list[tuple[str, int, MoveGrade]]:
    from arena.config import settings
    from arena.store.db import create_pool
    from arena.store.turns import PostgresTurnStore

    pool = await create_pool(settings().database_url)
    try:
        store = PostgresTurnStore(pool, "c4-grade")
        matches = await pool.fetch(
            "SELECT m.id, array_agg(s.player_id ORDER BY s.seat) seats FROM matches m "
            "JOIN match_seats s ON s.match_id = m.id WHERE m.tournament_id = $1 "
            "AND m.status = 'completed' GROUP BY m.id ORDER BY m.id",
            tournament_id,
        )
        solver = Solver()
        out = []
        for m in matches:
            turns = await store.load_turns(str(m["id"]))
            cols = []
            for t in turns:
                if t.forfeited or t.action is None:
                    break  # a Connect 4 forfeit ends the game
                cols.append(int(t.action["column"]))
            for g in grade_moves(cols, solver, time_limit):
                out.append((m["seats"][g.seat], m["id"], g))
            done = Counter(g.grade for _, mid, g in out if mid == m["id"])
            print(f"match {m['id']}: {len(cols)} moves {dict(done)}", flush=True)
        return out
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tournament", type=int, required=True)
    parser.add_argument("--time-limit", type=float, default=5.0, help="seconds per solve")
    parser.add_argument("--out", help="also write every graded move as JSON lines")
    args = parser.parse_args()
    rows = asyncio.run(_grade_tournament(args.tournament, args.time_limit))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            for player, match_id, g in rows:
                f.write(json.dumps({"player": player, "match": match_id, **asdict(g)}) + "\n")
    for player, counts in sorted(summarize([(p, g) for p, _, g in rows]).items()):
        solved = sum(n for k, n in counts.items() if k != "unsolved")
        acc = counts["optimal"] / solved if solved else float("nan")
        print(f"{player}: {dict(counts)} accuracy {acc:.1%} of {solved} solved")


if __name__ == "__main__":
    main()
