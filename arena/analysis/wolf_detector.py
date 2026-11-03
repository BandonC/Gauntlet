"""Wolf detector: can the words alone tell a wolf from a villager?

Every discussion message from every finished Werewolf game becomes a row: its text, labelled by
whether the speaker was a wolf. TF-IDF over words and word pairs feeds a logistic regression, and
every score is out of fold, with folds grouped by match so no game is in both training and test.

Two questions, both answered from out-of-fold scores only:
- AUC: how well a message's score ranks wolves above everyone else (per message, and per seat
  per game with its messages averaged). The interval comes from resampling whole matches.
- Day by day: at each day's vote, the detector's top suspect among the living is the seat with the
  highest mean score over everything it has said up to that vote. Is that seat a wolf more often
  than the village's own votes land on one, and than chance?

The detector is a spectator: it may suspect any living seat, including a seat it would be voting
from. A villager can't suspect themself, so chance is computed both ways.

Run: `python -m arena.analysis.wolf_detector` (reads the database, writes nothing).
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from arena.games.werewolf import WOLF, WWState

WEREWOLF_GAMES = ("werewolf", "werewolf_dayfirst")


@dataclass(frozen=True)
class Said:
    match: str
    day: int
    seat: int
    text: str
    wolf: bool


@dataclass(frozen=True)
class VoteDay:
    match: str
    day: int
    alive: tuple[int, ...]  # at the vote
    wolves: tuple[int, ...]  # among the alive
    village_votes: tuple[int, ...]  # targets chosen by living non-wolves


def extract(match: str, state: WWState) -> tuple[list[Said], list[VoteDay]]:
    """Messages and per-day vote facts from one finished game's final state."""
    wolves = {s for s, r in enumerate(state.roles) if r == WOLF}
    said = [Said(match, m.day, m.seat, m.text, m.seat in wolves) for m in state.messages]
    days = sorted({v.day for v in state.day_votes})
    out = []
    for d in days:
        dead = {x.seat for x in state.deaths if x.day < d or (x.day == d and x.cause == "night")}
        alive = tuple(s for s in range(len(state.roles)) if s not in dead)
        village = tuple(
            v.target
            for v in state.day_votes
            if v.day == d and v.seat not in wolves and v.seat in alive
        )
        out.append(VoteDay(match, d, alive, tuple(s for s in alive if s in wolves), village))
    return said, out


@dataclass
class Report:
    messages: int
    matches: int
    wolf_share: float
    auc: float
    auc_interval: tuple[float, float]
    seat_auc: float
    by_day: dict[int, dict[str, float | int]]
    top_terms: list[tuple[str, float]]  # most wolf-like, most village-like last


def out_of_fold_scores(said: Sequence[Said], folds: int = 5, seed: int = 0) -> list[float]:
    """P(wolf) for each message from a model that never saw its match."""
    import numpy as np
    from sklearn.model_selection import GroupKFold

    texts = [s.text for s in said]
    y = np.array([s.wolf for s in said], dtype=int)
    groups = [s.match for s in said]
    scores = np.zeros(len(said))
    for train, test in GroupKFold(n_splits=folds).split(texts, y, groups):
        model = _model(seed)
        model[0].fit([texts[i] for i in train])
        x = model[0].transform([texts[i] for i in train])
        model[1].fit(x, y[train])
        scores[test] = model[1].predict_proba(model[0].transform([texts[i] for i in test]))[:, 1]
    return scores.tolist()


def _model(seed: int):
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression

    return (
        TfidfVectorizer(ngram_range=(1, 2), min_df=2, sublinear_tf=True, lowercase=True),
        LogisticRegression(max_iter=2000, class_weight="balanced", C=1.0, random_state=seed),
    )


def top_terms(said: Sequence[Said], k: int = 12) -> list[tuple[str, float]]:
    """Weights from a model fitted on everything: descriptive only, never used for scoring."""
    vec, clf = _model(0)
    x = vec.fit_transform([s.text for s in said])
    clf.fit(x, [s.wolf for s in said])
    names = vec.get_feature_names_out()
    order = clf.coef_[0].argsort()
    picks = list(order[::-1][:k]) + list(order[:k][::-1])
    return [(str(names[i]), float(clf.coef_[0][i])) for i in picks]


