-- Decision 0034, S2f part 2. A Display's open picture session is on its row (`picture_session`, a
-- JSON object; at most one per Display, as `picture_sessions`' unique display key held), and the
-- shared-serial fact is a Display flag (`shared_serial`): `shared_serials` was keyed by (maker,
-- product, serial), so the flag goes on the Display with that triple; a triple with no Display
-- loses the fact, which the judge finds again at the next sighting of the two Outputs at once.
-- The kept and sent picture entries are one table, `picture_history` (single table inheritance:
-- `kind` 'kept', numbered and trimmed per Display, or 'sent', keyed and trimmed per port), the
-- entries' ids kept; `by` is who appended it, unknown for every entry before this release.
-- `displays` and `power_requests` gain their audit fields (`displays.first_seen_at` is its
-- created time, so it is renamed). Plain statements, no trigger or function, forward-only.
ALTER TABLE displays RENAME COLUMN first_seen_at TO created_at;
ALTER TABLE displays
    ADD COLUMN picture_session JSONB CHECK (jsonb_typeof(picture_session) = 'object'),
    ADD COLUMN shared_serial BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN created_by TEXT,
    ADD COLUMN changed_at DOUBLE PRECISION,
    ADD COLUMN changed_by TEXT;

UPDATE displays d SET picture_session = jsonb_build_object(
        'id', ps.id, 'frame_id', ps.frame_id, 'sequence', ps.sequence, 'draft', ps.draft,
        'idle_until', ps.idle_until, 'created_at', ps.created_at, 'began_at_change', ps.began_at_change,
        'hardware_before', ps.hardware_before, 'touched_hardware', to_jsonb(ps.touched_hardware),
        'touched_software', to_jsonb(ps.touched_software), 'test_pattern', ps.test_pattern,
        'test_pattern_ask', ps.test_pattern_ask, 'restored_from', ps.restored_from)
FROM picture_sessions ps WHERE ps.display_id = d.id;

UPDATE displays d SET shared_serial = TRUE
FROM shared_serials s WHERE s.maker = d.maker AND s.product = d.product AND s.serial = d.serial;

UPDATE displays SET created_by = 'unknown', changed_at = created_at, changed_by = 'unknown';
ALTER TABLE displays
    ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN changed_at SET NOT NULL,
    ALTER COLUMN changed_by SET NOT NULL;
-- A session is found by its id (the Picture tab's session routes).
CREATE UNIQUE INDEX displays_picture_session ON displays ((picture_session->>'id')) WHERE picture_session IS NOT NULL;

CREATE TABLE picture_history (
    id UUID PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('kept', 'sent')),
    display_id UUID NOT NULL REFERENCES displays(id) ON DELETE CASCADE,
    number BIGINT CHECK (number >= 1),
    player_id TEXT REFERENCES players(id) ON DELETE CASCADE,
    output_id TEXT,
    picture_change BIGINT CHECK (picture_change >= 1),
    frame_id TEXT REFERENCES frames(id) ON DELETE SET NULL,
    entry_kind TEXT NOT NULL,
    at TIMESTAMPTZ NOT NULL,
    by TEXT NOT NULL,
    session_id UUID,
    restored_from UUID,
    picture JSONB NOT NULL CHECK (jsonb_typeof(picture) = 'object'),
    test_pattern TEXT CHECK (test_pattern IN ('white', 'grey-ramp', 'black-level', 'target-pre-software',
                                              'target-post-software')),
    CHECK ((kind = 'kept') = (number IS NOT NULL)),
    CHECK ((kind = 'sent') = (player_id IS NOT NULL AND output_id IS NOT NULL AND picture_change IS NOT NULL)),
    CHECK (kind = 'sent' OR (frame_id IS NULL AND test_pattern IS NULL)),
    CHECK ((kind = 'kept' AND entry_kind IN ('kept-done', 'kept-copied', 'kept-unrecorded'))
           OR (kind = 'sent' AND entry_kind IN ('sent-draft', 'sent-back', 'sent-kept'))));
CREATE UNIQUE INDEX picture_history_kept ON picture_history (display_id, number) WHERE kind = 'kept';
CREATE UNIQUE INDEX picture_history_sent ON picture_history (player_id, output_id, picture_change) WHERE kind = 'sent';
CREATE INDEX picture_history_display_at ON picture_history (display_id, at DESC);
CREATE INDEX picture_history_frame_at ON picture_history (frame_id, at DESC, picture_change DESC) WHERE kind = 'sent';

INSERT INTO picture_history (id, kind, display_id, number, entry_kind, at, by, session_id, restored_from, picture)
SELECT id, 'kept', display_id, number, kind, at, 'unknown', session_id, restored_from, picture
FROM picture_kept_entries;

INSERT INTO picture_history (id, kind, display_id, player_id, output_id, picture_change, frame_id, entry_kind, at, by,
                             session_id, restored_from, picture, test_pattern)
SELECT id, 'sent', display_id, player_id, output_id, picture_change, frame_id, kind, at, 'unknown', session_id,
       restored_from, picture, test_pattern
FROM picture_sent_entries;

ALTER TABLE power_requests
    ADD COLUMN created_by TEXT,
    ADD COLUMN changed_at DOUBLE PRECISION,
    ADD COLUMN changed_by TEXT;
UPDATE power_requests SET created_by = 'unknown', changed_at = created_at, changed_by = 'unknown';
ALTER TABLE power_requests
    ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN changed_at SET NOT NULL,
    ALTER COLUMN changed_by SET NOT NULL;

DROP TABLE picture_kept_entries, picture_sent_entries, picture_sessions, shared_serials;
