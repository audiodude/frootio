-- frootio schema, version 1.
-- Conventions: INTEGER autoincrement ids, ISO-8601 UTC text timestamps,
-- JSON stored as TEXT, booleans as INTEGER 0/1, media paths are relative to
-- the media root. Nothing is hard-deleted from the UI: rows are archived or
-- hidden instead. Later changes go in frootio/migrations/NNN_name.sql.

-- Key/value app settings (e.g. last used OpenRouter model). value is JSON.
CREATE TABLE settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- ---------------------------------------------------------------- library --

CREATE TABLE series (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    slug           TEXT NOT NULL UNIQUE,
    style_preamble TEXT NOT NULL DEFAULT '',   -- prompt part 1 source
    archived       INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE characters (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    slug           TEXT NOT NULL UNIQUE,
    description    TEXT NOT NULL DEFAULT '',   -- used in the refs legend
    main_image_id  INTEGER REFERENCES character_images(id),  -- the only image sent to the video model
    voice_id       INTEGER REFERENCES character_voices(id),  -- current voice sample pointer
    archived       INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- Content-addressed images: characters/<slug>/images/<sha12>.<ext>.
-- Main, extras and generated candidates all live here; "main" is the pointer above.
CREATE TABLE character_images (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id      INTEGER NOT NULL REFERENCES characters(id),
    path              TEXT NOT NULL,
    sha256            TEXT NOT NULL,
    width             INTEGER,                 -- needed for H3 image-token cost (aspect ratio)
    height            INTEGER,
    source            TEXT NOT NULL DEFAULT 'upload'
                      CHECK (source IN ('upload', 'generated', 'import')),
    gen_prompt        TEXT,                    -- nano-banana prompt, when generated
    gen_model         TEXT,                    -- fal endpoint used
    gen_input_paths   TEXT,                    -- JSON list of input image relpaths (edit mode)
    gen_request_id    TEXT,
    hidden            INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    UNIQUE (character_id, sha256)
);
CREATE INDEX character_images_character ON character_images(character_id);

-- Content-addressed voice samples: characters/<slug>/voice/<sha12>.wav (2-5s).
CREATE TABLE character_voices (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id      INTEGER NOT NULL REFERENCES characters(id),
    path              TEXT NOT NULL,
    sha256            TEXT NOT NULL,
    duration_s        REAL NOT NULL,
    source            TEXT NOT NULL
                      CHECK (source IN ('upload', 'take_clip', 'chatterbox', 'import')),
    source_take_id    INTEGER REFERENCES takes(id),   -- take_clip: vocals stem it came from
    clip_start_s      REAL,                           -- take_clip/upload range picked in the clipper
    clip_end_s        REAL,
    tts_voice         TEXT,                           -- chatterbox preset voice
    tts_text          TEXT,                           -- chatterbox test line
    gen_request_id    TEXT,
    created_at        TEXT NOT NULL,
    UNIQUE (character_id, sha256)
);
CREATE INDEX character_voices_character ON character_voices(character_id);

-- Speaker tags resolving to a character (matched case-insensitively). The name
-- always matches implicitly. Cross-character uniqueness within a series is
-- validated in code (two characters cast in the same series can't share one).
CREATE TABLE character_aliases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id  INTEGER NOT NULL REFERENCES characters(id),
    alias         TEXT NOT NULL,
    alias_norm    TEXT NOT NULL,              -- lower(trim(alias))
    created_at    TEXT NOT NULL,
    UNIQUE (character_id, alias_norm)
);
CREATE INDEX character_aliases_norm ON character_aliases(alias_norm);

CREATE TABLE series_cast (
    series_id     INTEGER NOT NULL REFERENCES series(id),
    character_id  INTEGER NOT NULL REFERENCES characters(id),
    created_at    TEXT NOT NULL,
    PRIMARY KEY (series_id, character_id)
);

CREATE TABLE locations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    slug         TEXT NOT NULL UNIQUE,
    description  TEXT NOT NULL DEFAULT '',     -- prompt part 3 source
    archived     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- Content-addressed: locations/<slug>/images/<sha12>.<ext>. ref_order 1..3 marks
-- the (optional) ref images sent to the model; NULL = library/candidate only.
CREATE TABLE location_images (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id      INTEGER NOT NULL REFERENCES locations(id),
    path             TEXT NOT NULL,
    sha256           TEXT NOT NULL,
    width            INTEGER,
    height           INTEGER,
    ref_order        INTEGER CHECK (ref_order IS NULL OR ref_order BETWEEN 1 AND 3),
    source           TEXT NOT NULL DEFAULT 'upload'
                     CHECK (source IN ('upload', 'generated', 'import')),
    gen_prompt       TEXT,
    gen_model        TEXT,
    gen_input_paths  TEXT,
    gen_request_id   TEXT,
    hidden           INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    UNIQUE (location_id, sha256)
);
CREATE INDEX location_images_location ON location_images(location_id);

-- Locations follow the same sharing rules as characters.
CREATE TABLE series_locations (
    series_id    INTEGER NOT NULL REFERENCES series(id),
    location_id  INTEGER NOT NULL REFERENCES locations(id),
    created_at   TEXT NOT NULL,
    PRIMARY KEY (series_id, location_id)
);

-- Series music library: series/<slug>/music/<track>.mp3
CREATE TABLE music_tracks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id   INTEGER NOT NULL REFERENCES series(id),
    name        TEXT NOT NULL,
    path        TEXT NOT NULL,
    sha256      TEXT,
    duration_s  REAL,
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    UNIQUE (series_id, path)
);

-- --------------------------------------------------------------- episodes --

CREATE TABLE episodes (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id            INTEGER NOT NULL REFERENCES series(id),
    number               INTEGER NOT NULL,
    title                TEXT NOT NULL DEFAULT '',
    default_location_id  INTEGER REFERENCES locations(id),
    take_count           INTEGER NOT NULL DEFAULT 1 CHECK (take_count >= 1),
    spend_cap_usd        REAL NOT NULL DEFAULT 20.0,   -- video-only failsafe cap
    -- assembly settings
    music_track_id       INTEGER REFERENCES music_tracks(id),
    music_offset_s       REAL NOT NULL DEFAULT 0,     -- start offset into the track
    music_volume         REAL NOT NULL DEFAULT 0.35,  -- linear gain
    music_duck_db        REAL NOT NULL DEFAULT 12.0,  -- how far music ducks under dialogue
    fx_volume            REAL NOT NULL DEFAULT 1.0,   -- linear gain for all fx stems
    captions_on          INTEGER NOT NULL DEFAULT 1,
    -- preview.mp4 is reused until the assembly settings hash changes
    preview_path         TEXT,
    preview_hash         TEXT,
    preview_rendered_at  TEXT,
    archived             INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    UNIQUE (series_id, number)
);

-- Final renders: series/<slug>/episodes/<n>/exports/<timestamp>.mp4
CREATE TABLE exports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id   INTEGER NOT NULL REFERENCES episodes(id),
    path         TEXT,
    settings     TEXT NOT NULL DEFAULT '{}',   -- JSON snapshot of assembly settings + takes/trims
    captions_on  INTEGER NOT NULL DEFAULT 1,
    status       TEXT NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'running', 'done', 'failed')),
    error        TEXT,
    job_id       INTEGER REFERENCES jobs(id),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX exports_episode ON exports(episode_id);

