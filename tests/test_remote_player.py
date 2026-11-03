"""Remote HTTP players against a mock transport: signing, retries, breaker, failure semantics."""

import json

import httpx2
import pytest

from arena.engine import InMemoryTurnStore, MatchRunner, MatchSpec
from arena.games.connect4 import Connect4
from arena.players.base import TurnContext
from arena.players.bots import RandomPlayer
from arena.players.remote import (
    IDEMPOTENCY_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    CircuitBreaker,
    RemotePlayer,
    sign,
    verify,
)
from arena.providers.base import ProviderError
from arena.roster import PlayerConfig, PlayerFactory, Roster, config_hash, load_roster

SECRET = "s3cret"
G = Connect4()


async def no_sleep(_: float) -> None:
    return None


def ctx(attempt: int = 1) -> TurnContext:
    s = G.initial_state(2, 1)
    view = G.player_view(s, 0)
    return TurnContext(
        "connect4", 0, G.rules_text(), view, G.render_view(view), G.action_spec(s, 0),
        G.legal_actions_hint(s, 0), attempt=attempt, match_id="m1", turn_no=4,
    )  # fmt: skip


def player(handler, **kw) -> RemotePlayer:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return RemotePlayer(
        "agent", "http://agent.test/move", SECRET, client=client, sleep=no_sleep, **kw
    )


def test_signatures_verify_and_reject_tampering_and_stale_requests():
    body = b'{"a":1}'
    sig = sign(SECRET, 1000, body)
    assert verify(SECRET, "1000", body, sig, now=1000)
    assert verify(SECRET, "1000", body, sig, now=1299)
    assert not verify(SECRET, "1000", body, sig, now=1301)  # outside the 5-minute window
    assert not verify(SECRET, "1000", body, sig, now=699)
    assert not verify(SECRET, "1000", b'{"a":2}', sig, now=1000)
    assert not verify("other", "1000", body, sig, now=1000)
    assert not verify(SECRET, None, body, sig) and not verify(SECRET, "x", body, sig)


async def test_a_signed_request_gets_the_agents_move():
    seen = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        body = req.content
        assert verify(SECRET, req.headers[TIMESTAMP_HEADER], body, req.headers[SIGNATURE_HEADER])
        seen.append((req.headers[IDEMPOTENCY_HEADER], json.loads(body)))
        return httpx2.Response(200, json={"action": {"column": 3}})

    d = await player(handler).act(ctx())
    assert d.action == {"column": 3}
    key, body = seen[0]
    assert key == "m1:4:1"
    assert body["game"] == "connect4" and body["turn"] == 4 and body["legal_actions"]
    assert body["action_schema"]["properties"]["column"]


async def test_server_errors_are_retried_under_one_idempotency_key():
    keys, codes = [], iter([503, 502, 200])

    def handler(req):
        keys.append(req.headers[IDEMPOTENCY_HEADER])
        code = next(codes)
        return httpx2.Response(code, json={"action": {"column": 1}} if code == 200 else {})

    d = await player(handler).act(ctx(attempt=2))
    assert d.action == {"column": 1} and keys == ["m1:4:2"] * 3


async def test_timeouts_become_a_retryable_infrastructure_error_never_a_bad_move():
    calls = []

    def handler(req):
        calls.append(1)
        raise httpx2.ReadTimeout("slow", request=req)

    with pytest.raises(ProviderError) as e:
        await player(handler, retries=2).act(ctx())
    assert e.value.retryable and len(calls) == 3


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(400, text="bad request"),
        httpx2.Response(200, text="not json"),
        httpx2.Response(200, json={"move": 3}),
        httpx2.Response(200, json={"action": 3}),
    ],
)
async def test_the_agents_own_bad_answers_are_invalid_attempts(response):
    calls = []

    def handler(req):
        calls.append(1)
        return response

    d = await player(handler).act(ctx())
    assert d.action is None and d.raw and len(calls) == 1  # not retried: it's the agent's answer


def test_breaker_opens_then_lets_one_trial_through():
    now = [0.0]
    b = CircuitBreaker(threshold=3, cooldown=10, clock=lambda: now[0])
    for _ in range(3):
        assert b.allow()
        b.failure()
    assert b.state == "open" and not b.allow() and b.retry_after() == 10
    now[0] = 10
    assert b.state == "half_open" and b.allow() and not b.allow()  # one trial only
    b.failure()
    assert b.state == "open"  # the trial failed: open again
    now[0] = 20
    assert b.allow()
    b.success()
    assert b.state == "closed" and b.failures == 0


async def test_an_open_breaker_fails_fast_without_calling_the_agent():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx2.Response(500)

    breaker = CircuitBreaker(threshold=2, cooldown=60)
    p = player(handler, breaker=breaker, retries=5)
    with pytest.raises(ProviderError, match="circuit open") as e:
        await p.act(ctx())
    assert len(calls) == 2 and e.value.retryable and e.value.retry_after > 0
    with pytest.raises(ProviderError, match="circuit open"):
        await p.act(ctx())
    assert len(calls) == 2


def spec(seed, seats):
    return MatchSpec(f"r{seed}", "connect4", seed, seats)


async def test_a_good_agent_plays_whole_games():
    def handler(req):
        legal = json.loads(req.content)["legal_actions"]
        return httpx2.Response(200, json={"action": legal[0]})

    out = await MatchRunner(G, [player(handler), RandomPlayer("rnd", 1)], InMemoryTurnStore()).run(
        spec(1, ["agent", "rnd"])
    )
    assert out.forfeits == [] and all(t.attempts == 1 for t in out.turns)


async def test_a_garbage_agent_forfeits_like_a_model_and_a_down_agent_never_does():
    garbage = player(lambda req: httpx2.Response(200, text="<html>"))
    out = await MatchRunner(G, [garbage, RandomPlayer("rnd", 1)], InMemoryTurnStore()).run(
        spec(2, ["agent", "rnd"])
    )
    assert out.forfeits == [0] and out.turns[0].attempts == 3

    store = InMemoryTurnStore()
    down = player(lambda req: httpx2.Response(503), retries=1)
    with pytest.raises(ProviderError):
        await MatchRunner(G, [down, RandomPlayer("rnd", 1)], store).run(spec(3, ["agent", "rnd"]))
    assert await store.load_turns("r3") == []  # nothing persisted, nothing forfeited


def test_roster_http_players(tmp_path):
    path = tmp_path / "roster.toml"
    path.write_text(
        '[[players]]\nid = "mine"\nprovider = "http"\nurl = "http://localhost:9000/move"\n'
        'secret = "abc"\n',
        encoding="utf-8",
    )
    cfg = load_roster(path).players["mine"]
    assert config_hash(cfg).startswith("http-") and "abc" not in config_hash(cfg)
    with pytest.raises(ValueError, match="needs url and secret"):
        PlayerConfig(id="x", provider="http", url="http://a")
    with pytest.raises(ValueError, match="http players only"):
        PlayerConfig(id="x", provider="bot", url="http://a")
    factory = PlayerFactory(Roster({"mine": cfg, "twin": cfg}, {}), {})
    a, pricing = factory.build("mine")
    b, _ = factory.build("twin")
    assert isinstance(a, RemotePlayer) and a.breaker is b.breaker and pricing.input_per_mtok == 0
