"""The example agent, and a tournament of remote agents that misbehave: slow (some calls time out),
flaky (some 503) and garbage (some non-JSON replies). The worker must finish every match with the
usual invariants, and only the garbage agent's own answers may cost it attempts or forfeits."""

import asyncio
import importlib.util
import json
import os
import pathlib
import random
import time

import httpx2
import pytest

from arena.bench import bench_dsn, drop_database, reset_database
from arena.chaos import check_invariants
from arena.players.remote import IDEMPOTENCY_HEADER, SIGNATURE_HEADER, TIMESTAMP_HEADER, sign
from arena.roster import PlayerConfig, PlayerFactory, Roster
from arena.store.db import create_pool
from arena.store.tournaments import create_round_robin
from arena.worker import WorkerConfig, run_worker

SECRET = "chaos-secret"
EXAMPLE = pathlib.Path(__file__).parent.parent / "examples" / "agent.py"


def example_app(secret: str = SECRET):
    spec = importlib.util.spec_from_file_location("example_agent", EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.make_app(secret)


def signed(body: bytes, secret: str = SECRET, ts: int | None = None, key: str = "m:0:1") -> dict:
    ts = int(time.time()) if ts is None else ts
    return {
        TIMESTAMP_HEADER: str(ts),
        SIGNATURE_HEADER: sign(secret, ts, body),
        IDEMPOTENCY_HEADER: key,
        "Content-Type": "application/json",
    }


async def test_example_agent_checks_signatures_and_repeats_itself_on_retries():
    client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=example_app()), base_url="http://a"
    )
    turn = {"match_id": "m", "turn": 0, "legal_actions": [{"column": c} for c in range(7)]}
    body = json.dumps(turn).encode()
    first = await client.post("/move", content=body, headers=signed(body))
    again = await client.post("/move", content=body, headers=signed(body))
    assert first.status_code == 200 and first.json() == again.json()
    assert first.json()["action"] in turn["legal_actions"]
    wrong = await client.post("/move", content=body, headers=signed(body, secret="other"))
    stale = await client.post(
        "/move", content=body, headers=signed(body, ts=int(time.time()) - 301)
    )
    assert wrong.status_code == 401 and stale.status_code == 401


class Faulty(httpx2.AsyncBaseTransport):
    """In front of the example agent: injects one kind of fault at a seeded rate."""

    def __init__(
        self, inner: httpx2.AsyncBaseTransport, kind: str, rate: float, seed: int, outage: int = 0
    ):
        self.inner, self.kind, self.rate = inner, kind, rate
        self.rng = random.Random(seed)
        self.faults = 0
        self.outage = outage  # the first N calls all fail: enough to use up a turn's retries

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if self.outage:
            self.outage -= 1
            self.faults += 1
            return httpx2.Response(503, request=request)
        if self.kind != "good" and self.rng.random() < self.rate:
            self.faults += 1
            if self.kind == "slow":
                await asyncio.sleep(0.01)
                raise httpx2.ReadTimeout("agent too slow", request=request)
            if self.kind == "flaky":
                return httpx2.Response(503, request=request)
            if self.kind == "garbage":
                return httpx2.Response(200, content=b"<html>oops</html>", request=request)
        elif self.kind == "slow":
            await asyncio.sleep(self.rng.uniform(0, 0.005))
        return await self.inner.handle_async_request(request)


class Router(httpx2.AsyncBaseTransport):
    def __init__(self, routes: dict[str, httpx2.AsyncBaseTransport]):
        self.routes = routes

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return await self.routes[request.url.host].handle_async_request(request)


@pytest.mark.db
async def test_misbehaving_agents_never_break_an_invariant():
    admin = os.environ["DATABASE_URL"]
    dsn = bench_dsn(admin)
    await reset_database(admin)
    app = httpx2.ASGITransport(app=example_app())
    kinds = {"good": 0.0, "slow": 0.15, "flaky": 0.25, "garbage": 0.3}
    faults = {k: Faulty(app, k, rate, seed=i) for i, (k, rate) in enumerate(kinds.items())}
    faults["flaky"].outage = 4  # 1 call + 3 retries: one turn fails and its match is re-queued
    players = {
        k: PlayerConfig(id=f"agent-{k}", provider="http", url=f"http://{k}/move", secret=SECRET)
        for k in kinds
    }
    factory = PlayerFactory(
        Roster({p.id: p for p in players.values()}, {}),
        {},
        http_client=httpx2.AsyncClient(transport=Router(faults)),
        remote_options={"backoff": 0.01},
    )
    pool = await create_pool(dsn, max_size=4)
    try:
        _, n = await create_round_robin(
            pool, "connect4", list(players.values()), games_per_pair=2, budget_usd=None, seed=0
        )
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            await run_worker(pool, factory, WorkerConfig(), concurrency=3, exit_when_idle=True)
            left = await pool.fetchval("SELECT count(*) FROM matches WHERE status <> 'completed'")
            if left == 0:
                break
            await asyncio.sleep(1)  # a re-queued match waits out its backoff
        assert await check_invariants(pool, n) == []
        assert all(f.faults > 0 for k, f in faults.items() if k != "good")  # every fault happened
        # The outage re-queued a match, which then finished on a later run attempt.
        assert await pool.fetchval("SELECT max(run_attempts) FROM matches") > 1
        rows = await pool.fetch(
            "SELECT s.player_id, sum(t.forfeited::int) forfeits, "
            "sum(jsonb_array_length(t.rejections)) rejected FROM turns t JOIN match_seats s "
            "ON s.match_id = t.match_id AND s.seat = t.seat GROUP BY 1"
        )
        by_player = {r["player_id"]: r for r in rows}
        for k in ("good", "slow", "flaky"):  # infrastructure faults never cost an attempt
            assert by_player[f"agent-{k}"]["rejected"] == 0
            assert by_player[f"agent-{k}"]["forfeits"] == 0
        assert by_player["agent-garbage"]["rejected"] > 0
    finally:
        await pool.close()
        await drop_database(admin)
