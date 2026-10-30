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
