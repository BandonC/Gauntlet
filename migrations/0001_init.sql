-- Phase 2 schema. Apply with: python -m arena.store.migrate

CREATE TABLE players (
    id              TEXT PRIMARY KEY,              -- roster id, e.g. 'haiku-5.5/c4-v1'
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    prompt_key      TEXT NOT NULL,                 -- e.g. 'connect4/v1'
    config          JSONB NOT NULL DEFAULT '{}',   -- temperature, max_tokens, prices
    config_hash     TEXT NOT NULL UNIQUE,          -- LLMPlayer.config_hash
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tournaments (
    id              BIGSERIAL PRIMARY KEY,
    game            TEXT NOT NULL,
    format          TEXT NOT NULL CHECK (format IN ('round_robin', 'swiss')),
    status          TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'paused', 'completed')),
    budget_usd      NUMERIC(10, 4),                -- NULL = no cap
    spent_usd       NUMERIC(10, 6) NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE matches (
    id               BIGSERIAL PRIMARY KEY,
    tournament_id    BIGINT REFERENCES tournaments(id),
    game             TEXT NOT NULL,
    seed             BIGINT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    lease_owner      TEXT,                         -- worker id holding the lease
    lease_expires_at TIMESTAMPTZ,
    available_at     TIMESTAMPTZ NOT NULL DEFAULT now(),  -- retry backoff
    run_attempts     INT NOT NULL DEFAULT 0,
    last_error       TEXT,
    scores           JSONB,                        -- per seat, set on completion
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at       TIMESTAMPTZ,
    completed_at     TIMESTAMPTZ
);

-- Claim scan: only rows a worker could take.
CREATE INDEX matches_claimable ON matches (available_at, id)
    WHERE status IN ('queued', 'running');

CREATE TABLE match_seats (
    match_id    BIGINT NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    seat        SMALLINT NOT NULL,
    player_id   TEXT NOT NULL REFERENCES players(id),
    PRIMARY KEY (match_id, seat)
);

-- Append-only event log. A match's state is ALWAYS rebuilt from these rows.
CREATE TABLE turns (
    match_id            BIGINT NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    turn_no             INT NOT NULL,
    seat                SMALLINT NOT NULL,
    action              JSONB,                     -- NULL when forfeited
    forfeited           BOOLEAN NOT NULL DEFAULT false,
    attempts            SMALLINT NOT NULL,
    raw_response        TEXT,
    error               TEXT,
    input_tokens        INT NOT NULL DEFAULT 0,
    output_tokens       INT NOT NULL DEFAULT 0,
    cache_read_tokens   INT NOT NULL DEFAULT 0,
    cache_write_tokens  INT NOT NULL DEFAULT 0,
    cost_usd            NUMERIC(10, 6) NOT NULL DEFAULT 0,
    latency_ms          INT NOT NULL DEFAULT 0,
    worker_id           TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (match_id, turn_no)               -- duplicate turn = conflict, never two
);

CREATE TABLE ratings (
    player_id    TEXT NOT NULL REFERENCES players(id),
    game         TEXT NOT NULL,
    rating       DOUBLE PRECISION NOT NULL DEFAULT 1500,
    games_played INT NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (player_id, game)
);

CREATE TABLE rating_history (
    id            BIGSERIAL PRIMARY KEY,
    player_id     TEXT NOT NULL REFERENCES players(id),
    game          TEXT NOT NULL,
    match_id      BIGINT NOT NULL REFERENCES matches(id),
    rating_before DOUBLE PRECISION NOT NULL,
    rating_after  DOUBLE PRECISION NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (player_id, match_id)                   -- rating a match twice is a bug
);