def auc(labels: Sequence[bool], scores: Sequence[float]) -> float:
    from sklearn.metrics import roc_auc_score

    return float(roc_auc_score(list(labels), list(scores)))


def match_bootstrap_auc(
    said: Sequence[Said], scores: Sequence[float], rounds: int = 1000, seed: int = 0
) -> tuple[float, float]:
    """95% interval for the message AUC, resampling whole matches."""
    by_match: dict[str, list[int]] = defaultdict(list)
    for i, s in enumerate(said):
        by_match[s.match].append(i)
    keys = sorted(by_match)
    rng = random.Random(seed)
    values = []
    for _ in range(rounds):
        idx = [i for k in rng.choices(keys, k=len(keys)) for i in by_match[k]]
        labels = [said[i].wolf for i in idx]
        if all(labels) or not any(labels):
            continue
        values.append(auc(labels, [scores[i] for i in idx]))
    values.sort()
    return values[int(0.025 * len(values))], values[int(0.975 * len(values)) - 1]


def seat_scores(said: Sequence[Said], scores: Sequence[float], upto: int | None = None):
    """Mean score per (match, seat), over messages on days up to `upto` (all if None)."""
    total: dict[tuple[str, int], list[float]] = defaultdict(list)
    for s, x in zip(said, scores, strict=True):
        if upto is None or s.day <= upto:
            total[s.match, s.seat].append(x)
    return {k: sum(v) / len(v) for k, v in total.items()}


def by_day(
    said: Sequence[Said], scores: Sequence[float], days: Iterable[VoteDay]
) -> dict[int, dict[str, float | int]]:
    """Per day: detector top-suspect hits, village votes on a wolf, and chance."""
    per_match: dict[str, list[int]] = defaultdict(list)
    for i, s in enumerate(said):
        per_match[s.match].append(i)
    out: dict[int, dict[str, float | int]] = {}
    for vd in days:
        idx = per_match.get(vd.match, [])
        sub = [said[i] for i in idx]
        sc = seat_scores(sub, [scores[i] for i in idx], upto=vd.day)
        known = {s: sc[vd.match, s] for s in vd.alive if (vd.match, s) in sc}
        row = out.setdefault(
            vd.day,
            {
                "tables": 0,
                "detector_hits": 0,
                "village_votes": 0,
                "village_hits": 0,
                "chance_spectator": 0.0,
                "chance_villager": 0.0,
            },
        )
        row["tables"] += 1
        hit = False
        if known:
            top = max(known, key=lambda s: (known[s], -s))
            hit = top in vd.wolves
            row["detector_hits"] += hit
        # The village's choice as a table: its most-voted target, if one stands alone.
        counts = Counter(vd.village_votes).most_common()
        plural = (
            counts[0][0] if counts and (len(counts) == 1 or counts[0][1] > counts[1][1]) else None
        )
        village_hit = plural is not None and plural in vd.wolves
        row["village_plurality_hits"] = row.get("village_plurality_hits", 0) + village_hit
        row["only_detector"] = row.get("only_detector", 0) + (hit and not village_hit)
        row["only_village"] = row.get("only_village", 0) + (village_hit and not hit)
        n, w = len(vd.alive), len(vd.wolves)
        row["chance_spectator"] += w / n
        row["chance_villager"] += w / (n - 1)  # a villager choosing among the others
        row["village_votes"] += len(vd.village_votes)
        row["village_hits"] += sum(t in vd.wolves for t in vd.village_votes)
    for row in out.values():
        row["chance_spectator"] = row["chance_spectator"] / row["tables"]
        row["chance_villager"] = row["chance_villager"] / row["tables"]
    return out


