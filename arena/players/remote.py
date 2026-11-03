"""Remote HTTP players: bring your own agent.

Each turn the arena POSTs the seat's view to the agent's URL and expects `{"action": {...}}` back.

Protocol (version 1):
- Body: JSON with `protocol`, `match_id`, `turn`, `attempt`, `game`, `seat`, `rules`, `view`,
  `view_text`, `action_name`, `action_schema`, `legal_actions` (or null) and `previous_error`.
- Headers: `X-Arena-Timestamp` (Unix seconds), `X-Arena-Signature` (hex HMAC-SHA256 of
  `"<timestamp>." + body` under the shared secret), and `Idempotency-Key`
  (`<match_id>:<turn>:<attempt>`), the same on every retry of one attempt.
- The agent should reject a signature that doesn't verify, or a timestamp more than 5 minutes
  from its clock (`verify` does both), and may return a cached reply for a repeated key.

Failure semantics, which keep invariant 5 (infrastructure errors never forfeit a player):
- A timeout (10 s), a connection error, a 5xx or a 429 is the arena's problem, not the player's:
  retried with backoff, then raised as a retryable `ProviderError`, so the worker re-queues the
  match and nobody loses an attempt.
- Repeated failures open the agent's circuit breaker: calls fail fast for a cooldown, then one
  trial call decides whether it closes again. One breaker per agent URL, shared by the process.
- Any other reply is the agent's own answer: a 4xx, a body that isn't JSON, or JSON without an
  `action` object is an invalid attempt, and so is an action the engine rejects. Three of those
  forfeit, exactly like a model's.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable

import httpx2

from arena.players.base import Decision, TurnContext
from arena.providers.base import ProviderError

PROTOCOL = 1
TIMESTAMP_HEADER = "X-Arena-Timestamp"
SIGNATURE_HEADER = "X-Arena-Signature"
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAY_WINDOW_S = 300
TIMEOUT_S = 10.0
RAW_LIMIT = 2000  # characters of a reply kept in the turn log


def sign(secret: str, timestamp: int, body: bytes) -> str:
    message = str(timestamp).encode() + b"." + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify(
    secret: str,
    timestamp: str | None,
    body: bytes,
    signature: str | None,
    now: float | None = None,
    window: int = REPLAY_WINDOW_S,
) -> bool:
    """For agents: is this request from the arena, and recent?"""
    if not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((time.time() if now is None else now) - ts) > window:
        return False
    return hmac.compare_digest(sign(secret, ts, body), signature)


class CircuitBreaker:
    """Closed until `threshold` failures in a row, then open for `cooldown` seconds, then half
    open: one trial call is let through, and its result closes or reopens the circuit."""

    def __init__(
        self,
        threshold: int = 5,
        cooldown: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.threshold = threshold
        self.cooldown = cooldown
        self.clock = clock
        self.failures = 0
        self.opened_at: float | None = None
        self.trial = False

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if self.clock() - self.opened_at >= self.cooldown else "open"

    def allow(self) -> bool:
        s = self.state
        if s == "closed":
            return True
        if s == "half_open" and not self.trial:
            self.trial = True
            return True
        return False

    def retry_after(self) -> float:
        if self.opened_at is None:
            return 0.0
        return max(0.0, self.cooldown - (self.clock() - self.opened_at))

    def success(self) -> None:
        self.failures, self.opened_at, self.trial = 0, None, False

    def failure(self) -> None:
        self.failures += 1
        if self.trial or self.failures >= self.threshold:
            self.opened_at, self.trial = self.clock(), False


class RemotePlayer:
    def __init__(
        self,
        player_id: str,
        url: str,
        secret: str,
        *,
        client: httpx2.AsyncClient | None = None,
        breaker: CircuitBreaker | None = None,
        timeout: float = TIMEOUT_S,
        retries: int = 3,
        backoff: float = 0.5,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.player_id = player_id
        self.url = url
        self.secret = secret
        self.client = client or httpx2.AsyncClient()
        self.breaker = breaker or CircuitBreaker()
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.clock = clock
        self.sleep = sleep

    def body(self, ctx: TurnContext) -> bytes:
        payload = {
            "protocol": PROTOCOL,
            "match_id": ctx.match_id,
            "turn": ctx.turn_no,
            "attempt": ctx.attempt,
            "game": ctx.game_name,
            "seat": ctx.seat,
            "rules": ctx.rules,
            "view": ctx.view,
            "view_text": ctx.view_text,
            "action_name": ctx.action_spec.name,
            "action_schema": ctx.action_spec.input_schema,
            "legal_actions": ctx.legal_actions_hint,
            "previous_error": ctx.previous_error,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    async def act(self, ctx: TurnContext) -> Decision:
        body = self.body(ctx)
        key = f"{ctx.match_id}:{ctx.turn_no}:{ctx.attempt}"
        last = "no attempt made"
        started = time.monotonic()
        for i in range(self.retries + 1):
            if i:
                await self.sleep(self.backoff * 2 ** (i - 1))
            if not self.breaker.allow():
                raise ProviderError(
                    f"{self.player_id}: circuit open after repeated failures ({last})",
                    retryable=True,
                    retry_after=self.breaker.retry_after(),
                )
            ts = int(self.clock())
            headers = {
                "Content-Type": "application/json",
                TIMESTAMP_HEADER: str(ts),
                SIGNATURE_HEADER: sign(self.secret, ts, body),
                IDEMPOTENCY_HEADER: key,
            }
            try:
                resp = await self.client.post(
                    self.url, content=body, headers=headers, timeout=self.timeout
                )
            except (httpx2.TimeoutException, httpx2.TransportError) as e:
                self.breaker.failure()
                last = f"{type(e).__name__}: {e}"
                continue
            if resp.status_code >= 500 or resp.status_code == 429:
                self.breaker.failure()
                last = f"HTTP {resp.status_code}"
                continue
            self.breaker.success()
            latency = int((time.monotonic() - started) * 1000)
            return _decision(resp, latency)
        raise ProviderError(
            f"{self.player_id}: agent failed {self.retries + 1} times ({last})", retryable=True
        )


def _decision(resp: httpx2.Response, latency_ms: int) -> Decision:
    """The agent answered: whatever it said is its move, usable or not."""
    text = resp.text[:RAW_LIMIT]
    if resp.status_code != 200:
        return Decision(action=None, raw=f"HTTP {resp.status_code}: {text}", latency_ms=latency_ms)
    try:
        data = json.loads(resp.text)
    except ValueError:
        return Decision(action=None, raw=text, latency_ms=latency_ms)
    action = data.get("action") if isinstance(data, dict) else None
    return Decision(
        action=action if isinstance(action, dict) else None, raw=text, latency_ms=latency_ms
    )
