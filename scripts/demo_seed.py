"""Queue the demo's bot tournament once. Safe to run on every `docker compose up`.

Does nothing if the database already holds a Liar's Dice tournament, so restarts and
`docker compose --profile demo up` run twice never pile up duplicates. Bots only: free.
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

from arena.cli import main as arena_main

GAME = "liars_dice"


async def _already_seeded() -> bool:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        n = await conn.fetchval("SELECT count(*) FROM tournaments WHERE game = $1", GAME)
    finally:
        await conn.close()
    return bool(n)


def main() -> int:
    if asyncio.run(_already_seeded()):
        print("demo_seed: a tournament already exists, nothing to do")
        return 0
    arena_main(
        [
            "tournament",
            "--game",
            GAME,
            "--players",
            "liars-dice-bot,liars-dice-bot-v2",
            "--games-per-pair",
            "20",
        ]
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
