-- Strip a take's own audio (vocals + fx stems) from assembly; music still plays.
ALTER TABLE takes ADD COLUMN audio_muted INTEGER NOT NULL DEFAULT 0;