-- Every explicit save that changes the text creates a new version.
CREATE TABLE script_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id  INTEGER NOT NULL REFERENCES episodes(id),
    version     INTEGER NOT NULL,               -- 1, 2, ... per episode
    text        TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'edit',   -- 'edit' or 'import:<filename>'
    created_at  TEXT NOT NULL,
    UNIQUE (episode_id, version)
);

-- Level 2 split results awaiting accept/reject (and level 0 runs, for history).
CREATE TABLE split_proposals (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id         INTEGER NOT NULL REFERENCES episodes(id),
    script_version_id  INTEGER REFERENCES script_versions(id),
    level              INTEGER NOT NULL CHECK (level IN (0, 2)),
    model              TEXT,
    result             TEXT NOT NULL DEFAULT '[]',   -- JSON list of proposed snippets
    estimated_total_s  REAL,
    warnings           TEXT NOT NULL DEFAULT '[]',   -- JSON list of strings
    cost_usd           REAL NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'accepted', 'rejected', 'failed')),
    error              TEXT,
    job_id             INTEGER REFERENCES jobs(id),
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX split_proposals_episode ON split_proposals(episode_id);

-- --------------------------------------------------------------- snippets --
-- One fal call, at most 15s. Prompt parts: style, legend, location (derived:
-- inherited or overridden), body, tail (always per-snippet text).
-- <part>_overridden = 1 means <part>_override is frozen user text;
-- <part>_source_snapshot is the inherited text at override time, compared with
-- the current inherited text to show the "source changed" badge.
CREATE TABLE snippets (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id                INTEGER NOT NULL REFERENCES episodes(id),
    position                  INTEGER NOT NULL DEFAULT 0,   -- board order among active snippets
    archived                  INTEGER NOT NULL DEFAULT 0,
    archived_at               TEXT,
    origin                    TEXT NOT NULL DEFAULT 'manual'
                              CHECK (origin IN ('split0', 'split2', 'manual', 'copy')),
    split_proposal_id         INTEGER REFERENCES split_proposals(id),
    copied_from_snippet_id    INTEGER REFERENCES snippets(id),
    body                      TEXT NOT NULL DEFAULT '',
    tail                      TEXT NOT NULL DEFAULT 'No music. Dialogue, foley and ambience only.',
    script_slice              TEXT NOT NULL DEFAULT '',     -- caption source; editable
    location_id               INTEGER REFERENCES locations(id),
    target_duration_s         INTEGER NOT NULL DEFAULT 15
                              CHECK (target_duration_s BETWEEN 5 AND 15),
    continue_from_previous    INTEGER NOT NULL DEFAULT 0,
    pinned_seed               INTEGER,                      -- NULL = random
    style_overridden          INTEGER NOT NULL DEFAULT 0,
    style_override            TEXT NOT NULL DEFAULT '',
    style_source_snapshot     TEXT,
    legend_overridden         INTEGER NOT NULL DEFAULT 0,
    legend_override           TEXT NOT NULL DEFAULT '',
    legend_source_snapshot    TEXT,
    location_overridden       INTEGER NOT NULL DEFAULT 0,
    location_override         TEXT NOT NULL DEFAULT '',
    location_source_snapshot  TEXT,
    warnings                  TEXT NOT NULL DEFAULT '[]',   -- JSON list of strings
    selected_take_id          INTEGER REFERENCES takes(id),
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);
CREATE INDEX snippets_episode ON snippets(episode_id, archived, position);

