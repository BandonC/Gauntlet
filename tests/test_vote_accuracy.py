"""Per-night village vote accuracy, counted from real engine games played by bots."""

import pytest

from arena.analysis.vote_accuracy import NIGHTS_BEFORE_DAY_ONE, Tally, tally
from arena.engine import InMemoryTurnStore, MatchRunner, MatchSpec
from arena.engine.match_runner import iter_states
from arena.games.registry import get_game
from arena.games.werewolf import SEATS, WOLF
from arena.players.bots import AccuracyVoteBot


async def final_state(name: str, seed: int, p: float, focus: float):
    game = get_game(name)
    roles = list(game.initial_state(SEATS, seed).roles)
    players = [AccuracyVoteBot(f"b{s}", seed, roles, p, focus) for s in range(SEATS)]
    spec = MatchSpec(f"va{seed}", name, seed, [q.player_id for q in players])
    out = await MatchRunner(game, players, InMemoryTurnStore()).run(spec)
    *_, final = iter_states(game, spec, out.turns)
    return final


@pytest.mark.parametrize("name", sorted(NIGHTS_BEFORE_DAY_ONE))
async def test_perfect_voters_always_hit_and_the_first_vote_removes_a_wolf(name):
    total = Tally()
    for seed in range(5):
        final = await final_state(name, seed, 1.0, 1.0)
        total.add(tally(name, final, final.winner == "village"))
    assert total.games == 5 and sum(total.votes.values()) > 0
    assert total.hits == total.votes
    assert total.first_vote == {"wolf": 5}
    assert min(total.votes) == NIGHTS_BEFORE_DAY_ONE[name]  # day 1 sits after that many nights


async def test_counts_only_living_non_wolves_and_every_village_vote_once():
    final = await final_state("werewolf_dayfirst", 3, 0.5, 0.0)
    t = tally("werewolf_dayfirst", final, final.winner == "village")
    wolves = {s for s, r in enumerate(final.roles) if r == WOLF}
    expected = 0
    for v in final.day_votes:
        dead = {
            d.seat for d in final.deaths if d.day < v.day or (d.day == v.day and d.cause == "night")
        }
        expected += v.seat not in wolves and v.seat not in dead
    assert sum(t.votes.values()) == expected
    assert sum(t.hits.values()) <= expected
