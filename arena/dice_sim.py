"""Liar's Dice bot against bot on the real engine: free, in memory, no database.

`sweep` tunes a bot's challenge threshold against v1. `field` then plays the leading
thresholds against each other and against bluffers, because beating v1 alone can just mean
exploiting v1. `evaluate` checks the choice on seeds tuning never saw, so the best of many
thresholds isn't flattered by its own luck. Seat 0 opens every game, so seats alternate.

    python -m arena.dice_sim sweep --games 4000
    python -m arena.dice_sim field --games 1000
    python -m arena.dice_sim evaluate --games 1000
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterable

from arena.games.liars_dice import FACES, LiarsDice
from arena.players.base import Decision, Player, TurnContext
from arena.players.bots import (
    LIARS_DICE_V1_THRESHOLD,
    LIARS_DICE_V2_THRESHOLD,
    LiarsDiceBot,
    _at_least,
)
from arena.web import wilson

TUNING_SEEDS = 0  # sweep starts here
FIELD_SEEDS = 500_000  # field starts here
EVALUATION_SEEDS = 1_000_000  # evaluate starts here, far from any tuning run
THRESHOLDS = [round(0.05 * i, 2) for i in range(1, 15)]
FIELD_THRESHOLDS = [0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5]
FIELD_BLUFFERS = [(1, 0.3), (1, 0.5), (2, 0.3), (2, 0.5)]  # (extra quantity, threshold)


class Bluffer:
    """The threshold rule, raising `extra` above the minimal raise: pressure on a bot that
    seldom challenges. A sparring partner for `field`, not a tournament player."""

    def __init__(self, player_id: str, extra: int, challenge_below: float):
        self.player_id = player_id
        self.extra = extra
        self.challenge_below = challenge_below

    async def act(self, ctx: TurnContext) -> Decision:
        v = ctx.view
        own, total, bids = v["your_dice"], v["total_dice"], v["bids"]

        def p_true(q: int, f: int) -> float:
            return _at_least(q - own.count(f), total - len(own), 1 / FACES)

        last = bids[-1] if bids else None
        if last and p_true(last["quantity"], last["face"]) < self.challenge_below:
            return Decision(action={"action": "challenge"})
        options = []
        for f in range(1, FACES + 1):
            if last is None:
                q = 1
            else:
                q = last["quantity"] + (0 if f > last["face"] else 1)
            q = min(q + self.extra, total)
            if last is None or (q, f) > (last["quantity"], last["face"]):
                options.append((-p_true(q, f), q, -f))
        if not options:
            return Decision(action={"action": "challenge"})
        _, q, neg_f = min(options)
        return Decision(action={"action": "bid", "quantity": q, "face": -neg_f})


async def play(players: list[Player], seed: int) -> int:
    """One game; returns the winning seat. An illegal action raises: a bot must never make one."""
    game = LiarsDice()
    state = game.initial_state(len(players), seed)
    while not game.is_terminal(state):
        seat = game.current_player(state)
        view = game.player_view(state, seat)
        ctx = TurnContext("liars_dice", seat, "", view, "", game.action_spec(state, seat), None)
        action = (await players[seat].act(ctx)).action
        assert action is not None
        error = game.validate_action(state, seat, action)
        if error:
            raise ValueError(f"seed {seed}: {players[seat].player_id} played {action}: {error}")
        state = game.apply(state, seat, action)
    assert state.winner is not None  # the loop ended on a terminal state
    return state.winner


async def head_to_head(first: Player, second: Player, seeds: Iterable[int]) -> tuple[int, int]:
    """Games `first` won, and games played, with seats alternating."""
    wins = games = 0
    for i, seed in enumerate(seeds):
        seats = [first, second] if i % 2 == 0 else [second, first]
        wins += seats[await play(seats, seed)] is first
        games += 1
    return wins, games


async def sweep(games: int, opponent: float = LIARS_DICE_V1_THRESHOLD) -> list[dict]:
    """Every threshold against the same opponent on the same tuning seeds."""
    rows = []
    for threshold in THRESHOLDS:
        seeds = range(TUNING_SEEDS, TUNING_SEEDS + games)
        wins, played = await head_to_head(
            LiarsDiceBot("candidate", threshold), LiarsDiceBot("opponent", opponent), seeds
        )
        rows.append({"threshold": threshold, "wins": wins, "games": played})
    return rows


async def field(games: int) -> dict[float, dict[str, float]]:
    """Win rate of each threshold against every threshold and bluffer, on one set of seeds."""
    opponents: dict[str, Player] = {
        f"{t:.2f}": LiarsDiceBot("opponent", t) for t in FIELD_THRESHOLDS
    }
    for extra, threshold in FIELD_BLUFFERS:
        opponents[f"+{extra}@{threshold}"] = Bluffer("bluffer", extra, threshold)
    seeds = range(FIELD_SEEDS, FIELD_SEEDS + games)
    table: dict[float, dict[str, float]] = {}
    for threshold in FIELD_THRESHOLDS:
        candidate = LiarsDiceBot("candidate", threshold)
        table[threshold] = {}
        for name, opponent in opponents.items():
            wins, played = await head_to_head(candidate, opponent, seeds)
            table[threshold][name] = wins / played
    return table


async def evaluate(games: int) -> tuple[int, int]:
    """The v2 threshold against v1, on seeds tuning never touched."""
    seeds = range(EVALUATION_SEEDS, EVALUATION_SEEDS + games)
    return await head_to_head(
        LiarsDiceBot("v2", LIARS_DICE_V2_THRESHOLD),
        LiarsDiceBot("v1", LIARS_DICE_V1_THRESHOLD),
        seeds,
    )


def _line(label: str, wins: int, games: int) -> str:
    low, high = wilson(wins, games)
    return f"{label}: {wins}/{games} = {wins / games:.1%} (95% interval {low:.1%}-{high:.1%})"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["sweep", "field", "evaluate"])
    parser.add_argument("--games", type=int, default=1000)
    args = parser.parse_args()
    if args.command == "sweep":
        for row in asyncio.run(sweep(args.games)):
            print(_line(f"{row['threshold']:.2f}", row["wins"], row["games"]))
    elif args.command == "field":
        table = asyncio.run(field(args.games))
        names = list(next(iter(table.values())))
        print("      " + " ".join(f"{n:>8}" for n in names) + "     mean")
        for threshold, row in table.items():
            cells = " ".join(f"{row[n]:8.1%}" for n in names)
            print(f"{threshold:.2f}  {cells} {sum(row.values()) / len(row):8.1%}")
    else:
        wins, games = asyncio.run(evaluate(args.games))
        print(_line("v2 against v1", wins, games))


if __name__ == "__main__":
    main()
