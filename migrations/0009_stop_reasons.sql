-- Why the model stopped, one entry per attempt of a turn, in order, as the provider said it.
-- It tells a reply cut off at max_tokens from one that skipped the tool. A null entry means none
-- reported. Turns from before this migration are empty.

ALTER TABLE turns ADD COLUMN stop_reasons JSONB NOT NULL DEFAULT '[]';