-- Cast in legend order (character main refs come first in the ref list).
CREATE TABLE snippet_cast (
    snippet_id    INTEGER NOT NULL REFERENCES snippets(id),
    character_id  INTEGER NOT NULL REFERENCES characters(id),
    position      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (snippet_id, character_id)
);

-- ------------------------------------------------------------------ takes --
-- One generation result. Never deleted; hidden instead. Stores exactly what
-- was sent so the prompt can be loaded back into the editor.
-- status: queued -> running -> processing -> done; failure states are failed
-- (fal error / submit failure, $0), cancelled ($0), download_failed and
-- processing_failed (cost still counts; retry without a new fal call).
CREATE TABLE takes (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    snippet_id             INTEGER NOT NULL REFERENCES snippets(id),
    episode_id             INTEGER NOT NULL REFERENCES episodes(id),
    status                 TEXT NOT NULL DEFAULT 'queued'
                           CHECK (status IN ('queued', 'running', 'processing', 'done', 'failed',
                                             'cancelled', 'download_failed', 'processing_failed')),
    error                  TEXT,
    hidden                 INTEGER NOT NULL DEFAULT 0,
    batch_id               TEXT,          -- one Generate click (shares uploaded ref URLs)
    retry_of_take_id       INTEGER REFERENCES takes(id),
    job_id                 INTEGER REFERENCES jobs(id),
    model                  TEXT NOT NULL DEFAULT 'minimax/h3-max/reference-to-video',
    -- the five prompt parts as sent, with the derived parts' flags
    style_text             TEXT NOT NULL DEFAULT '',
    style_overridden       INTEGER NOT NULL DEFAULT 0,
    legend_text            TEXT NOT NULL DEFAULT '',
    legend_overridden      INTEGER NOT NULL DEFAULT 0,
    location_text          TEXT NOT NULL DEFAULT '',
    location_overridden    INTEGER NOT NULL DEFAULT 0,
    body_text              TEXT NOT NULL DEFAULT '',
    tail_text              TEXT NOT NULL DEFAULT '',
    prompt                 TEXT NOT NULL DEFAULT '',   -- exact joined string sent
    request_args           TEXT,          -- JSON of the full fal arguments sent
    -- refs: JSON list of {kind, path, sha, label, url} (path relative to media root)
    refs                   TEXT NOT NULL DEFAULT '[]',
    ref_tokens             INTEGER,
    requested_duration_s   INTEGER NOT NULL,
    seed_requested         INTEGER,       -- pinned seed (+ offset in the batch), NULL = random
    seed                   INTEGER,       -- seed fal reported back
    -- continuity: start frame extracted from the previous snippet's selected take
    start_frame_take_id    INTEGER REFERENCES takes(id),
    start_frame_time_s     REAL,          -- that take's trim-out point when extracted
    start_frame_path       TEXT,          -- .../start-frames/<sha12>.png
    start_frame_url        TEXT,
    -- cost
    estimated_cost_usd     REAL NOT NULL DEFAULT 0,
    cost_usd               REAL,          -- computed from fal's formula once known; 0 for failed/cancelled
    fal_request_id         TEXT,
    download_attempts      INTEGER NOT NULL DEFAULT 0,
    -- media (relative to media root)
    video_path             TEXT,
    vocals_path            TEXT,
    fx_path                TEXT,
    poster_path            TEXT,
    peaks_path             TEXT,          -- waveform peaks JSON (assembly, selected takes)
    result_url             TEXT,          -- fal video URL
    video_duration_s       REAL,
    width                  INTEGER,
    height                 INTEGER,
    -- assembly trims (H3 overshoots up to ~0.7s)
    trim_in_s              REAL NOT NULL DEFAULT 0,
    trim_out_s             REAL,          -- NULL = end of video
    created_at             TEXT NOT NULL,
    submitted_at           TEXT,
    completed_at           TEXT,
    updated_at             TEXT NOT NULL
);
CREATE INDEX takes_snippet ON takes(snippet_id);
CREATE INDEX takes_episode_status ON takes(episode_id, status);

