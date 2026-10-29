# LLM Arena

Tournaments between LLM players across turn-based games, with Elo ratings, per-turn
token cost tracking, and replays.

Under the hood: an event-sourced turn log (matches resume on any worker after a crash),
a Postgres job queue with leases and fenced writes (`FOR UPDATE SKIP LOCKED`, no Redis),
per-provider rate limiting with retry-after-aware backoff, and tournament budget caps.

## Quick start

```bash
pip install -e ".[dev]"
docker compose up -d db
cp .env.example .env && cp roster.example.toml roster.toml

arena play connect4 --p1 random:1 --p2 random:2      # free, no setup needed
arena migrate
arena tournament --game connect4 --players random,haiku-5.5/c4-v1 --games-per-pair 10 --budget 2
arena worker --concurrency 4
arena serve                                          # http://localhost:8000
```

See [CLAUDE.md](CLAUDE.md) for architecture, invariants and the roadmap.
