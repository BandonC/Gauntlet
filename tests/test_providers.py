import pytest

from arena.engine import InMemoryTurnStore, MatchRunner, MatchSpec
from arena.games.connect4 import Connect4
from arena.players.llm import LLMPlayer
from arena.players.prompting import load_prompt
from arena.providers.base import Completion, ProviderError
from arena.providers.mock import MockProvider
from arena.providers.ratelimit import RateLimiter
from arena.providers.retry import RetryingProvider
from arena.types import Usage

G = Connect4()
PROMPT = load_prompt("connect4/v1")


def move(col: int) -> Completion:
    return Completion(
        {"column": col}, f'{{"column": {col}}}', Usage(input_tokens=500, output_tokens=20)
    )


async def test_llm_player_vs_llm_player_with_mock_provider():
    a = LLMPlayer("a", MockProvider([move(0)] * 4), "mock-model", PROMPT)
    b = LLMPlayer("b", MockProvider([move(1)] * 3), "mock-model", PROMPT)
    spec = MatchSpec("m", "connect4", 0, ["a", "b"])
    out = await MatchRunner(G, [a, b], InMemoryTurnStore()).run(spec)
    assert out.scores == [1.0, 0.0]
    assert out.usage_by_seat[0].input_tokens == 2000

    req = a.provider.requests[0]
    assert "Connect 4" in req.system and "drop_piece" in req.system
    assert "Legal columns" in req.user and req.tool.name == "drop_piece"


async def test_model_without_tool_call_counts_as_invalid_attempt():
    no_tool = Completion(None, "I think column 3 is best!", Usage(output_tokens=10))
    a = LLMPlayer("a", MockProvider([no_tool, move(0), move(0), move(0), move(0)]), "m", PROMPT)
    b = LLMPlayer("b", MockProvider([move(1)] * 3), "m", PROMPT)
    out = await MatchRunner(G, [a, b], InMemoryTurnStore()).run(
        MatchSpec("m", "connect4", 0, ["a", "b"])
    )
    assert out.turns[0].attempts == 2
    assert "Your previous move was rejected" in a.provider.requests[1].user


async def test_prompt_hash_changes_player_identity():
    p1 = LLMPlayer("x", MockProvider([]), "m", PROMPT)
    p2 = LLMPlayer("x", MockProvider([]), "m", PROMPT, temperature=0.7)
    assert p1.config_hash != p2.config_hash


async def test_retrying_provider_recovers_and_honours_retry_after():
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    inner = MockProvider(
        [
            ProviderError("overloaded", retryable=True),
            ProviderError("429", retryable=True, retry_after=5.0),
            move(3),
        ]
    )
    p = RetryingProvider(inner, max_attempts=4, sleep=fake_sleep)
    out = await p.complete(None)  # type: ignore[arg-type]
    assert out.tool_input == {"column": 3}
    assert len(sleeps) == 2 and sleeps[1] >= 5.0


async def test_retrying_provider_gives_up_on_non_retryable():
    p = RetryingProvider(MockProvider([ProviderError("bad request", retryable=False)]))
    with pytest.raises(ProviderError):
        await p.complete(None)  # type: ignore[arg-type]


async def test_provider_error_fails_match_instead_of_forfeiting():
    a = LLMPlayer("a", MockProvider([ProviderError("down", retryable=True)]), "m", PROMPT)
    b = LLMPlayer("b", MockProvider([]), "m", PROMPT)
    store = InMemoryTurnStore()
    with pytest.raises(ProviderError):
        await MatchRunner(G, [a, b], store).run(MatchSpec("m", "connect4", 0, ["a", "b"]))
    assert store.turns.get("m", []) == []  # nothing recorded, nobody forfeited


async def test_rate_limiter_spaces_requests():
    t = [0.0]

    async def fake_sleep(s):
        t[0] += s

    limiter = RateLimiter(requests_per_minute=60, burst=1, clock=lambda: t[0], sleep=fake_sleep)
    for _ in range(3):
        async with limiter.slot():
            pass
    assert t[0] == pytest.approx(2.0)  # 1 req/s, first one free


async def test_token_source_replaces_rpm_bucket_but_not_concurrency_or_penalty():
    t = [0.0]

    async def fake_sleep(s):
        t[0] += s

    class CountingSource:
        takes = 0

        async def take(self):
            self.takes += 1

    source = CountingSource()
    limiter = RateLimiter(
        requests_per_minute=60,
        max_concurrency=1,
        burst=1,
        token_source=source,
        clock=lambda: t[0],
        sleep=fake_sleep,
    )
    for _ in range(3):  # the in-process 1 req/s bucket would sleep 2s here
        async with limiter.slot():
            pass
    assert source.takes == 3 and t[0] == 0.0

    limiter.penalize(5.0)  # retry-after still pauses callers in this process
    async with limiter.slot():
        assert limiter._sem.locked()  # concurrency cap still held around the call
    assert t[0] == pytest.approx(5.0) and source.takes == 4
