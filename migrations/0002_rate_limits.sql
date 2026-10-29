-- Cross-process requests-per-minute limiting: one token bucket per vendor, shared by
-- every worker process. Rows are seeded from roster.toml [providers] when a worker
-- starts (store/ratelimit.py: seed_rate_limits). Concurrency caps and retry-after
-- pauses stay in-process (providers/ratelimit.py).

CREATE TABLE rate_limits (
    vendor          TEXT PRIMARY KEY,                       -- 'anthropic', 'openai'
    tokens          DOUBLE PRECISION NOT NULL,              -- as of updated_at
    capacity        DOUBLE PRECISION NOT NULL CHECK (capacity >= 1),
    refill_per_sec  DOUBLE PRECISION NOT NULL CHECK (refill_per_sec > 0),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
