-- Weng-Lin Plackett-Luce on every match, finishing placements, and the 'tables' format.
--
-- wl_* start at the defaults (mu 25, sigma 25/3): run `arena ratings rebuild` once to
-- replay completed matches into them. Placements of matches completed before this
-- migration come from their scores, which is exact for the heads-up matches every
-- earlier tournament played: a forfeit there already scored 0.

ALTER TABLE ratings
    ADD COLUMN wl_mu    DOUBLE PRECISION NOT NULL DEFAULT 25,
    ADD COLUMN wl_sigma DOUBLE PRECISION NOT NULL DEFAULT 8.333333333333334;

ALTER TABLE rating_history
    ADD COLUMN wl_mu_before    DOUBLE PRECISION,   -- NULL for pre-0005 rows
    ADD COLUMN wl_mu_after     DOUBLE PRECISION,
    ADD COLUMN wl_sigma_before DOUBLE PRECISION,
    ADD COLUMN wl_sigma_after  DOUBLE PRECISION;

ALTER TABLE matches ADD COLUMN placements JSONB;    -- per seat, 1 is best, ties allowed

UPDATE matches m
SET placements = (
    SELECT jsonb_agg(r.place ORDER BY r.seat)
    FROM (
        SELECT e.seat, rank() OVER (ORDER BY e.score::float DESC) AS place
        FROM jsonb_array_elements_text(m.scores) WITH ORDINALITY AS e(score, seat)
    ) r
)
WHERE m.scores IS NOT NULL;

ALTER TABLE tournaments
    DROP CONSTRAINT tournaments_format_check,
    ADD CONSTRAINT tournaments_format_check
        CHECK (format IN ('round_robin', 'swiss', 'tables'));
