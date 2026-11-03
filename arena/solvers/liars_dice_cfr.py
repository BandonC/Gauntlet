"""CFR+ for two-player Liar's Dice with perfect recall, and its exact exploitability.

A round starts from a public state, (dice counts, opener), with fresh dice and nothing private
carried over, so each round is solved as its own game. Its payoffs are the values of the rounds
that follow, solved first, bottom-up. Values are always P(seat 0 wins), the game's own payoff
(score 1 for the winner, 0 for the loser).

Inside a round, a public node is the sequence of bids so far. Bids must rise, so a sequence is
a set of bid indices and a node is that set as a bitmask: 2**nb nodes for nb possible bids. A
node's last bid is its highest bit, its parent is the node without that bit, and its depth (bids
made) says who moves. The search runs layer by layer over all nodes at a depth at once, and over
every roll a player can hold, so Python never visits a node on its own.

Exploitability is measured exactly in the same units: (BR_0 + BR_1 - 1) / 2, where BR_p is what
a best responder in seat p wins against the other seat's average strategy over the whole game,
best-responding in later rounds too. 0 means a Nash equilibrium.

Run: `python -m arena.solvers.liars_dice_cfr` (writes the policy file the bot reads).
"""

from __future__ import annotations

import argparse
import json
import math
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import combinations_with_replacement
from pathlib import Path

import numpy as np

POLICY_PATH = Path(__file__).resolve().parents[1] / "players" / "data" / "liars_dice_small_cfr.bin"
MEMORY_LIMIT = 1 << 30  # refuse to train a round whose estimate is over 1 GiB

State = tuple[int, int, int]  # (dice of seat 0, dice of seat 1, opener)


def rolls(dice: int, faces: int) -> tuple[list[tuple[int, ...]], np.ndarray]:
    """Every sorted roll of `dice` dice, and its probability."""
    out = list(combinations_with_replacement(range(1, faces + 1), dice))
    probs = []
    for r in out:
        n = math.factorial(dice)
        for f in set(r):
            n //= math.factorial(r.count(f))
        probs.append(n / faces**dice)
    return out, np.array(probs)


def bid_of(index: int, faces: int) -> tuple[int, int]:
    """Bid index -> (quantity, face). Index order is the raise order."""
    return index // faces + 1, index % faces + 1


def index_of(quantity: int, face: int, faces: int) -> int:
    return (quantity - 1) * faces + face - 1


