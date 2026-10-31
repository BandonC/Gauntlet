-- What each reply was made of, one list per attempt of a turn, in order: block type, size in
-- characters, and the full text of text blocks. Thinking blocks are size only. Turns from before
-- this migration are empty. Not shown on match pages or in exports: text here can be a model's
-- private reasoning.

ALTER TABLE turns ADD COLUMN reply_blocks JSONB NOT NULL DEFAULT '[]';
