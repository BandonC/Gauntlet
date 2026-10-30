import json

from arena import cli
from arena.cli import _build_player
from arena.costs import FREE
from arena.players.bots import LiarsDiceBot
from arena.providers.base import Completion, CompletionRequest
from arena.providers.mock import MockProvider, RecordingProvider, load_recording
from arena.roster import PlayerConfig, PlayerFactory, ProviderLimits, Roster
from arena.types import ActionSpec, Usage

TOOL = ActionSpec("t", "d", {"type": "object", "properties": {}})


async def test_recording_round_trips_through_load_recording(tmp_path):
    path = tmp_path / "rec.jsonl"
    replies = [
        Completion({"action": "bid", "quantity": 3, "face": 4}, "", Usage(100, 12, 50, 0)),
        Completion(None, "I refuse to bid", Usage(90, 8)),
    ]
    rec = RecordingProvider(MockProvider(list(replies), name="anthropic"), path)
    for _ in replies:
        await rec.complete(CompletionRequest("m", "sys", "user", TOOL))
    replay = load_recording(path)
    assert [await replay.complete(CompletionRequest("m", "s", "u", TOOL)) for _ in replies] == (
        replies
    )


async def test_recorded_lines_say_which_model_produced_them(tmp_path):
    path = tmp_path / "rec.jsonl"
    replies = [
        Completion({"column": 3}, "", Usage(10, 2)),
        Completion({"column": 4}, "", Usage(9, 2)),
    ]
    rec = RecordingProvider(MockProvider(list(replies), name="anthropic"), path)
    await rec.complete(CompletionRequest("claude-a", "s", "u", TOOL))
    await rec.complete(CompletionRequest("claude-b", "s", "u", TOOL))
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["model"] for line in lines] == ["claude-a", "claude-b"]


def test_load_recording_can_take_one_models_lines(tmp_path):
    path = tmp_path / "rec.jsonl"
    usage = Usage(1, 2).__dict__
    rows = [("a", 1), ("b", 2), ("a", 3)]
    path.write_text(
        "".join(
            json.dumps({"model": m, "tool_input": {"column": c}, "raw_text": "", "usage": usage})
            + "\n"
            for m, c in rows
        )
    )
    script = load_recording(path, model="a")._script
    assert [c.tool_input["column"] for c in script] == [1, 3]
    assert len(load_recording(path)._script) == 3


def test_recordings_made_before_the_model_field_still_load(tmp_path):
    path = tmp_path / "old.jsonl"
    usage = Usage(1, 2).__dict__
    path.write_text(json.dumps({"tool_input": {}, "raw_text": "", "usage": usage}) + "\n")
    assert len(load_recording(path)._script) == 1


def test_worker_record_flag_reaches_the_factory_it_runs_with(tmp_path, monkeypatch):
    seen = {}

    class Pool:
        async def close(self):
            pass

    async def create_pool(*a, **k):
        return Pool()

    async def seed_rate_limits(pool, limits):
        return []

    async def run_worker(pool, factory, cfg, **kwargs):
        seen["factory"] = factory

    monkeypatch.setattr("arena.store.db.create_pool", create_pool)
    monkeypatch.setattr("arena.store.ratelimit.seed_rate_limits", seed_rate_limits)
    monkeypatch.setattr("arena.store.ratelimit.postgres_buckets", lambda pool, vendors: {"v": 1})
    monkeypatch.setattr("arena.worker.run_worker", run_worker)
    monkeypatch.setattr("arena.worker.install_stop_signals", lambda stop: lambda: None)

    def run(*flags):
        cli.main(["worker", "--exit-when-idle", *flags])
        return seen["factory"]

    recorded = run("--record", str(tmp_path / "rec"))
    assert recorded._record_dir == tmp_path / "rec"
    assert recorded._token_sources == {"v": 1}  # shared rate limits survive the swap
    assert run()._record_dir is None


def test_factory_with_record_dir_wraps_each_vendor_adapter(tmp_path):
    cfg = PlayerConfig("h", "anthropic", model="claude-haiku-5-5", prompt="liars_dice/v1")
    roster = Roster({"h": cfg}, {"anthropic": ProviderLimits(60, 2)})
    factory = PlayerFactory(roster, {"anthropic": "test-key"}).with_record_dir(tmp_path / "rec")
    factory.build("h")  # builds the adapter; makes no request
    inner = factory._providers["anthropic"]._inner  # RetryingProvider -> RecordingProvider
    assert isinstance(inner, RecordingProvider)
    assert inner._path.parent == tmp_path / "rec" and inner._path.name.startswith("anthropic-")

    plain = PlayerFactory(roster, {"anthropic": "test-key"})
    plain.build("h")
    assert not isinstance(plain._providers["anthropic"]._inner, RecordingProvider)


def test_play_builds_the_liars_dice_bot_without_a_roster_entry():
    player, pricing = _build_player(None, "liars-dice-bot")
    assert isinstance(player, LiarsDiceBot) and pricing is FREE
