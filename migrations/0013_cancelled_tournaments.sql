-- A cancelled tournament keeps its data and its real spend, and is left out of ratings.
ALTER TABLE tournaments DROP CONSTRAINT tournaments_status_check;
ALTER TABLE tournaments ADD CONSTRAINT tournaments_status_check
    CHECK (status IN ('running', 'paused', 'completed', 'cancelled'));
