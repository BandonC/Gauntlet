-- Sharded spend: see arena/store/spend.py. Existing tournaments stay exact.
ALTER TABLE tournaments ADD COLUMN spend_mode TEXT NOT NULL DEFAULT 'exact'
    CHECK (spend_mode IN ('exact', 'sharded'));

CREATE TABLE tournament_spend_shards (
    tournament_id BIGINT NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    worker_id     TEXT NOT NULL,                  -- one worker slot
    spent_usd     NUMERIC(10, 6) NOT NULL DEFAULT 0,
    PRIMARY KEY (tournament_id, worker_id)
);
