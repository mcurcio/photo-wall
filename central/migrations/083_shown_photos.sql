-- Roadmap 5a (run ledger .claude/runs/photo-5a.md, slice O1): shown history.
-- `photo_selections`: what each offered layer shows (one row per assignment id, first write
-- wins), written by coordination when it offers a plan. `shown_photos`: one row per layer its
-- Player reported presented (owner, 2026-10-11: "“Shown” means it was displayed on a frame"),
-- copied from its selection. Times are the layers' planned intervals on Central's clock.
-- The only reader and writer is central/infra/shown_history_store.py (PgShownHistory).
--
-- Plain statements only: no trigger, stored procedure or function. Forward-only (central/db.py).

CREATE TABLE photo_selections (
    assignment_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, frame_id TEXT NOT NULL,
    scene_id TEXT NOT NULL, pass_number INTEGER NOT NULL CHECK (pass_number >= 1),
    starts_at DOUBLE PRECISION NOT NULL, ends_at DOUBLE PRECISION NOT NULL,
    fade_in DOUBLE PRECISION NOT NULL CHECK (fade_in >= 0),
    after_end TEXT NOT NULL CHECK (after_end IN ('leave_as_is', 'keep_this_photo', 'keep_nothing')));
CREATE INDEX photo_selections_ends ON photo_selections(ends_at);

CREATE TABLE shown_photos (LIKE photo_selections INCLUDING CONSTRAINTS,
    presented_at DOUBLE PRECISION NOT NULL, PRIMARY KEY (assignment_id));
CREATE INDEX shown_photos_ends ON shown_photos(ends_at);
CREATE INDEX shown_photos_pass ON shown_photos(scene_id, pass_number);
CREATE INDEX shown_photos_kept ON shown_photos(frame_id, starts_at DESC) WHERE after_end <> 'leave_as_is';
