"""Village vote accuracy by nights elapsed, from recorded Werewolf games. Read only, free.

How often a living villager's day vote names a wolf, grouped by how many nights the table has
played before that vote: night-first `werewolf` votes on day d after d nights, `werewolf_dayfirst`
after d - 1. Indexing by nights rather than by day is what lets one curve serve both openings,
since a night is when the kill and the seer's inspection add information. It also counts each
table's first vote as a wolf out, someone else out, or a tie. `arena.balance` uses the fitted
curve; this module is how to refit it.

    python -m arena.analysis.vote_accuracy --tournaments 6,7
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections import Counter
from dataclasses import dataclass, field

from arena.games.werewolf import WOLF, WWState

# Nights played before day 1's vote, by game id.
NIGHTS_BEFORE_DAY_ONE = {"werewolf": 1, "werewolf_dayfirst": 0}


@dataclass
class Tally:
    hits: Counter[int] = field(default_factory=Counter)  # nights elapsed -> votes on a wolf
    votes: Counter[int] = field(default_factory=Counter)  # nights elapsed -> village votes
    first_vote: Counter[str] = field(default_factory=Counter)  # "wolf", "other", "tie"
    games: int = 0
    village_wins: int = 0

    def add(self, other: Tally) -> None:
        self.hits.update(other.hits)
        self.votes.update(other.votes)
        self.first_vote.update(other.first_vote)
        self.games += other.games
        self.village_wins += other.village_wins

    def rates(self) -> dict[int, float]:
        return {n: self.hits[n] / self.votes[n] for n in sorted(self.votes)}


def tally(game: str, state: WWState, village_won: bool) -> Tally:
    """One finished game. A villager is any living non-wolf at the vote, the seer included."""
    offset = NIGHTS_BEFORE_DAY_ONE[game] - 1
    wolves = {s for s, r in enumerate(state.roles) if r == WOLF}
    out = Tally(games=1, village_wins=int(village_won))
    for day in sorted({v.day for v in state.day_votes}):
        dead = {
            d.seat for d in state.deaths if d.day < day or (d.day == day and d.cause == "night")
        }
        for v in state.day_votes:
            if v.day == day and v.seat not in wolves and v.seat not in dead:
                out.votes[day + offset] += 1
                out.hits[day + offset] += v.target in wolves
    first_day = min((v.day for v in state.day_votes), default=None)
    if first_day is not None:
        voted = [d for d in state.deaths if d.day == first_day and d.cause == "vote"]
        out.first_vote["tie" if not voted else "wolf" if voted[0].role == WOLF else "other"] += 1
    return out


async def load(database_url: str, tournaments: list[int]) -> dict[int, Tally]:
    from arena.engine.match_runner import MatchSpec, iter_states
    from arena.games.registry import get_game
    from arena.store.db import create_pool
    from arena.store.turns import PostgresTurnStore

    pool = await create_pool(database_url)
    try:
        store = PostgresTurnStore(pool, "vote-accuracy")
        rows = await pool.fetch(
            "SELECT m.id, m.tournament_id, m.game, m.seed, m.options, m.scores, "
            "array_agg(s.player_id ORDER BY s.seat) seats "
            "FROM matches m JOIN match_seats s ON s.match_id = m.id "
            "WHERE m.tournament_id = ANY($1) AND m.status = 'completed' "
            "AND m.game = ANY($2) GROUP BY m.id ORDER BY m.id",
            tournaments,
            list(NIGHTS_BEFORE_DAY_ONE),
        )
        out: dict[int, Tally] = {t: Tally() for t in tournaments}
        for r in rows:
            spec = MatchSpec(str(r["id"]), r["game"], r["seed"], list(r["seats"]), r["options"])
            turns = await store.load_turns(str(r["id"]))
            *_, final = iter_states(get_game(r["game"]), spec, turns)
            out[r["tournament_id"]].add(tally(r["game"], final, sum(r["scores"]) == 5))
        return out
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tournaments", default="6,7")
    args = parser.parse_args()
    ids = [int(t) for t in args.tournaments.split(",")]
    url = os.environ.get("DATABASE_URL") or "postgresql://arena:arena@localhost:5434/arena"
    by_t = asyncio.run(load(url, ids))
    pooled = Tally()
    for t, tl in by_t.items():
        pooled.add(tl)
        cells = "  ".join(f"n{n} {tl.hits[n]}/{tl.votes[n]}" for n in sorted(tl.votes))
        print(
            f"tournament {t}: {cells}  first vote {dict(tl.first_vote)}  "
            f"village {tl.village_wins}/{tl.games}"
        )
    cells = "  ".join(
        f"n{n} {pooled.hits[n]}/{pooled.votes[n]} ({r:.3f})" for n, r in pooled.rates().items()
    )
    print(f"pooled: {cells}")


if __name__ == "__main__":
    main()
