"""The demo compose profile. Tests never start Docker, so the compose file gets static checks,
and the seed script and turn pacing run for real (the seed on Postgres)."""

from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path

import asyncpg
import pytest

from arena.players.base import Decision, TurnContext
from arena.worker import PacedPlayer, WorkerConfig

ROOT = Path(__file__).resolve().parent.parent
DSN = os.environ.get("DATABASE_URL")


def test_demo_services_are_all_in_the_demo_profile():
    text = (ROOT / "docker-compose.yml").read_text()
    blocks = text.split("\n  demo-")[1:]
    assert len(blocks) == 5  # db, migrate, api, worker, seed (the anchor is in migrate)
    for block in blocks:
        assert "profiles: [demo]" in block.split("\n\n")[0] or "*demo-app" in block


def test_demo_worker_is_paced_so_matches_can_be_watched_live():
    worker = (ROOT / "docker-compose.yml").read_text().split("\n  demo-worker:")[1]
    assert '"--turn-delay"' in worker.split("\n\n")[0]


def test_dockerfile_installs_editable_so_migrations_are_found():
    assert "pip install --no-cache-dir -e ." in (ROOT / "Dockerfile").read_text()


def test_serve_accepts_a_host():
    assert '"--host"' in (ROOT / "arena" / "cli.py").read_text()


class _Fixed:
    player_id = "fixed"

    async def act(self, ctx: TurnContext) -> Decision:
        return Decision(action={"column": 3})


async def test_paced_player_waits_then_returns_the_inner_decision():
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    paced = PacedPlayer(_Fixed(), 1.5, sleep=fake_sleep)
    decision = await paced.act(None)  # type: ignore[arg-type]
    assert slept == [1.5]
    assert decision.action == {"column": 3}
    assert paced.player_id == "fixed"


def test_turn_delay_is_off_by_default():
    assert WorkerConfig().turn_delay_seconds == 0.0


def _load_seed():
    spec = importlib.util.spec_from_file_location("demo_seed", ROOT / "scripts" / "demo_seed.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _reset_and_migrate() -> None:
    from arena.store.migrate import migrate

    conn = await asyncpg.connect(DSN)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.close()
    await migrate(DSN)


async def _counts() -> tuple[int, int]:
    conn = await asyncpg.connect(DSN)
    try:
        return (
            await conn.fetchval("SELECT count(*) FROM tournaments"),
            await conn.fetchval("SELECT count(*) FROM matches"),
        )
    finally:
        await conn.close()


async def _drain_paced(delay: float) -> tuple[int, int]:
    from arena.roster import PlayerFactory, load_roster
    from arena.store.db import create_pool
    from arena.worker import run_worker

    pool = await create_pool(DSN, max_size=6)
    try:
        cfg = WorkerConfig(idle_sleep_seconds=0.05, turn_delay_seconds=delay)
        factory = PlayerFactory(load_roster(ROOT / "roster.example.toml"), {})
        await run_worker(pool, factory, cfg, concurrency=2, exit_when_idle=True)
        done = await pool.fetchval("SELECT count(*) FROM matches WHERE status = 'completed'")
        return done, await pool.fetchval("SELECT count(*) FROM turns")
    finally:
        await pool.close()


@pytest.mark.db
@pytest.mark.skipif(not DSN, reason="DATABASE_URL not set")
def test_seed_queues_once_and_paced_workers_finish_it(monkeypatch):
    asyncio.run(_reset_and_migrate())
    monkeypatch.setenv("ARENA_ROSTER_PATH", str(ROOT / "roster.example.toml"))
    seed = _load_seed()

    assert seed.main() == 0
    assert asyncio.run(_counts()) == (1, 20)
    assert seed.main() == 0  # a second `compose up` queues nothing more
    assert asyncio.run(_counts()) == (1, 20)

    waits: list[float] = []
    real_sleep = asyncio.sleep

    async def counting_sleep(seconds: float) -> None:
        waits.append(seconds)
        await real_sleep(seconds)

    monkeypatch.setattr(PacedPlayer, "__init__", _paced_init(counting_sleep))
    done, turns = asyncio.run(_drain_paced(0.001))
    assert done == 20
    # Bots never make an illegal move, so one attempt a turn: every turn waited first.
    assert len(waits) == turns > 0
    assert set(waits) == {0.001}


def _paced_init(sleep):
    original = PacedPlayer.__init__

    def init(self, inner, delay, sleep_=None):
        original(self, inner, delay, sleep)

    return init
