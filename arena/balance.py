"""Werewolf balance simulator: what-if rulesets, no engine changes and no API calls.

`play` re-implements the phase loop of `games/werewolf.py` with the rules as parameters, so
candidates can be compared before any of them becomes a game. Nothing here is playable:
a ruleset that gets adopted becomes a new game id, so its ratings stay apart from the old.
`tests/test_balance.py` holds `play` to the real engine on the current rules.

Votes are a model. Each village voter picks a wolf with probability `p`, which is either one
number for every vote or a curve by nights elapsed: `p[n]` after n nights, the last entry for
any night beyond it. `FITTED_P` is that curve from tournaments 6 and 7 (see
`arena.analysis.vote_accuracy`); it's indexed by nights, not days, because a night is when the
kill and the seer's inspection add information, so one curve covers both openings. A seer's or
doctor's skill never shows up in `p` beyond what those tournaments already contain.
`focus` sets how much a table votes together without changing `p`: each day the table has
one shared target (a wolf with probability p), and each voter follows it with probability
`focus`, otherwise drawing alone. 0 is fully independent voters. Wolves vote for a non-wolf:
the table's target if it is one, otherwise one shared counter-target. Kills are uniform
over living non-wolves, and a doctor protects a uniform living seat.

    python -m arena.balance --games 2000            # the fitted per-night curve
    python -m arena.balance --games 2000 --p 0.54   # the old fixed accuracy
"""

from __future__ import annotations

import argparse
import random
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from arena.games.werewolf import (
    DISCUSSION_ROUNDS,
    SEER,
    VILLAGE_WINS,
    VILLAGER,
    WOLF,
    winner_of,
)

DOCTOR = "doctor"
DEFAULT_P = 0.54  # tournament 6: 125 of 229 village votes hit a wolf
# Village votes on a wolf after 0, 1 and 2 nights, tournaments 6 and 7 pooled: 73/200,
# 152/293, 62/99. After 3 nights there were 6 votes, too few to fit, so the last entry holds.
FITTED_P = (73 / 200, 152 / 293, 62 / 99)
FITTED_FOCUS = 0.7

Accuracy = float | Sequence[float]


def accuracy_after(p: Accuracy, nights: int) -> float:
    """`p` itself, or the curve's entry after this many nights (its last, past the end)."""
    if isinstance(p, (int, float)):
        return float(p)
    return p[min(nights, len(p) - 1)]


@dataclass(frozen=True)
class Ruleset:
    name: str
    seats: int = 7
    wolves: int = 2
    doctor: bool = False
    day_first: bool = False

    def roles(self) -> list[str]:
        specials = [WOLF] * self.wolves + [SEER] + [DOCTOR] * self.doctor
        return specials + [VILLAGER] * (self.seats - len(specials))


CURRENT = Ruleset("a) current rules")
VARIANTS = (
    CURRENT,
    Ruleset("b) day-first", day_first=True),
    Ruleset("c) 9 seats, 2 wolves", seats=9),
    Ruleset("d) current + doctor", doctor=True),
    Ruleset("e) day-first + doctor", day_first=True, doctor=True),
)


@dataclass(frozen=True)
class Game:
    village_won: bool
    turns: int
    first_vote: str  # "wolf", "other" or "tie"


@dataclass(frozen=True)
class Result:
    rules: Ruleset
    games: int
    village_wins: int
    mean_turns: float
    first_vote: Counter

    @property
    def village_rate(self) -> float:
        return self.village_wins / self.games

    @property
    def margin(self) -> float:
        """Half-width of a 95% interval on village_rate."""
        rate = self.village_rate
        return 1.96 * (rate * (1 - rate) / self.games) ** 0.5


def vote_target(
    roles: list[str], alive: list[int], seat: int, day: int, seed: int, p: float, focus: float
) -> int:
    """One seat's day vote. A pure function, so `AccuracyVoteBot` and `play` share it."""
    table = random.Random(f"{seed}:table:{day}")
    own = random.Random(f"{seed}:seat:{day}:{seat}")
    wolves = [s for s in alive if roles[s] == WOLF]
    others = [s for s in alive if roles[s] != WOLF]
    target = table.choice(wolves) if table.random() < p else table.choice(others)
    counter = table.choice(others)
    if roles[seat] == WOLF:
        return target if roles[target] != WOLF else counter
    innocent = [s for s in others if s != seat]
    if own.random() < focus:
        return target if target != seat else own.choice(innocent)  # the accused still misses
    return own.choice(wolves if own.random() < p else innocent)


def _winner(roles: list[str], alive: list[int]) -> bool | None:
    """True if the village has won, False if the wolves have, None if the game goes on."""
    winner = winner_of(tuple(alive), tuple(roles))
    return None if winner is None else winner == VILLAGE_WINS


def _voted_out(votes: list[int]) -> int | None:
    counts = Counter(votes).most_common()
    return None if len(counts) > 1 and counts[0][1] == counts[1][1] else counts[0][0]


def protect(rng: random.Random, alive: list[int], doctor: int, last_saved: int | None) -> int:
    """A uniform living seat, except the doctor itself right after protecting itself."""
    return rng.choice([s for s in alive if not (s == doctor == last_saved)])


def play(rules: Ruleset, seed: int, p: Accuracy = DEFAULT_P, focus: float = 0.0) -> Game:
    rng = random.Random(f"{seed}:game")
    roles = rules.roles()
    rng.shuffle(roles)
    alive = list(range(rules.seats))
    turns, day, night_next, first_vote, last_saved = 0, 1, not rules.day_first, "", None
    nights = 0
    while True:
        if night_next:
            nights += 1
            turns += sum(roles[s] != VILLAGER for s in alive)
            victim = rng.choice([s for s in alive if roles[s] != WOLF])
            doctor = next((s for s in alive if roles[s] == DOCTOR), None)
            last_saved = protect(rng, alive, doctor, last_saved) if doctor is not None else None
            if victim != last_saved:
                alive.remove(victim)
                won = _winner(roles, alive)
                if won is not None:
                    return Game(won, turns, first_vote)
        turns += len(alive) * (DISCUSSION_ROUNDS + 1)
        today = accuracy_after(p, nights)
        votes = [vote_target(roles, alive, s, day, seed, today, focus) for s in alive]
        out = _voted_out(votes)
        if not first_vote:
            first_vote = "tie" if out is None else "wolf" if roles[out] == WOLF else "other"
        if out is not None:
            alive.remove(out)
            won = _winner(roles, alive)
            if won is not None:
                return Game(won, turns, first_vote)
        day, night_next = day + 1, True


def simulate(rules: Ruleset, games: int, p: Accuracy = DEFAULT_P, focus: float = 0.0) -> Result:
    played = [play(rules, seed, p, focus) for seed in range(games)]
    return Result(
        rules,
        games,
        sum(g.village_won for g in played),
        sum(g.turns for g in played) / games,
        Counter(g.first_vote for g in played),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--games", type=int, default=2000)
    parser.add_argument("--p", type=float, help="one fixed accuracy instead of FITTED_P")
    parser.add_argument("--focus", type=float, default=FITTED_FOCUS)
    args = parser.parse_args()
    p: Accuracy = FITTED_P if args.p is None else args.p
    for rules in VARIANTS:
        r = simulate(rules, args.games, p, args.focus)
        print(
            f"{rules.name:24} {100 * r.village_rate:5.1f}% ±{100 * r.margin:.1f}  "
            f"{r.mean_turns:5.1f} turns"
        )


if __name__ == "__main__":
    main()
