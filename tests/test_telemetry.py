"""Traces nest match -> turn -> provider.call, and every metric moves when it should."""

import asyncio
import os
import random
import sys
from dataclasses import replace

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY

from arena.costs import FREE, Pricing
from arena.engine.match_runner import TurnRecord
from arena.players.base import Decision, TurnContext
from arena.players.bots import RandomPlayer
from arena.providers.base import Completion, CompletionRequest, ProviderError
from arena.providers.mock import MockProvider
from arena.providers.retry import RetryingProvider
from arena.roster import PlayerConfig
from arena.store.db import create_pool
from arena.store.tournaments import create_round_robin
from arena.store.turns import PostgresTurnStore
from arena.telemetry import TracedPlayer, configure_tracing
from arena.types import ActionSpec, Usage
from arena.worker import WorkerConfig, run_one

SPANS = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(SPANS))
trace.set_tracer_provider(_provider)

REQUEST = CompletionRequest("m", "s", "u", ActionSpec("t", "d", {}))
GOOD = Completion({"column": 3}, "", Usage(10, 2), "tool_use")
TRANSIENT = ProviderError("529", retryable=True)
PERMANENT = ProviderError("400", retryable=False)


async def no_sleep(_seconds: float) -> None:
    return None


def sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def retrying(script: list, name: str = "mock") -> RetryingProvider:
    return RetryingProvider(MockProvider(script, name), sleep=no_sleep, rng=random.Random(0))


@pytest.fixture(autouse=True)
def clear_spans():
    SPANS.clear()


async def test_a_retry_counts_as_an_error_and_a_retry_and_a_second_span():
    before = (
        sample("arena_provider_errors_total", provider="flaky", retryable="true"),
        sample("arena_provider_retries_total", provider="flaky"),
    )
    await retrying([TRANSIENT, GOOD], "flaky").complete(REQUEST)

    assert (
        sample("arena_provider_errors_total", provider="flaky", retryable="true") == before[0] + 1
    )
    assert sample("arena_provider_retries_total", provider="flaky") == before[1] + 1
    calls = SPANS.get_finished_spans()
    assert [s.name for s in calls] == ["provider.call", "provider.call"]
    assert [s.attributes["arena.call_attempt"] for s in calls] == [1, 2]
    assert not calls[0].status.is_ok
    assert calls[1].attributes["arena.input_tokens"] == 10


async def test_a_permanent_error_is_counted_but_never_retried():
    before = sample("arena_provider_retries_total", provider="fatal")
    with pytest.raises(ProviderError):
        await retrying([PERMANENT], "fatal").complete(REQUEST)
    assert sample("arena_provider_errors_total", provider="fatal", retryable="false") == 1
    assert sample("arena_provider_retries_total", provider="fatal") == before


async def test_tracing_never_changes_what_the_player_decided():
    decision = Decision({"column": 1}, raw="x")

    class Fixed:
        player_id = "p"

        async def act(self, ctx):
            return decision

    ctx = TurnContext("connect4", 0, "", {}, "", ActionSpec("t", "d", {}), None)
    before = sample("arena_turn_seconds_count", game="g")
    assert await TracedPlayer(Fixed(), "g").act(ctx) is decision
    assert sample("arena_turn_seconds_count", game="g") == before + 1


def test_no_endpoint_means_no_tracing_setup():
    assert configure_tracing(None)() is None


@pytest.mark.db
@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")
class TestWorker:
    @pytest.fixture
    async def pool(self):
        import asyncpg

        from arena.store.migrate import migrate

        dsn = os.environ["DATABASE_URL"]
        conn = await asyncpg.connect(dsn)
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await conn.close()
        await migrate(dsn)
        p = await create_pool(dsn, max_size=6)
        yield p
        await p.close()

    async def test_spans_nest_match_turn_provider_and_counters_move(self, pool):
        class Calling(RandomPlayer):
            """A random player that also makes the provider call an LLM would."""

            def __init__(self, player_id, seed, provider):
                super().__init__(player_id, seed)
                self._provider = provider

            async def act(self, ctx):
                await self._provider.complete(REQUEST)
                return replace(await super().act(ctx), usage=Usage(1000, 100))

        provider = retrying([GOOD] * 60)

        class Factory:
            def build(self, player_id):
                return Calling(player_id, 1, provider), Pricing(1.0, 1.0)

        players = [PlayerConfig("r1", "random", seed=1), PlayerConfig("r2", "random", seed=2)]
        await create_round_robin(pool, "connect4", players, games_per_pair=1, budget_usd=None)
        claimed = sample("arena_matches_claimed_total")
        completed = sample("arena_matches_completed_total")
        spend = sample("arena_spend_usd_total")

        cfg = WorkerConfig(lease_seconds=30, heartbeat_seconds=5)
        while await run_one(pool, Factory(), "A", cfg):
            pass

        played = await pool.fetchval("SELECT count(*) FROM matches WHERE status = 'completed'")
        assert played >= 1
        assert sample("arena_matches_claimed_total") == claimed + played
        assert sample("arena_matches_completed_total") == completed + played
        logged = await pool.fetchval("SELECT sum(cost_usd)::float FROM turns")
        assert logged > 0
        assert sample("arena_spend_usd_total") - spend == pytest.approx(logged)

        spans = SPANS.get_finished_spans()
        by_id = {s.context.span_id: s for s in spans}
        names = {s.name for s in spans}
        assert names == {"match", "turn", "provider.call"}
        assert [s for s in spans if s.name == "match"][0].parent is None
        for span in spans:
            if span.name == "turn":
                assert by_id[span.parent.span_id].name == "match"
            elif span.name == "provider.call":
                assert by_id[span.parent.span_id].name == "turn"

    async def test_a_setup_failure_counts_as_a_failed_match(self, pool):
        class Broken:
            def build(self, player_id):
                raise ValueError("no such player")

        players = [PlayerConfig("r1", "random", seed=1), PlayerConfig("r2", "random", seed=2)]
        await create_round_robin(pool, "connect4", players, games_per_pair=1, budget_usd=None)
        before = sample("arena_matches_failed_total")
        assert await run_one(pool, Broken(), "A", WorkerConfig())
        assert sample("arena_matches_failed_total") == before + 1

    async def test_fenced_turn_adds_no_spend(self, pool):
        players = [PlayerConfig("r1", "random", seed=1), PlayerConfig("r2", "random", seed=2)]
        await create_round_robin(pool, "connect4", players, games_per_pair=1, budget_usd=None)
        mid = await pool.fetchval("SELECT min(id) FROM matches")
        before = sample("arena_spend_usd_total")
        record = TurnRecord(0, 0, {"column": 3}, False, 1, None, None, Usage(), 0.5, 1)
        with pytest.raises(Exception, match="lease lost"):
            await PostgresTurnStore(pool, "nobody").append_turn(str(mid), record)
        assert sample("arena_spend_usd_total") == before


def test_free_pricing_is_still_the_default_for_bots():
    assert FREE.cost(Usage(1000, 1000)) == 0


def test_module_imports_are_lazy_for_the_sdk_exporter():
    """Importing the worker must not load the OTLP exporter: tracing is opt-in."""
    code = "import sys, arena.worker; sys.exit('opentelemetry.exporter' in ' '.join(sys.modules))"

    async def run():
        proc = await asyncio.create_subprocess_exec(sys.executable, "-c", code)
        return await proc.wait()

    assert asyncio.run(run()) == 0