def estimate_bytes(c0: int, c1: int, faces: int) -> int:
    """Peak memory for one round: regrets and strategy sums (N x K x A float64 each), reach and
    value arrays, and the per-layer temporaries (about eight N_layer x K x A arrays)."""
    nb = (c0 + c1) * faces
    n, a = 1 << nb, nb + 1
    k = max(len(rolls(c0, faces)[0]), len(rolls(c1, faces)[0]))
    widest = math.comb(nb, nb // 2)
    return 2 * n * k * a * 8 + 4 * n * k * 8 + n * a + 8 * widest * k * a * 8


@dataclass
class Round:
    """One round's game tree and its CFR+ state."""

    c0: int
    c1: int
    opener: int
    faces: int
    nb: int = field(init=False)

    def __post_init__(self) -> None:
        f = self.faces
        self.nb = nb = (self.c0 + self.c1) * f
        self.n = 1 << nb
        self.a = nb + 1  # bids 0..nb-1, then challenge
        self.rolls = [rolls(self.c0, f), rolls(self.c1, f)]
        self.nr = [len(self.rolls[0][0]), len(self.rolls[1][0])]
        self.k = max(self.nr)
        self.prior = [np.zeros(self.k), np.zeros(self.k)]
        for p in (0, 1):
            self.prior[p][: self.nr[p]] = self.rolls[p][1]
        nodes = np.arange(self.n, dtype=np.int64)
        depth = np.zeros(self.n, dtype=np.int64)
        last = np.full(self.n, -1, dtype=np.int64)
        for b in range(nb):
            has = (nodes >> b) & 1
            depth += has
            last = np.where(has == 1, b, last)
        self.depth, self.last = depth, last
        self.parent = np.where(last >= 0, nodes & ~(1 << np.maximum(last, 0)), -1)
        self.layers = [nodes[depth == d] for d in range(nb + 1)]
        legal = np.zeros((self.n, self.a), dtype=bool)
        legal[:, :nb] = np.arange(nb)[None, :] > last[:, None]
        legal[:, nb] = last >= 0
        self.legal = legal
        # truth[b][r0, r1]: does bid b hold?
        counts = [
            np.array([[r.count(face) for face in range(1, f + 1)] for r in self.rolls[p][0]])
            for p in (0, 1)
        ]
        self.truth = np.zeros((nb, self.nr[0], self.nr[1]), dtype=bool)
        for b in range(nb):
            q, face = bid_of(b, f)
            self.truth[b] = (counts[0][:, face - 1][:, None] + counts[1][:, face - 1][None, :]) >= q
        self.regret = np.zeros((self.n, self.k, self.a))
        self.strategy_sum = np.zeros((self.n, self.k, self.a))
        self.iterations = 0

    def mover(self, depth: int) -> int:
        return (self.opener + depth) % 2

    def next_state(self, loser: int) -> State:
        c = [self.c0, self.c1]
        c[loser] -= 1
        return (c[0], c[1], loser)

    def payoffs(self, value0: Callable[[State], float]) -> np.ndarray:
        """u0[challenger, b, r0, r1]: P(seat 0 wins) after a challenge of bid b."""
        u = np.zeros((2, self.nb, self.nr[0], self.nr[1]))
        for ch in (0, 1):
            if_true = value0(self.next_state(ch))  # the bid holds: the challenger loses a die
            if_false = value0(self.next_state(1 - ch))
            u[ch] = np.where(self.truth, if_true, if_false)
        return u

    # --- strategies ---------------------------------------------------------------------

    def current(self, nodes: np.ndarray) -> np.ndarray:
        """Regret matching at these nodes; uniform over legal moves where no regret is positive."""
        return _normalise(np.maximum(self.regret[nodes], 0.0), self.legal[nodes])

    def average(self, nodes: np.ndarray) -> np.ndarray:
        return _normalise(self.strategy_sum[nodes], self.legal[nodes])

    # --- passes -------------------------------------------------------------------------

    def reach(self, policy: Callable[[np.ndarray], np.ndarray]) -> list[np.ndarray]:
        """Both seats' reach at every node, chance included."""
        out = [np.zeros((self.n, self.k)), np.zeros((self.n, self.k))]
        out[0][0], out[1][0] = self.prior[0], self.prior[1]
        for d in range(self.nb):
            children = self.layers[d + 1]
            parents = self.parent[children]
            m = self.mover(d)
            sigma = policy(parents)  # (L, K, A)
            acts = self.last[children]
            out[m][children] = out[m][parents] * sigma[np.arange(len(children)), :, acts]
            out[1 - m][children] = out[1 - m][parents]
        return out

    def backward(
        self,
        p: int,
        u0: np.ndarray,
        reach: list[np.ndarray],
        mode: str,
        policy: Callable[[np.ndarray], np.ndarray] | None = None,
        weight: float = 0.0,
    ) -> np.ndarray:
        """Counterfactual values for seat p (per own roll) at every node, deepest layer first.
        mode "cfr" also updates p's regrets and strategy sums, "br" maximises at p's nodes, and
        "eval" follows `policy` at p's nodes too."""
        val = np.zeros((self.n, self.k))
        bits = 1 << np.arange(self.nb, dtype=np.int64)
        np_ = self.nr[p]
        opp_policy = self.current if mode == "cfr" else policy
        for d in range(self.nb, -1, -1):
            nodes = self.layers[d]
            m = self.mover(d)
            q = np.zeros((len(nodes), self.k, self.a))
            if d < self.nb:  # bids lead to children
                child = nodes[:, None] | bits[None, :]  # (L, nb); masked by legality below
                q[:, :, : self.nb] = np.transpose(val[child], (0, 2, 1))
            if d > 0:  # challenge the last bid
                u = u0[m, self.last[nodes]]  # (L, R0, R1)
                opp = reach[1 - p][nodes]
                if m != p:
                    # A challenge is a leaf, so the opponent's chance of choosing it has to be
                    # applied here; a bid's is carried by the child's reach.
                    opp = opp * opp_policy(nodes)[:, :, self.nb]  # type: ignore[misc]
                if p == 0:
                    q[:, : self.nr[0], self.nb] = np.einsum("lij,lj->li", u, opp[:, : self.nr[1]])
                else:
                    q[:, : self.nr[1], self.nb] = np.einsum(
                        "lij,li->lj", 1.0 - u, opp[:, : self.nr[0]]
                    )
            legal = self.legal[nodes][:, None, :]
            q = np.where(legal, q, 0.0)
            if m != p:
                val[nodes] = q.sum(axis=2)
                continue
            if mode == "br":
                best = np.where(legal, q, -np.inf).max(axis=2)
                val[nodes] = np.where(np.isfinite(best), best, 0.0)
                continue
            sigma = self.current(nodes) if mode == "cfr" else policy(nodes)  # type: ignore[misc]
            v = (sigma * q).sum(axis=2)
            val[nodes] = v
            if mode == "cfr":
                gain = np.where(legal, q - v[:, :, None], 0.0)
                gain[:, np_:, :] = 0.0
                self.regret[nodes] = np.maximum(self.regret[nodes] + gain, 0.0)
                own = reach[p][nodes][:, :, None]
                self.strategy_sum[nodes] += weight * own * sigma
        return val

    def iterate(self, u0: np.ndarray) -> None:
        """One CFR+ iteration: alternating updates, linear averaging."""
        self.iterations += 1
        for p in (0, 1):
            reach = self.reach(self.current)
            self.backward(p, u0, reach, "cfr", weight=float(self.iterations))

    def value(self, p: int, u0: np.ndarray, mode: str) -> float:
        """Seat p's expected payoff (P(p wins)) at the root: against the average strategy as a
        best responder ("br"), or with both seats on the average strategy ("eval")."""
        reach = self.reach(self.average)
        val = self.backward(p, u0, reach, mode, policy=self.average)
        return float(self.prior[p] @ val[0])


def _normalise(weights: np.ndarray, legal: np.ndarray) -> np.ndarray:
    legal3 = np.broadcast_to(legal[:, None, :], weights.shape)
    w = np.where(legal3, weights, 0.0)
    total = w.sum(axis=2, keepdims=True)
    uniform = legal3 / np.maximum(legal3.sum(axis=2, keepdims=True), 1)
    return np.where(total > 0, w / np.where(total > 0, total, 1.0), uniform)


@dataclass
class Solution:
    faces: int
    dice: int
    rounds: dict[State, Round]
    eq: dict[State, float]  # P(seat 0 wins) with both on the average strategy
    curve: list[tuple[int, float]]  # (iterations of the opening round, exploitability)

    def terminal(self, s: State) -> float | None:
        if s[0] == 0:
            return 0.0
        if s[1] == 0:
            return 1.0
        return None

    def best_response(self, p: int) -> float:
        """P(seat p wins) for a best responder in seat p over the whole game."""
        memo: dict[State, float] = {}

        def br0(s: State) -> float:  # in seat-0 terms
            t = self.terminal(s)
            if t is not None:
                return t
            if s not in memo:
                rnd = self.rounds[s]
                u0 = rnd.payoffs(br0)
                v = rnd.value(p, u0, "br")
                memo[s] = v if p == 0 else 1.0 - v
            return memo[s]

        root = (self.dice, self.dice, 0)
        return br0(root) if p == 0 else 1.0 - br0(root)

    def exploitability(self) -> float:
        return (self.best_response(0) + self.best_response(1) - 1.0) / 2.0


def reachable(dice: int) -> list[State]:
    """Every round state from the opening (dice, dice, seat 0), children before parents."""
    seen: dict[State, None] = {}

    def visit(s: State) -> None:
        if s[0] == 0 or s[1] == 0 or s in seen:
            return
        for loser in (0, 1):
            c = [s[0], s[1]]
            c[loser] -= 1
            visit((c[0], c[1], loser))
        seen[s] = None

    visit((dice, dice, 0))
    return list(seen)


def solve(
    dice: int,
    faces: int,
    iterations: int,
    sub_iterations: int,
    threshold: float,
    check_every: int = 50,
    log: Callable[[str], None] = lambda _: None,
) -> Solution:
    states = reachable(dice)
    for s in states:
        need = estimate_bytes(s[0], s[1], faces)
        if need > MEMORY_LIMIT:
            raise MemoryError(f"round {s} needs about {need / 2**20:.0f} MiB, over the 1 GiB limit")
    log(
        "memory estimate per round: "
        + ", ".join(f"{s}: {estimate_bytes(s[0], s[1], faces) / 2**20:.0f} MiB" for s in states)
    )
    sol = Solution(faces, dice, {}, {}, [])

    def eq0(s: State) -> float:
        t = sol.terminal(s)
        return t if t is not None else sol.eq[s]

    root = (dice, dice, 0)
    for s in states:
        rnd = Round(s[0], s[1], s[2], faces)
        sol.rounds[s] = rnd
        u0 = rnd.payoffs(eq0)
        budget = iterations if s == root else sub_iterations
        started = time.monotonic()
        for i in range(1, budget + 1):
            rnd.iterate(u0)
            if s == root and (i % check_every == 0 or i == budget):
                sol.eq[s] = rnd.value(0, u0, "eval")
                e = sol.exploitability()
                sol.curve.append((i, e))
                log(
                    f"  {i} iterations: exploitability {e:.6f} ({time.monotonic() - started:.0f} s)"
                )
                if e < threshold:
                    break
        sol.eq[s] = rnd.value(0, u0, "eval")
        log(f"round {s}: {rnd.iterations} iterations, P(seat 0 wins) {sol.eq[s]:.4f}")
    return sol


# --- the policy file the bot reads ----------------------------------------------------------


def export_policy(sol: Solution, path: Path) -> dict:
    """Average strategies as uint8 (probability x 255), dense per round: node, then the mover's
    roll, then action. A zlib-compressed JSON header plus the arrays."""
    header: dict = {"faces": sol.faces, "dice": sol.dice, "rounds": []}
    blobs = []
    offset = 0
    for s, rnd in sol.rounds.items():
        probs = rnd.average(np.arange(rnd.n))
        q = np.rint(probs * 255).astype(np.uint8)
        blob = q.tobytes()
        header["rounds"].append(
            {"state": list(s), "nodes": rnd.n, "k": rnd.k, "a": rnd.a, "offset": offset}
        )
        blobs.append(blob)
        offset += len(blob)
    head = json.dumps(header).encode()
    raw = len(head).to_bytes(4, "big") + head + b"".join(blobs)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zlib.compress(raw, 9))
    return header


