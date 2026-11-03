"""Wolf detector: extraction from game states, and that scores never see their own match."""

import random

from arena.analysis.wolf_detector import (
    Said,
    VoteDay,
    analyse,
    auc,
    by_day,
    extract,
    mcnemar_p,
    out_of_fold_scores,
)
from arena.engine import InMemoryTurnStore, MatchRunner, MatchSpec
from arena.engine.match_runner import iter_states
from arena.games.werewolf import Death, Message, Vote, Werewolf, WWState
from arena.players.bots import WerewolfBot

ROLES = ("wolf", "villager", "seer", "wolf", "villager", "villager", "villager")


def test_extract_knows_who_was_alive_at_each_vote_and_whose_votes_count():
    state = WWState(
        seed=1,
        roles=ROLES,
        alive=(0, 2, 3, 5),
        queue=(),
        messages=(Message(1, 1, 0, "hello"), Message(2, 1, 4, "hi")),
        deaths=(
            Death(1, 6, "villager", "night"),
            Death(1, 1, "villager", "vote"),
            Death(2, 4, "villager", "night"),
        ),
        day_votes=(
            Vote(1, 0, 1),  # a wolf's vote: not the village's
            Vote(1, 2, 3),
            Vote(1, 1, 0),
            Vote(2, 2, 3),
            Vote(2, 5, 0),
        ),
    )
    said, days = extract("m", state)
    assert [(s.seat, s.wolf) for s in said] == [(0, True), (4, False)]
    d1, d2 = days
    assert d1.alive == (0, 1, 2, 3, 4, 5) and d1.wolves == (0, 3)  # seat 6 died on night 1
    assert sorted(d1.village_votes) == [0, 3]
    assert d2.alive == (0, 2, 3, 5) and sorted(d2.village_votes) == [0, 3]


def corpus(marker_per_match: bool, matches: int = 40, seed: int = 0) -> list[Said]:
    rng = random.Random(seed)
    words = "the a vote we should look at player think trust quiet loud".split()
    out = []
    for m in range(matches):
        marker = f"tok{m}" if marker_per_match else "honestly"
        for seat in range(7):
            wolf = seat in (0, 3)
            for day in (1, 2):
                text = " ".join(rng.choices(words, k=8))
                if wolf:
                    text += f" {marker}"
                out.append(Said(f"m{m}", day, seat, text, wolf))
    return out


def test_a_shared_wolf_habit_is_found():
    said = corpus(marker_per_match=False)
    assert auc([s.wolf for s in said], out_of_fold_scores(said)) > 0.95


def test_a_habit_seen_only_inside_one_match_cannot_leak_across_folds():
    # Each match's wolves share a word no other match uses. A model that had seen the test
    # match would score near 1; grouped folds keep it near chance.
    said = corpus(marker_per_match=True)
    assert 0.3 < auc([s.wolf for s in said], out_of_fold_scores(said)) < 0.7


def test_day_comparison_pairs_the_detector_with_the_village_plurality():
    said = [Said("m", 1, s, "x", s == 0) for s in range(3)]
    scores = [0.9, 0.1, 0.2]  # seat 0, a wolf, is the top suspect
    days = [VoteDay("m", 1, (0, 1, 2), (0,), (1, 1, 0))]  # the village mostly voted seat 1
    row = by_day(said, scores, days)[1]
    assert row["detector_hits"] == 1 and row["village_plurality_hits"] == 0
    assert row["only_detector"] == 1 and row["village_hits"] == 1 and row["village_votes"] == 3
    assert abs(row["chance_spectator"] - 1 / 3) < 1e-9 and abs(row["chance_villager"] - 0.5) < 1e-9


def test_mcnemar():
    assert mcnemar_p(0, 0) == 1.0
    assert abs(mcnemar_p(10, 0) - 2 / 2**10) < 1e-12
    assert mcnemar_p(5, 5) == 1.0


async def test_runs_end_to_end_on_bot_games():
    game = Werewolf()
    said, days = [], []
    for seed in range(6):
        players = [WerewolfBot(f"b{i}", seed * 10 + i) for i in range(7)]
        spec = MatchSpec(f"w{seed}", "werewolf", seed, [p.player_id for p in players])
        store = InMemoryTurnStore()
        await MatchRunner(game, players, store).run(spec)
        *_, final = iter_states(game, spec, await store.load_turns(spec.match_id))
        s, d = extract(spec.match_id, final)
        said += s
        days += d
    rep = analyse(said, days, folds=3)
    assert rep.matches == 6 and 0.0 <= rep.auc <= 1.0 and rep.by_day
