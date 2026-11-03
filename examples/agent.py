"""A minimal LLM Arena agent: verify the arena's signature, answer with a legal move.

    pip install fastapi uvicorn
    ARENA_AGENT_SECRET=... uvicorn examples.agent:app --port 9000

Then add it to roster.toml as `provider = "http"`, `url = "http://localhost:9000/move"`, and the
same `secret`. Replace `choose` with your strategy. The protocol is in arena/players/remote.py.
"""

import hashlib
import hmac
import json
import os
import random
import time
from collections import OrderedDict

from fastapi import FastAPI, HTTPException, Request

WINDOW_S = 300  # reject requests more than 5 minutes from our clock: replays


def verified(secret: str, ts: str | None, body: bytes, sig: str | None) -> bool:
    if not ts or not sig or not ts.isdigit() or abs(time.time() - int(ts)) > WINDOW_S:
        return False
    want = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig)


def choose(turn: dict) -> dict:
    """Your strategy goes here. This one plays a random legal move where the game lists them;
    games with free text (Werewolf, Split the Deal) need a real strategy built from `view`."""
    legal = turn.get("legal_actions") or [{}]
    return random.Random(f"{turn['match_id']}:{turn['turn']}").choice(legal)


def make_app(secret: str) -> FastAPI:
    app = FastAPI()
    replies: OrderedDict[str, dict] = OrderedDict()  # Idempotency-Key -> the reply we sent

    @app.post("/move")
    async def move(request: Request) -> dict:
        body = await request.body()
        h = request.headers
        if not verified(secret, h.get("x-arena-timestamp"), body, h.get("x-arena-signature")):
            raise HTTPException(401, "bad signature")
        key = h.get("idempotency-key", "")
        if key not in replies:  # a retry of the same attempt gets the same answer
            replies[key] = {"action": choose(json.loads(body))}
            while len(replies) > 10_000:
                replies.popitem(last=False)
        return replies[key]

    return app


app = make_app(os.environ.get("ARENA_AGENT_SECRET", "change-me"))
