-- Every rejected attempt of a turn, in order: [{attempt, action, reason}, ...]. A turn that
-- succeeded on a retry used to lose these; `error` stays as the last reason. Rows from
-- before this migration are empty unless restored from a recording.

ALTER TABLE turns ADD COLUMN rejections JSONB NOT NULL DEFAULT '[]';
