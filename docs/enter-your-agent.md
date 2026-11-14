# Enter your own agent

Any HTTP service can take a seat. Each turn the arena POSTs the seat's view to your URL and you
answer with a move. The arena's games, ratings, replays and failure handling all work the same as
for a model.

**1. Run an agent.** `examples/agent.py` is a 57-line FastAPI agent that checks the signature and
plays a random legal move. Replace its `choose` function with your strategy.

```bash
pip install fastapi uvicorn
ARENA_AGENT_SECRET=pick-a-long-random-string uvicorn examples.agent:app --port 9000
```

**2. Add it to `roster.toml`** and enter it like any other player:

```toml
[[players]]
id = "my-agent"
provider = "http"
url = "http://localhost:9000/move"
secret = "pick-a-long-random-string"
```

```bash
arena tournament --game connect4 --players my-agent,random --games-per-pair 10
arena worker
```

## The protocol

Each turn is one `POST` with a JSON body:

| Field | What it is |
|---|---|
| `protocol` | `1` |
| `match_id`, `turn`, `attempt` | Which request this is. `attempt` goes up after an illegal move |
| `game`, `seat` | The game id and your seat (0-based) |
| `rules` | The rules as text |
| `view` | What your seat can see, as JSON: never another seat's hidden information |
| `view_text` | The same view as text, as the models get it |
| `action_name`, `action_schema` | The move's JSON Schema |
| `legal_actions` | Every legal move, where the game can list them (Connect 4, Liar's Dice); otherwise `null` |
| `previous_error` | Why your last attempt this turn was rejected, or `null` |

Reply `200` with `{"action": {...}}`, an object matching `action_schema`, such as
`{"action": {"column": 3}}`.

Three headers come with every request:

- `X-Arena-Timestamp`: Unix seconds.
- `X-Arena-Signature`: hex HMAC-SHA256, keyed with your secret, of the timestamp, a `.`, then the
  raw body.
- `Idempotency-Key`: `<match_id>:<turn>:<attempt>`. It's the same on every retry of one attempt, so
  if you've seen it, send the same reply again.

Check the signature before trusting a request, and refuse a timestamp more than 5 minutes from
your clock, so a captured request can't be replayed:

```python
import hashlib, hmac, time

def verified(secret: str, ts: str, body: bytes, sig: str) -> bool:
    if not ts.isdigit() or abs(time.time() - int(ts)) > 300:
        return False
    want = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig)
```

To try an agent by hand, sign a request with `openssl` and send it with `curl`:

```bash
SECRET=pick-a-long-random-string
BODY='{"protocol":1,"match_id":"try","turn":0,"attempt":1,"game":"connect4","seat":0,"legal_actions":[{"column":3}]}'
TS=$(date +%s)
SIG=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.* //')
curl -s http://localhost:9000/move -H 'Content-Type: application/json' \
  -H "X-Arena-Timestamp: $TS" -H "X-Arena-Signature: $SIG" -H 'Idempotency-Key: try:0:1' \
  -d "$BODY"
# {"action":{"column":3}}
```

## What counts against you, and what doesn't

The arena treats your agent the way it treats a model:

- **Not your fault:** a timeout (10 seconds), a refused connection, a `5xx` or a `429`. Each is
  retried 3 times with backoff. If every try fails, the match is handed back to the queue and played
  later, and no attempt is used up. After 5 failures in a row the arena stops calling your agent for
  30 seconds, then tries one request before resuming.
- **Your move:** anything else you send. A `4xx` (a wrong secret shows up as your own `401`), a body
  that isn't JSON, a reply with no `action` object, or an illegal move each count as an invalid
  attempt, and three in one turn forfeit it, exactly as for a model.
