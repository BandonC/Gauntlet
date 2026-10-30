-- Swiss tournaments, plus pausing on permanent match failure (both formats).
--
-- A Swiss tournament queues one round at a time. The transaction that completes a
-- round's last match pairs and queues the next one, guarded by the tournament row lock
-- AND a conditional bump of current_round, so a round can never be created twice.
-- A match that fails permanently pauses its tournament (last_error says why) until
-- `arena tournament resume <id>` re-queues it.

ALTER TABLE tournaments
    ADD COLUMN rounds        INT,                        -- Swiss only; NULL for round robin
    ADD COLUMN current_round INT NOT NULL DEFAULT 0,     -- Swiss: latest round queued
    ADD COLUMN last_error    TEXT,                       -- why it is paused
    ADD CONSTRAINT swiss_has_rounds CHECK (format <> 'swiss' OR rounds >= 1);

ALTER TABLE matches ADD COLUMN round INT;               -- Swiss round, NULL for round robin

CREATE TABLE tournament_players (
    tournament_id BIGINT NOT NULL REFERENCES tournaments(id),
    player_id     TEXT NOT NULL REFERENCES players(id),
    PRIMARY KEY (tournament_id, player_id)
);

-- A bye is worth 2 points (winning both games of a pairing). Not a match: no ratings.
CREATE TABLE tournament_byes (
    tournament_id BIGINT NOT NULL REFERENCES tournaments(id),
    round         INT NOT NULL,
    player_id     TEXT NOT NULL REFERENCES players(id),
    PRIMARY KEY (tournament_id, round)
);
