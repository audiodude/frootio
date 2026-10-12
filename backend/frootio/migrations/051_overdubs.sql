-- Post-fix dialogue lines: Chatterbox HD clips in a character's cloned voice, placed on a
-- take and mixed into its vocals in assembly. Removing one archives it (nothing is deleted).
CREATE TABLE overdubs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    take_id          INTEGER NOT NULL REFERENCES takes(id),
    character_id     INTEGER NOT NULL REFERENCES characters(id),
    text             TEXT NOT NULL,
    exaggeration     REAL NOT NULL DEFAULT 0.5,
    voice_id         INTEGER REFERENCES character_voices(id),  -- sample cloned; NULL = preset
    tts_voice        TEXT,                                     -- preset used when no sample
    offset_s         REAL NOT NULL DEFAULT 0,  -- start, in the take's own (untrimmed) time
    volume           REAL NOT NULL DEFAULT 1.0,
    status           TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'done', 'failed')),
    error            TEXT,
    audio_path       TEXT,
    duration_s       REAL,
    cost_usd         REAL,
    job_id           INTEGER REFERENCES jobs(id),
    archived         INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX overdubs_take ON overdubs(take_id, archived);
