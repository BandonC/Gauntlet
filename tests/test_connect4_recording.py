"""Regression fixture: one real Connect 4 game, replayed from recorded model output.

Recorded 2026-10-30, match 17 of the first real run (tournament 3):
  arena worker --concurrency 1 --record recordings
  models: claude-haiku-5-5 and claude-haiku-4-5-20251001, max_tokens 256, no sampling params
  prompt: connect4/v1
  seed:   943830141; Haiku 5.5 in seat 0 (moves first)
  result: Haiku 5.5 won in 23 turns; 27 calls, 4 of them rejected ("column 3 is full")

Both models share the fixture; each line's `model` says whose reply it is. The full
recording stays local (recordings/ is gitignored). Re-recording costs real tokens and needs
approval (see "Spending money" in CLAUDE.md). If the engine or prompt rendering changes on
purpose, update EXPECTED here instead of re-recording.
"""

from pathlib import Path

from arena.engine import InMemoryTurnStore, MatchRunner, MatchSpec
from arena.engine.match_runner import iter_states
from arena.games.connect4 import Connect4
from arena.players.llm import LLMPlayer
from arena.players.prompting import load_prompt
from arena.providers.mock import load_recording

FIXTURE = Path(__file__).parent / "fixtures" / "connect4_haiku55_vs_haiku45.jsonl"
G = Connect4()
MODELS = ["claude-haiku-5-5", "claude-haiku-4-5-20251001"]
SEATS = ["haiku-5.5/c4-v1", "haiku-4.5/c4-v1"]
SPEC = MatchSpec("fixture", "connect4", 943830141, SEATS)

# The column played on every turn of the original run, in order.
EXPECTED = [
    3,
    3,
    3,
    2,
    3,
    3,
    3,
    4,
    4,
    4,
    4,
    4,
    4,
    0,
    5,
    5,
    5,
    1,
    5,
    6,
    5,
    1,
    5,
]
# (turn_no, attempts rejected) for turns where the first choice was refused.
EXPECTED_REJECTIONS = [(8, 1), (9, 1), (10, 1), (14, 1)]


def players():
    providers = [load_recording(FIXTURE, model=m) for m in MODELS]
    prompt = load_prompt("connect4/v1")
    built = [
        LLMPlayer(pid, p, model, prompt, temperature=None, max_tokens=256)
        for pid, p, model in zip(SEATS, providers, MODELS, strict=True)
    ]
    return built, providers


async def test_replaying_the_recording_reproduces_the_original_game():
    built, providers = players()
    store = InMemoryTurnStore()
    out = await MatchRunner(G, built, store).run(SPEC)

    assert out.scores == [1.0, 0.0] and out.placements == [1, 2] and out.forfeits == []
    assert not any(p._script for p in providers)  # every recorded reply was used
    turns = await store.load_turns(SPEC.match_id)
    assert [t.action["column"] for t in turns] == EXPECTED
    assert [(t.turn_no, len(t.rejections)) for t in turns if t.rejections] == EXPECTED_REJECTIONS

    states = list(iter_states(G, SPEC, turns))  # re-validates every turn
    assert G.is_terminal(states[-1])


async def test_real_retries_keep_every_rejection():
    built, _ = players()
    out = await MatchRunner(G, built, InMemoryTurnStore()).run(SPEC)
    rejected = [r for t in out.turns for r in t.rejections]
    assert len(rejected) == 4
    assert {r["reason"] for r in rejected} == {"column 3 is full"}
    assert all(t.error == t.rejections[-1]["reason"] for t in out.turns if t.rejections)
    assert sum(t.attempts for t in out.turns) == 27
