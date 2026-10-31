"""Roster options for models that reject forced tool use, and thinking accounting."""

import asyncio
import contextlib
import hashlib
import json

import pytest

from arena.costs import Pricing
from arena.engine import InMemoryTurnStore, MatchRunner, MatchSpec
from arena.engine.match_runner import MatchInterrupted
from arena.games.connect4 import Connect4
from arena.players.base import TurnContext
from arena.players.bots import RandomPlayer
from arena.players.llm import LLMPlayer, identity_hash
from arena.players.prompting import load_prompt
from arena.providers.base import Completion, CompletionRequest
from arena.providers.mock import MockProvider, RecordingProvider, load_recording
from arena.roster import PlayerConfig, config_hash
from arena.types import ActionSpec, Usage

PROMPT = load_prompt("liars_dice/v1")
BASE = {"id": "p", "provider": "anthropic", "model": "m", "prompt": "liars_dice/v1"}
REQ = CompletionRequest("m", "s", "u", ActionSpec("t", "d", {"type": "object"}))


def old_hash(temperature=0.0, max_tokens=256):
    """identity_hash as it was before these options, written out in full."""
    cfg = {
        "provider": "anthropic",
        "model": "m",
        "prompt": PROMPT.key,
        "prompt_hash": PROMPT.content_hash,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:16]


def test_a_default_player_keeps_the_identity_it_always_had():
    assert config_hash(PlayerConfig(**BASE)) == old_hash()
    assert identity_hash("anthropic", "m", PROMPT, 0.0, 256) == old_hash()


@pytest.mark.parametrize(
    "extra",
    [
        {"tool_choice": "auto"},
        {"tool_choice": "auto", "thinking_budget": 1024, "max_tokens": 2048},
        {"tool_choice": "auto", "thinking_budget": 1500, "max_tokens": 2048},
    ],
)
def test_each_setting_is_part_of_identity(extra):
    assert config_hash(PlayerConfig(**BASE, **extra)) != config_hash(
        PlayerConfig(**BASE, **{k: v for k, v in extra.items() if k == "max_tokens"})
    )


def test_two_budgets_are_two_players():
    a = PlayerConfig(**BASE, tool_choice="auto", thinking_budget=1024, max_tokens=2048)
    b = PlayerConfig(**BASE, tool_choice="auto", thinking_budget=1500, max_tokens=2048)
    assert config_hash(a) != config_hash(b)


def test_the_built_player_and_the_roster_agree_on_identity():
    cfg = PlayerConfig(**BASE, tool_choice="auto", thinking_budget=1024, max_tokens=2048)
    player = LLMPlayer(
        "p",
        MockProvider([], name="anthropic"),
        "m",
        PROMPT,
        max_tokens=2048,
        tool_choice="auto",
        thinking_budget=1024,
    )
    assert player.config_hash == config_hash(cfg)


@pytest.mark.parametrize(
    "extra",
    [
        {"tool_choice": "any"},
        {"tool_choice": "auto", "thinking_budget": 1023, "max_tokens": 2048},
        {"tool_choice": "auto", "thinking_budget": 2048, "max_tokens": 2048},
        {"thinking_budget": 1024, "max_tokens": 2048},  # forced can't think by budget
    ],
)
def test_the_roster_refuses_what_the_api_would(extra):
    with pytest.raises(ValueError, match="p:"):
        PlayerConfig(**BASE, **extra)


@pytest.mark.parametrize(
    "extra",
    [{"tool_choice": "auto"}, {"tool_choice": "auto", "thinking_budget": 1024, "max_tokens": 2048}],
)
def test_the_options_are_anthropic_only(extra):
    with pytest.raises(ValueError, match="Anthropic only"):
        PlayerConfig(id="p", provider="openai", model="m", prompt="liars_dice/v1", **extra)


async def test_a_player_sends_its_settings_with_every_request():
    provider = MockProvider([Completion({"column": 3}, "", Usage())], name="anthropic")
    player = LLMPlayer(
        "p", provider, "m", PROMPT, max_tokens=2048, tool_choice="auto", thinking_budget=1024
    )
    spec = ActionSpec("t", "d", {"type": "object"})
    ctx = TurnContext("Liar's Dice", 0, "rules", {}, "view", spec, None, 1, None)
    await player.act(ctx)
    sent = provider.requests[0]
    assert (sent.tool_choice, sent.thinking_budget) == ("auto", 1024)


