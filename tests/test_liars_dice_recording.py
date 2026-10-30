"""Regression fixture: a real Liar's Dice match, replayed from recorded model output.

Recorded 2026-10-29 with:
  arena play liars_dice --p1 haiku-5.5/ld-v1 --p2 liars-dice-bot --seed 1 \
      --budget 0.10 --record recordings
  model:  claude-haiku-5-5 (Anthropic), max_tokens 256, no sampling params sent
  prompt: liars_dice/v1
  seed:   1; Haiku in seat 0 (opens), LiarsDiceBot in seat 1
  result: bot won in 23 turns; 11 Haiku calls, $0.00094; no rejected or malformed moves

Re-recording costs real tokens and needs approval (see "Spending money" in CLAUDE.md).
If the engine, the bot or the prompt rendering changes on purpose, update EXPECTED here
instead of re-recording.
"""

from pathlib import Path

from arena.engine import InMemoryTurnStore, MatchRunner, MatchSpec
from arena.engine.match_runner import iter_states
from arena.games.liars_dice import LiarsDice
from arena.players.bots import LiarsDiceBot
from arena.players.llm import LLMPlayer
from arena.players.prompting import load_prompt
from arena.providers.mock import load_recording

FIXTURE = Path(__file__).parent / "fixtures" / "liars_dice_haiku_v1.jsonl"
G = LiarsDice()
SEATS = ["haiku-5.5/ld-v1", "liars-dice-bot"]
SPEC = MatchSpec("fixture", "liars_dice", 1, SEATS)
H, BOT = 0, 1
C = {"action": "challenge"}


def b(q, f):
    return {"action": "bid", "quantity": q, "face": f}


# (seat, action) for every turn of the original run, in order.
EXPECTED = [
    (H, b(2, 4)), (BOT, C), (H, b(2, 3)), (BOT, C), (BOT, b(1, 4)), (H, b(4, 2)),
    (BOT, C), (BOT, b(1, 3)), (H, b(3, 3)), (BOT, b(4, 3)), (H, C), (H, b(2, 2)),
    (BOT, b(3, 2)), (H, C), (H, b(2, 3)), (BOT, C), (BOT, b(1, 6)), (H, C),
    (H, b(1, 5)), (BOT, C), (BOT, b(1, 1)), (H, b(2, 1)), (BOT, C),
]  # fmt: skip
# Turns where the original run rejected a recorded move first: (turn_no, last error).
# Empty: every one of Haiku's moves was legal on the first attempt.
EXPECTED_REJECTIONS: list[tuple[int, str]] = []


def haiku_from_recording():
    provider = load_recording(FIXTURE)
    player = LLMPlayer(
        SEATS[0],
        provider,
        "claude-haiku-5-5",
        load_prompt("liars_dice/v1"),
        temperature=None,
        max_tokens=256,
    )
    return player, provider


def test_every_recorded_move_parses_as_a_liars_dice_action():
    provider = load_recording(FIXTURE)
    moves = [c.tool_input for c in provider._script]
    assert len(moves) == sum(seat == H for seat, _ in EXPECTED) == 11
    for m in moves:
        assert isinstance(m, dict) and m.get("action") in ("bid", "challenge"), m
        if m["action"] == "bid":
            assert isinstance(m["quantity"], int) and isinstance(m["face"], int), m


async def test_replaying_the_recording_reproduces_the_original_match():
    haiku, provider = haiku_from_recording()
    store = InMemoryTurnStore()
    out = await MatchRunner(G, [haiku, LiarsDiceBot(SEATS[1])], store).run(SPEC)

    assert out.scores == [0.0, 1.0] and out.forfeits == []
    assert not provider._script  # every recorded completion was used, none missing
    turns = await store.load_turns(SPEC.match_id)
    assert [(t.seat, t.action) for t in turns] == EXPECTED
    assert [(t.turn_no, t.error) for t in turns if t.attempts > 1] == EXPECTED_REJECTIONS

    # Each recorded move validates against the state it was played in (and the replay of
    # the persisted log re-validates every turn, raising CorruptTurnLog otherwise).
    states = list(iter_states(G, SPEC, turns))
    for t, s in zip(turns, states, strict=False):
        assert G.validate_action(s, t.seat, t.action) is None
        if t.action["action"] == "bid":
            assert t.action["quantity"] <= sum(s.dice_counts)  # never an impossible bid
    assert G.is_terminal(states[-1])
