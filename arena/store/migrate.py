"""Minimal forward-only migration runner: applies migrations/NNNN_*.sql in order, once.

Usage: python -m arena.store.migrate
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import asyncpg

from arena.config import settings

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


async def migrate(dsn: str) -> list[str]:
    conn = await asyncpg.connect(dsn)
    applied: list[str] = []
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text())
                await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", path.name)
            applied.append(path.name)
    finally:
        await conn.close()
    return applied


if __name__ == "__main__":
    names = asyncio.run(migrate(settings().database_url))
    print("applied:", ", ".join(names) if names else "nothing (up to date)")
