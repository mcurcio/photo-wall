-- Decision 0036: a Frame's status, since when. One table, one aggregate, one repository
-- (central/infra/frame_status_store.py; decision 0034), each row with its audit fields.
--
-- * `frame_status`: one row per Frame, the key of its status as Central last recorded it (its word,
--   or its setup step while it is being set up; the row of the decision table that matched; its
--   problem's code) and `since`, when that key began. The word itself is worked out on read
--   (central/frame_status/policy.py); only its beginning is kept. Written only by the scheduler's
--   `status` step, and only when the key changes (decision 0033). The vocabularies are code's
--   (central/frame_status/model.py), so the table has no CHECK on them.
-- * `events`: a change of a Frame's status is one more kind in Central's event log, 'frame-status'
--   (central/infra/events.py `EventKind`), body {"frame_id", "word", "setup", "row", "problem",
--   "since"}, kept a week like every event.
-- Plain statements, no trigger or function, forward-only.
CREATE TABLE frame_status (
    frame_id   TEXT PRIMARY KEY REFERENCES frames(id) ON DELETE CASCADE,
    word       TEXT NULL,
    setup      TEXT NULL,
    because    TEXT NOT NULL,
    problem    TEXT NULL,
    since      DOUBLE PRECISION NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    created_by TEXT NOT NULL,
    changed_at DOUBLE PRECISION NOT NULL,
    changed_by TEXT NOT NULL,
    CHECK ((word IS NULL) <> (setup IS NULL))
);

ALTER TABLE events DROP CONSTRAINT events_kind_check;
ALTER TABLE events ADD CONSTRAINT events_kind_check
    CHECK (kind IN ('record', 'gap', 'link-action', 'refusal', 'frame-status'));