def quantised(sol: Solution) -> Solution:
    """The solution as the bot plays it: probabilities rounded to 1/255 and renormalised."""
    for rnd in sol.rounds.values():
        probs = rnd.average(np.arange(rnd.n))
        q = np.rint(probs * 255)
        rnd.strategy_sum = q  # _normalise renormalises, and falls back to uniform on all-zero
    return sol


def main() -> None:
    parser = argparse.ArgumentParser(description="Train the liars_dice_small CFR+ policy.")
    parser.add_argument("--dice", type=int, default=2)
    parser.add_argument("--faces", type=int, default=4)
    parser.add_argument("--iterations", type=int, default=2000, help="opening round, at most")
    parser.add_argument("--sub-iterations", type=int, default=2000, help="later rounds")
    parser.add_argument("--threshold", type=float, default=0.001)
    parser.add_argument("--check-every", type=int, default=50)
    parser.add_argument("--out", type=Path, default=POLICY_PATH)
    parser.add_argument("--curve", type=Path, help="also write the exploitability curve as JSON")
    args = parser.parse_args()
    sol = solve(
        args.dice,
        args.faces,
        args.iterations,
        args.sub_iterations,
        args.threshold,
        args.check_every,
        log=lambda s: print(s, flush=True),
    )
    final = sol.exploitability()
    print(f"exploitability {final:.6f} (threshold {args.threshold})")
    export_policy(sol, args.out)
    bot = quantised(sol).exploitability()
    print(f"as stored for the bot (1/255 steps): exploitability {bot:.6f}")
    print(f"policy: {args.out} ({args.out.stat().st_size / 2**20:.2f} MiB)")
    if args.curve:
        args.curve.write_text(
            json.dumps(
                {
                    "curve": sol.curve,
                    "final": final,
                    "stored": bot,
                    "eq": {str(k): v for k, v in sol.eq.items()},
                },
                indent=1,
            )
        )


if __name__ == "__main__":
    main()
