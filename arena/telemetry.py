"""Traces and Prometheus metrics. Both are off the path of correctness: nothing here decides
anything, and with no exporter or scraper configured it all stays cheap and silent.

Traces: match -> turn -> provider.call. A `turn` span is one attempt at a turn, so a rejected
move shows as two of them. Off unless OTEL_EXPORTER_OTLP_ENDPOINT is set. The SDK is imported
only then, so workers that don't trace never load it.

Metrics live in each worker process, so `arena worker --metrics-port N` serves them on
/metrics. The API server plays no matches and has none to show.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from opentelemetry import trace
from prometheus_client import Counter, Histogram, start_http_server

from arena.players.base import Decision, Player, TurnContext

tracer = trace.get_tracer("arena")

MATCHES_CLAIMED = Counter("arena_matches_claimed", "Matches claimed, including re-claims")
MATCHES_COMPLETED = Counter("arena_matches_completed", "Matches completed and rated")
MATCHES_FAILED = Counter("arena_matches_failed", "Matches marked failed for good")
TURN_SECONDS = Histogram(
    "arena_turn_seconds",
    "Time a player took over one attempt at a turn",
    ["game"],
    buckets=(0.001, 0.01, 0.1, 0.5, 1, 2.5, 5, 10, 30, 60),
)
PROVIDER_ERRORS = Counter(
    "arena_provider_errors", "Failed provider calls, each attempt", ["provider", "retryable"]
)
PROVIDER_RETRIES = Counter("arena_provider_retries", "Provider calls retried", ["provider"])
SPEND_USD = Counter("arena_spend_usd", "USD added to tournaments by persisted turns")


class TracedPlayer:
    """Times every attempt, in a span and in arena_turn_seconds. The engine stays free of
    telemetry imports, so the worker wraps players instead."""

    def __init__(self, inner: Player, game: str):
        self.player_id = inner.player_id
        self._inner = inner
        self._game = game

    async def act(self, ctx: TurnContext) -> Decision:
        attributes = {
            "arena.game": self._game,
            "arena.player": self.player_id,
            "arena.seat": ctx.seat,
            "arena.attempt": ctx.attempt,
        }
        started = time.perf_counter()
        with tracer.start_as_current_span("turn", attributes=attributes):
            try:
                return await self._inner.act(ctx)
            finally:
                TURN_SECONDS.labels(self._game).observe(time.perf_counter() - started)


def configure_tracing(endpoint: str | None, service: str = "arena-worker") -> Callable[[], None]:
    """Export spans over OTLP/HTTP if `endpoint` is set. Returns a function that flushes
    what's buffered; call it on the way out."""
    if not endpoint:
        return lambda: None
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
    )
    trace.set_tracer_provider(provider)
    return provider.shutdown


def serve_metrics(port: int | None) -> None:
    if port is not None:
        start_http_server(port)