def test_thinking_tokens_add_up():
    total = Usage(10, 20, 0, 0, 15) + Usage(1, 2, 0, 0, 1)
    assert (total.output_tokens, total.thinking_tokens) == (22, 16)


def test_thinking_is_billed_once_as_output():
    pricing = Pricing(input_per_mtok=2.0, output_per_mtok=10.0)
    with_thinking = Usage(output_tokens=1000, thinking_tokens=900)
    assert pricing.cost(with_thinking) == pricing.cost(Usage(output_tokens=1000)) == 0.01


async def test_a_text_reply_under_auto_is_an_invalid_attempt_and_its_thinking_still_counts():
    text = Completion(None, "column 3", Usage(10, 300, 0, 0, 280))
    move = Completion({"column": 3}, "", Usage(10, 20, 0, 0, 5))
    player = LLMPlayer(
        "p",
        MockProvider([text, move], name="anthropic"),
        "m",
        load_prompt("connect4/v1"),
        tool_choice="auto",
    )
    store = InMemoryTurnStore()
    # Stop after the first turn: only the LLM's two attempts are scripted.
    stop = asyncio.Event()

    async def after_first_turn(*_):
        stop.set()

    runner = MatchRunner(
        Connect4(), [player, RandomPlayer("r", 1)], store, on_turn=after_first_turn
    )
    with pytest.raises(MatchInterrupted):
        await runner.run(MatchSpec("1", "connect4", 1, ["llm", "r"]), stop)
    first = store.turns["1"][0]
    assert first.attempts == 2 and not first.forfeited
    assert first.error == "no move was returned; call the tool with a legal move"
    assert (first.usage.output_tokens, first.usage.thinking_tokens) == (320, 285)


async def test_recordings_keep_thinking_tokens_and_old_ones_still_load(tmp_path):
    path = tmp_path / "r.jsonl"
    done = Completion({"column": 3}, "", Usage(1, 2, 0, 0, 1))
    await RecordingProvider(MockProvider([done]), path).complete(REQ)
    with path.open("a") as f:
        old = {"input_tokens": 1, "output_tokens": 2, "cache_read_tokens": 0}
        f.write(json.dumps({"tool_input": None, "raw_text": "", "usage": old}) + "\n")
    replay = load_recording(path)
    assert (await replay.complete(REQ)).usage.thinking_tokens == 1
    assert (await replay.complete(REQ)).usage.thinking_tokens == 0


@pytest.mark.parametrize(
    "extra",
    [
        {"thinking_mode": "between_tools", "tool_choice": "auto"},
        {"effort": "low"},
        {"effort": "high", "tool_choice": "auto"},
    ],
)
def test_a_new_setting_is_a_new_player_and_the_default_changes_nothing(extra):
    plain = {k: v for k, v in extra.items() if k == "tool_choice"}
    assert config_hash(PlayerConfig(**BASE, **extra)) != config_hash(PlayerConfig(**BASE, **plain))
    assert config_hash(PlayerConfig(**BASE, thinking_mode="default")) == old_hash()


def test_the_built_player_and_the_roster_agree_on_the_thinking_mode():
    cfg = PlayerConfig(**BASE, tool_choice="auto", thinking_mode="between_tools", effort="low")
    player = LLMPlayer(
        "p",
        MockProvider([], name="anthropic"),
        "m",
        PROMPT,
        tool_choice="auto",
        thinking_mode="between_tools",
        effort="low",
    )
    assert player.config_hash == config_hash(cfg)


@pytest.mark.parametrize("effort", ["xhigh", "max"])
def test_between_tools_is_refused_at_the_top_effort_levels(effort):
    with pytest.raises(ValueError, match="high or below"):
        PlayerConfig(**BASE, tool_choice="auto", thinking_mode="between_tools", effort=effort)


