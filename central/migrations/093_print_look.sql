-- The print look (design /print-look, 2026-10-12): the Display's Photo Wall (software) group gains
-- saturation, highlight rolloff, black floor and a LUT choice with its amount; the Frame gains its
-- mat (depth and lights), saved through Position's trial; the house keeps the paper profiles LUTs
-- are baked from. The rules live in app code (central/displays/picture.py, central/displays/lut.py,
-- central/infra/display_store.py, central/infra/lut_store.py, central/fleet/node_calibration.py,
-- central/registry.py); there is no trigger and no stored procedure here. No backwards
-- compatibility in the data.

-- The paper profiles: the one source of every LUT. A LUT is never stored; Central bakes it from
-- the profile whenever it is asked. Named by the profile's SHA-256; `name` is the profile's own
-- description, shown in the LUTs list.
CREATE TABLE luts (
    profile_sha256 TEXT PRIMARY KEY CHECK (profile_sha256 ~ '^[0-9a-f]{64}$'),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 128),
    profile BYTEA NOT NULL CHECK (octet_length(profile) BETWEEN 128 AND 8388608),
    added_at DOUBLE PRECISION NOT NULL);

-- The print look on the Display (contracts/node_picture.py SoftwarePicture). Each default changes
-- nothing, so this migration alone changes no picture. A LUT in use cannot be removed.
ALTER TABLE displays
    ADD COLUMN software_saturation_percent INTEGER NOT NULL DEFAULT 100
        CHECK (software_saturation_percent BETWEEN 0 AND 150),
    ADD COLUMN software_highlight_rolloff_percent INTEGER NOT NULL DEFAULT 0
        CHECK (software_highlight_rolloff_percent BETWEEN 0 AND 100),
    ADD COLUMN software_black_floor_tenths INTEGER NOT NULL DEFAULT 0
        CHECK (software_black_floor_tenths BETWEEN 0 AND 100),
    ADD COLUMN software_lut_profile TEXT NULL REFERENCES luts(profile_sha256) ON DELETE RESTRICT,
    ADD COLUMN software_lut_amount_percent INTEGER NOT NULL DEFAULT 100
        CHECK (software_lut_amount_percent BETWEEN 0 AND 100);

-- Open picture sessions are dropped: their drafts lack the new fields (a session's draft is a
-- DisplayPicture under the displays columns' names).
DELETE FROM picture_sessions;

-- The Frame's mat (contracts/models.py Mat): rides the binding beside profile and calibration.
-- The default casts no shadow.
ALTER TABLE frames
    ADD COLUMN mat JSONB NOT NULL DEFAULT '{"depth_mm": 0.0, "lights": []}'::jsonb
        CHECK (jsonb_typeof(mat) = 'object');

-- Position's trial carries the draft's mat beside its calibration, and its Save keeps both.
-- An active trial's candidate hash now covers the mat, so the ones open are ended (a console
-- begins the next at once).
UPDATE node_calibration_trials SET state = 'ended' WHERE state = 'active';
ALTER TABLE node_calibration_trials
    ADD COLUMN mat JSONB NOT NULL DEFAULT '{"depth_mm": 0.0, "lights": []}'::jsonb
        CHECK (jsonb_typeof(mat) = 'object'),
    ADD COLUMN saved_mat JSONB NULL;
