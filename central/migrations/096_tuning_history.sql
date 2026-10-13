-- Configuration snapshots (owner, 2026-10-13: "configuration snapshots" that keep "a history of what
-- has been done in the past"): each owner keeps its own history, written where its state converges,
-- and a snapshot is composed when read. Picture history by Display (kept) and by port (sent);
-- Position history by Frame. The rules live in app code (central/displays/picture_history.py,
-- central/fleet/position_history.py, central/tuning_history/; the stores in central/infra/); there is
-- no trigger and no stored procedure here. No backwards compatibility in the data: no entry is
-- written for the past.
--
-- Every `at` is PostgreSQL's clock_timestamp() at the append (one clock, compared only with itself),
-- stored as TIMESTAMPTZ so a read-only SQL reader sees UTC. Each table is trimmed by its writer to the
-- newest entries per key (central/tuning_history/model.py KEPT_ENTRIES, SENT_ENTRIES); no reaper.
-- `restored_from` names the entry a Restore loaded (any history table's `id`); entries are trimmed,
-- so it is never a foreign key.

-- What Photo Wall keeps for a Display: each Done and Copy, and a change made outside Photo Wall
-- found before one (kept-unrecorded). `number` is the Display's commit order, taken under its row
-- lock. `picture` is the DisplayPicture under the displays columns' names (as picture_sessions.draft);
-- `lut_recipe_id` the recipe its LUT was baked under then (NULL: no LUT).
CREATE TABLE picture_kept_entries (
    id UUID PRIMARY KEY,
    display_id UUID NOT NULL REFERENCES displays(id) ON DELETE CASCADE,
    number BIGINT NOT NULL CHECK (number >= 1),
    kind TEXT NOT NULL CHECK (kind IN ('kept-done', 'kept-copied', 'kept-unrecorded')),
    at TIMESTAMPTZ NOT NULL,
    session_id UUID NULL,
    restored_from UUID NULL,
    picture JSONB NOT NULL CHECK (jsonb_typeof(picture) = 'object'),
    lut_recipe_id TEXT NULL,
    UNIQUE (display_id, number));
CREATE INDEX picture_kept_entries_at ON picture_kept_entries (display_id, at DESC, number DESC);

-- What Central sent a port: one entry per rise of the port's picture_change (its number), with
-- the Display seen there and the Frame bound to it then. Written by the picture document's
-- projection; idempotent on (player_id, output_id, picture_change).
CREATE TABLE picture_sent_entries (
    id UUID PRIMARY KEY,
    player_id TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    output_id TEXT NOT NULL,
    picture_change BIGINT NOT NULL CHECK (picture_change >= 1),
    display_id UUID NOT NULL REFERENCES displays(id) ON DELETE CASCADE,
    frame_id TEXT NULL REFERENCES frames(id) ON DELETE SET NULL,
    kind TEXT NOT NULL CHECK (kind IN ('sent-draft', 'sent-back', 'sent-kept')),
    at TIMESTAMPTZ NOT NULL,
    session_id UUID NULL,
    restored_from UUID NULL,
    picture JSONB NOT NULL CHECK (jsonb_typeof(picture) = 'object'),
    lut_recipe_id TEXT NULL,
    test_pattern TEXT NULL CHECK (test_pattern IN ('white', 'grey-ramp', 'black-level')),
    UNIQUE (player_id, output_id, picture_change));
CREATE INDEX picture_sent_entries_display_at ON picture_sent_entries (display_id, at DESC, picture_change DESC);
CREATE INDEX picture_sent_entries_frame_at ON picture_sent_entries (frame_id, at DESC, picture_change DESC);

-- A Frame's Position, mat and lights: what Photo Wall keeps (a Save, a profile change, a change made
-- outside Photo Wall found before one) and what Central handed its Node (a trial's draft, the kept
-- values back). One numbering per Frame, taken under its row lock. `fit` is contracts.models
-- FrameFit's JSON, `mat` Mat's; `profile_*_px` and `display_id` are what the entry was made for: the
-- Frame's profile and the Display seen on its bound port then (NULL: none). A deleted Frame takes its
-- history.
CREATE TABLE position_entries (
    id UUID PRIMARY KEY,
    frame_id TEXT NOT NULL REFERENCES frames(id) ON DELETE CASCADE,
    number BIGINT NOT NULL CHECK (number >= 1),
    kind TEXT NOT NULL CHECK (kind IN ('kept-saved', 'kept-profile-changed', 'kept-unrecorded',
                                       'sent-draft', 'sent-back')),
    at TIMESTAMPTZ NOT NULL,
    session_id UUID NULL,
    restored_from UUID NULL,
    fit JSONB NOT NULL CHECK (jsonb_typeof(fit) = 'object'),
    mat JSONB NOT NULL CHECK (jsonb_typeof(mat) = 'object'),
    profile_width_px INTEGER NOT NULL CHECK (profile_width_px > 0),
    profile_height_px INTEGER NOT NULL CHECK (profile_height_px > 0),
    display_id UUID NULL REFERENCES displays(id) ON DELETE SET NULL,
    UNIQUE (frame_id, number));
CREATE INDEX position_entries_at ON position_entries (frame_id, at DESC, number DESC);

-- A picture session remembers the entry its draft was last restored from (NULL: none).
ALTER TABLE picture_sessions ADD COLUMN restored_from UUID NULL;

-- A Position trial: `session_id` stays the same across renewals (a begin's own trial_id, carried by
-- each renew), `restored_from` the entry its draft was last restored from. Trials are leased for at
-- most 110 s, so the ones already here are each their own session.
ALTER TABLE node_calibration_trials
    ADD COLUMN session_id UUID NULL,
    ADD COLUMN restored_from UUID NULL;
UPDATE node_calibration_trials SET session_id = trial_id;
