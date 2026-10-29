-- Glicko-2 beside Elo (post-v1). `ratings.rating` stays Elo; glicko_* are the Glicko-2
-- rating, rating deviation and volatility. Rows that already exist start at the
-- defaults: run `arena ratings rebuild` once to replay completed matches into them.

ALTER TABLE ratings
    ADD COLUMN glicko_rating DOUBLE PRECISION NOT NULL DEFAULT 1500,
    ADD COLUMN glicko_rd     DOUBLE PRECISION NOT NULL DEFAULT 350,
    ADD COLUMN glicko_vol    DOUBLE PRECISION NOT NULL DEFAULT 0.06;

ALTER TABLE rating_history
    ADD COLUMN glicko_rating_before DOUBLE PRECISION,   -- NULL for pre-0003 rows
    ADD COLUMN glicko_rating_after  DOUBLE PRECISION,
    ADD COLUMN glicko_rd_before     DOUBLE PRECISION,
    ADD COLUMN glicko_rd_after      DOUBLE PRECISION;
