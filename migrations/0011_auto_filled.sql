-- The engine filled in a field the reply left out, which only happens when that field could hold
-- one value (Werewolf's `action`). Kept apart from `truncated` so the quirk stays countable.

ALTER TABLE turns ADD COLUMN auto_filled BOOLEAN NOT NULL DEFAULT false;
