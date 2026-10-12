-- Punch-in: silence the take's own vocals stem for the line's span (fx keeps playing).
ALTER TABLE overdubs ADD COLUMN replace_voice INTEGER NOT NULL DEFAULT 0;
