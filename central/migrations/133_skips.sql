-- Decision 0036 (slice 3): Skip and Previous, and the cover Never show adds where its photo shows.
-- One table, one aggregate, one repository (central/infra/skip_store.py; decision 0034).
--
-- * `skips`: one row per request, written once and never updated, inside Coordination's lock.
--   `scope` and `target_id`: a wall (its surface id), one Frame, or a photo (Never show);
--   `cause`: next, previous or never-show; `at`: the shared instant the Frames move at;
--   `outcome`: per Frame, its cover (central/everyday/model.py `Cover`) or why it stayed;
--   `ends_at`: the last cover's end (`at` when none moved), which the batched retention reads
--   (a row past it by a day goes). The audit fields name the person (decision 0035).
-- Plain statements, no trigger or function, forward-only.
CREATE TABLE skips (
    request_id TEXT PRIMARY KEY CHECK (length(request_id) BETWEEN 1 AND 128),
    scope      TEXT NOT NULL CHECK (scope IN ('wall', 'frame', 'photo')),
    target_id  TEXT NOT NULL,
    cause      TEXT NOT NULL CHECK (cause IN ('next', 'previous', 'never-show')),
    at         DOUBLE PRECISION NOT NULL,
    outcome    JSONB NOT NULL CHECK (jsonb_typeof(outcome) = 'object'),
    ends_at    DOUBLE PRECISION NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    created_by TEXT NOT NULL,
    changed_at DOUBLE PRECISION NOT NULL,
    changed_by TEXT NOT NULL
);

CREATE INDEX skips_ends_at ON skips (ends_at);

-- Previous steps back through one Frame's shown history, newest first.
CREATE INDEX shown_photos_frame ON shown_photos (frame_id, presented_at DESC);
