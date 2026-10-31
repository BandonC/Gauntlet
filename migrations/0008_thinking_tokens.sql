-- Output tokens a turn spent on reasoning, as the provider reported them. Already included in
-- output_tokens and cost_usd, so this is a breakdown: never add it to a cost. 0 means none
-- reported, which covers every turn from before this migration.

ALTER TABLE turns ADD COLUMN thinking_tokens INTEGER NOT NULL DEFAULT 0;
