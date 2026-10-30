-- Per-match game options (Werewolf: the scheduler's balanced roles), and a flag on turns
-- whose action the engine shortened (Werewolf: messages over the limit).

ALTER TABLE matches ADD COLUMN options JSONB;       -- NULL = the game's defaults

ALTER TABLE turns ADD COLUMN truncated BOOLEAN NOT NULL DEFAULT false;
