"""Setup checks in `worker._run_claimed` that need no database."""

from typing import Any

from arena import worker
from arena.costs import FREE
from arena.players.bots import RandomPlayer
from arena.store import queue
from arena.store.spend import SHARDED


class _Factory:
    def build(self, player_id: str) -> tuple[RandomPlayer, Any]:
        return RandomPlayer(player_id), FREE


async def test_sharded_budgeted_match_without_tournament_fails_setup_loudly(monkeypatch):
    # The budget guard needs a tournament id. Without one the match must fail as a setup
    # error, never run unguarded with its cap unenforced.
    released: list[tuple[int, str, int]] = []

    async def fake_release(pool, match_id, worker_id, error, *, max_attempts, backoff_seconds):
        released.append((match_id, error, max_attempts))
        return "failed"

    monkeypatch.setattr(queue, "release_for_retry", fake_release)
    claimed = queue.ClaimedMatch(
        id=7,
        tournament_id=None,
        game="connect4",
        seed=1,
        run_attempts=1,
        seats=["random:1", "random:2"],
        spend_mode=SHARDED,
        has_budget=True,
    )
    await worker._run_claimed(
        None,  # type: ignore[arg-type]  # setup fails before the pool is touched
        _Factory(),  # type: ignore[arg-type]
        "w1",
        worker.WorkerConfig(),
        claimed,
        None,
    )
    assert released == [(7, "setup: sharded budgeted match without tournament_id", 1)]