-- Caption alignment cache per take and script slice.
CREATE TABLE caption_alignments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    take_id       INTEGER NOT NULL REFERENCES takes(id),
    slice_sha256  TEXT NOT NULL,       -- hash of the caption text it was aligned for
    caption_text  TEXT NOT NULL,
    words         TEXT NOT NULL,       -- JSON list of {word, start, end, line}
    method        TEXT NOT NULL CHECK (method IN ('aligned', 'fallback', 'mixed')),
    created_at    TEXT NOT NULL,
    UNIQUE (take_id, slice_sha256)
);

-- ------------------------------------------------------------- jobs/spend --

CREATE TABLE jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    payload         TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    fal_request_id  TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    result          TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX jobs_status ON jobs(status, id);

CREATE TABLE spend (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL CHECK (kind IN ('video', 'image', 'tts', 'llm')),
    amount_usd    REAL NOT NULL,
    episode_id    INTEGER REFERENCES episodes(id),
    series_id     INTEGER REFERENCES series(id),
    character_id  INTEGER REFERENCES characters(id),
    location_id   INTEGER REFERENCES locations(id),
    take_id       INTEGER REFERENCES takes(id),
    note          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
CREATE INDEX spend_episode ON spend(episode_id, kind);
CREATE INDEX spend_series ON spend(series_id);
