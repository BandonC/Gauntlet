"""Live match view: a Server-Sent Events stream of one match's turns.

Polling, not LISTEN/NOTIFY: the turn log stays the only source of truth, and a viewer
costs one small query a second. The log is replayed only when new turns have arrived.

Each `turn` event carries the same turn dict as the JSON API plus its label and the
spectator frame after that turn, so the replay page can append to its data. An event's
id is the number of turns sent so far, so a reconnecting EventSource resumes from its
Last-Event-ID. `done` ends the stream once the match is completed or failed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import asyncpg

from arena.store import read
from arena.web import with_labels

FINAL_STATUSES = ("completed", "failed")
POLL_SECONDS = 1.0
KEEPALIVE_SECONDS = 15.0  # a comment line, so proxies don't drop an idle stream
KEEPALIVE = ": keepalive\n\n"


def format_event(event: str, event_id: int, data: dict) -> str:
    return f"id: {event_id}\nevent: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def resume_point(after: int, last_event_id: str | None) -> int:
    """Turns the client already has: the page's count, or further along after a
    reconnect. A malformed header counts for nothing."""
    try:
        seen = int(last_event_id) if last_event_id else 0
    except ValueError:
        seen = 0
    return max(after, seen, 0)


async def match_events(
    pool: asyncpg.Pool,
    match_id: int,
    after: int = 0,
    *,
    poll_seconds: float = POLL_SECONDS,
    keepalive_seconds: float = KEEPALIVE_SECONDS,
) -> AsyncIterator[str]:
    """Yield SSE messages for every turn past `after`, until the match ends. The status
    and turn count come from one statement, so a final status always sees the last turn."""
    idle = 0.0
    while True:
        progress = await read.match_progress(pool, match_id)
        if progress is None:
            return
        status, turns = progress
        if turns > after:
            detail = await read.match_detail(pool, match_id)
            assert detail is not None  # matches are never deleted while being watched
            labelled = with_labels(detail["turns"], detail["game"])
            for turn, frame in zip(labelled[after:], detail["frames"][after + 1 :], strict=True):
                after += 1
                yield format_event("turn", after, {"turn": turn, "frame": frame})
            idle = 0.0
        if status in FINAL_STATUSES:
            yield format_event("done", after, {"status": status})
            return
        await asyncio.sleep(poll_seconds)
        idle += poll_seconds
        if idle >= keepalive_seconds:
            idle = 0.0
            yield KEEPALIVE