def mcnemar_p(only_a: int, only_b: int) -> float:
    """Exact two-sided McNemar test on the discordant pairs."""
    n = only_a + only_b
    if n == 0:
        return 1.0
    k = min(only_a, only_b)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def analyse(said: Sequence[Said], days: Sequence[VoteDay], folds: int = 5) -> Report:
    scores = out_of_fold_scores(said, folds)
    seats = seat_scores(said, scores)
    wolves = {(s.match, s.seat): s.wolf for s in said}
    keys = sorted(seats)
    return Report(
        messages=len(said),
        matches=len({s.match for s in said}),
        wolf_share=sum(s.wolf for s in said) / len(said),
        auc=auc([s.wolf for s in said], scores),
        auc_interval=match_bootstrap_auc(said, scores),
        seat_auc=auc([wolves[k] for k in keys], [seats[k] for k in keys]),
        by_day=by_day(said, scores, days),
        top_terms=top_terms(said),
    )


async def load(database_url: str) -> tuple[list[Said], list[VoteDay]]:
    from arena.engine.match_runner import MatchSpec, iter_states
    from arena.games.registry import get_game
    from arena.store.db import create_pool
    from arena.store.ratings import NOT_CANCELLED_SQL
    from arena.store.turns import PostgresTurnStore

    pool = await create_pool(database_url)
    try:
        store = PostgresTurnStore(pool, "wolf-detector")
        rows = await pool.fetch(
            "SELECT m.id, m.game, m.seed, m.options, array_agg(s.player_id ORDER BY s.seat) seats "
            "FROM matches m JOIN match_seats s ON s.match_id = m.id "
            f"WHERE m.game = ANY($1) AND m.status = 'completed' AND {NOT_CANCELLED_SQL} "
            "GROUP BY m.id ORDER BY m.id",
            list(WEREWOLF_GAMES),
        )
        said: list[Said] = []
        days: list[VoteDay] = []
        for r in rows:
            game = get_game(r["game"])
            spec = MatchSpec(str(r["id"]), r["game"], r["seed"], list(r["seats"]), r["options"])
            *_, final = iter_states(game, spec, await store.load_turns(str(r["id"])))
            s, d = extract(str(r["id"]), final)
            said += s
            days += d
        return said, days
    finally:
        await pool.close()


def main() -> None:
    from arena.config import settings
    from arena.web import wilson

    parser = argparse.ArgumentParser(description="Wolf detector over every Werewolf game.")
    parser.add_argument("--folds", type=int, default=5)
    args = parser.parse_args()
    said, days = asyncio.run(load(settings().database_url))
    rep = analyse(said, days, args.folds)
    lo, hi = rep.auc_interval
    print(
        f"{rep.messages} messages from {rep.matches} games, {rep.wolf_share:.1%} by wolves\n"
        f"message AUC {rep.auc:.3f} ({lo:.3f}-{hi:.3f}, matches resampled); "
        f"seat AUC {rep.seat_auc:.3f}"
    )
    for d, row in sorted(rep.by_day.items()):
        t, vv = int(row["tables"]), int(row["village_votes"])
        dl, dh = wilson(int(row["detector_hits"]), t)
        vl, vh = wilson(int(row["village_hits"]), vv) if vv else (0.0, 0.0)
        print(
            f"day {d}: detector top suspect a wolf {row['detector_hits']}/{t} "
            f"({row['detector_hits'] / t:.1%}, {dl:.1%}-{dh:.1%}); village votes on a wolf "
            f"{row['village_hits']}/{vv} ({row['village_hits'] / max(vv, 1):.1%}, "
            f"{vl:.1%}-{vh:.1%}); "
            f"chance {row['chance_spectator']:.1%} spectator, {row['chance_villager']:.1%} villager"
        )
        ph = int(row["village_plurality_hits"])
        pl, phi = wilson(ph, t)
        print(
            f"   same tables: village's most-voted seat a wolf {ph}/{t} ({ph / t:.1%}, "
            f"{pl:.1%}-{phi:.1%}); only the detector right {row['only_detector']}, only the "
            f"village {row['only_village']}, McNemar p = "
            f"{mcnemar_p(int(row['only_detector']), int(row['only_village'])):.4f}"
        )
    print("wolf-like:", ", ".join(f"{w} {c:+.2f}" for w, c in rep.top_terms[:12]))
    print("village-like:", ", ".join(f"{w} {c:+.2f}" for w, c in rep.top_terms[12:]))


if __name__ == "__main__":
    main()