@pytest.mark.parametrize("effort", [None, "low", "medium", "high"])
def test_between_tools_is_allowed_at_high_effort_or_below(effort):
    PlayerConfig(**BASE, tool_choice="auto", thinking_mode="between_tools", effort=effort)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"thinking_mode": "between_tools"}, "needs tool_choice"),
        (
            {
                "thinking_mode": "between_tools",
                "tool_choice": "auto",
                "thinking_budget": 1024,
                "max_tokens": 2048,
            },
            "thinking_budget",
        ),
        ({"thinking_mode": "off"}, "thinking_mode must be"),
        ({"effort": "extreme"}, "effort must be"),
    ],
)
def test_the_roster_refuses_thinking_settings_that_cannot_work(extra, message):
    with pytest.raises(ValueError, match=message):
        PlayerConfig(**BASE, **extra)


@pytest.mark.parametrize(
    "extra", [{"thinking_mode": "between_tools", "tool_choice": "auto"}, {"effort": "low"}]
)
def test_the_new_settings_are_anthropic_only(extra):
    with pytest.raises(ValueError, match="Anthropic only"):
        PlayerConfig(id="p", provider="openai", model="m", prompt="liars_dice/v1", **extra)


async def test_a_player_sends_its_thinking_mode_and_effort():
    provider = MockProvider([Completion({"column": 3}, "", Usage())], name="anthropic")
    player = LLMPlayer(
        "p",
        provider,
        "m",
        PROMPT,
        tool_choice="auto",
        thinking_mode="between_tools",
        effort="low",
    )
    spec = ActionSpec("t", "d", {"type": "object"})
    await player.act(TurnContext("Liar's Dice", 0, "rules", {}, "view", spec, None, 1, None))
    sent = provider.requests[0]
    assert (sent.thinking_mode, sent.effort) == ("between_tools", "low")


async def first_turn(script, max_attempts=3):
    """Plays one turn of Connect 4 from a scripted LLM, and returns its record."""
    player = LLMPlayer("p", MockProvider(script, name="anthropic"), "m", load_prompt("connect4/v1"))
    store = InMemoryTurnStore()
    stop = asyncio.Event()

    async def after_first_turn(*_):
        stop.set()

    runner = MatchRunner(
        Connect4(),
        [player, RandomPlayer("r", 1)],
        store,
        on_turn=after_first_turn,
        max_attempts=max_attempts,
    )
    with contextlib.suppress(MatchInterrupted):  # a forfeit ends the game before the stop
        await runner.run(MatchSpec("1", "connect4", 1, ["llm", "r"]), stop)
    return store.turns["1"][0]


async def test_a_reply_cut_off_can_be_told_from_one_that_skipped_the_tool():
    cut = Completion(None, "", Usage(10, 1024, 0, 0, 1000), "max_tokens")
    skipped = Completion(None, "column 3", Usage(10, 5), "end_turn")
    move = Completion({"column": 3}, "", Usage(10, 20), "tool_use")
    turn = await first_turn([cut, skipped, move])
    assert turn.stop_reasons == ["max_tokens", "end_turn", "tool_use"]
    assert turn.attempts == 3 and not turn.forfeited


async def test_a_forfeit_keeps_the_stop_reason_of_every_attempt():
    cut = Completion(None, "", Usage(10, 1024), "max_tokens")
    turn = await first_turn([cut, cut, cut])
    assert turn.forfeited and turn.stop_reasons == ["max_tokens"] * 3


async def test_a_provider_that_reports_no_stop_reason_records_a_null():
    turn = await first_turn([Completion({"column": 3}, "", Usage())])
    assert turn.stop_reasons == [None]


async def test_recordings_keep_the_stop_reason_and_old_ones_load_without_it(tmp_path):
    path = tmp_path / "r.jsonl"
    done = Completion({"column": 3}, "", Usage(1, 2), "max_tokens")
    await RecordingProvider(MockProvider([done]), path).complete(REQ)
    with path.open("a") as f:
        f.write(json.dumps({"tool_input": None, "raw_text": "", "usage": {}}) + "\n")
    replay = load_recording(path)
    assert (await replay.complete(REQ)).stop_reason == "max_tokens"
    assert (await replay.complete(REQ)).stop_reason is None
