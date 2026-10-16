-- Room light on the wall (decision 0028). Room lights and Sensors, observed, never driven; each
-- reporting reference's last word; the one refusal log; the Home Assistant link's heartbeat; what
-- each Player drew with; the Display's light replaces the picture's sliders and its LUT; a Frame's
-- lighting replaces its mat. The rules live in app code (central/room_light/, central/frame_lighting/,
-- central/infra/); there is no trigger and no stored procedure here, and every range is pydantic's:
-- the only checks are each JSON column's type. No backwards compatibility in the data.
--
-- Upgrade data, so every picture equals today's at neutral sliders: each Display's light is
-- contracts/node_picture.py UPGRADE_DISPLAY_LIGHT (1000 lx matched, black 0.0003 of white); one fixed
-- room light "room" exists; each Frame's lighting is central/frame_lighting/policy.py
-- upgrade_lighting(UPGRADE_DISPLAY_LIGHT, its mat's depth, its Display's software brightness / 100)
-- (tests/test_migration_099.py holds this data to it): one wash of "room" at 1000 x brightness / 100
-- lx (at least 0.1), at full, and paper of whiteness 0.9 and ink -log10(0.0003). The Display is the one
-- its Position names, else the one last seen on its bound Output; with neither, brightness 100 %. Its
-- trials' and Position history's lighting take the same wash with their own mat's depth. The old mat
-- lights' relative strengths have no lux, so they are not carried. The Display's LUT is not carried:
-- its name needs the recipe Central bakes with, which is app code's.

-- Room lights by id (central/room_light/model.py RoomLight, as JSON); `position` keeps the House ›
-- Lights order.
CREATE TABLE room_lights (
    id TEXT PRIMARY KEY,
    light JSONB NOT NULL CHECK (jsonb_typeof(light) = 'object'),
    position INTEGER NOT NULL);

-- Sensors by id (central/room_light/model.py Sensor, as JSON).
CREATE TABLE sensors (
    id TEXT PRIMARY KEY,
    sensor JSONB NOT NULL CHECK (jsonb_typeof(sensor) = 'object'));

-- Each reference's last word and when Central heard it, kept whether or not a room light or Sensor
-- names it (the newest of each source are kept: central/infra/room_light_store.py).
CREATE TABLE light_words (
    source TEXT NOT NULL,
    address TEXT NOT NULL,
    word JSONB NOT NULL CHECK (jsonb_typeof(word) = 'object'),
    heard_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (source, address));

CREATE TABLE reading_words (
    source TEXT NOT NULL,
    address TEXT NOT NULL,
    word JSONB NOT NULL CHECK (jsonb_typeof(word) = 'object'),
    heard_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (source, address));

-- The one refusal log of every source (the newest of each source are kept).
CREATE TABLE report_refusals (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    address TEXT NOT NULL,
    reason TEXT NOT NULL,
    at TIMESTAMPTZ NOT NULL);

-- The Home Assistant link's newest heartbeat: what it was doing, since when, and when it beat.
CREATE TABLE home_assistant_link (
    one BOOLEAN PRIMARY KEY DEFAULT true CHECK (one),
    beat TEXT NOT NULL,
    since TIMESTAMPTZ NOT NULL,
    beat_at TIMESTAMPTZ NOT NULL);

-- The room-lights document's `change`, raised in the same transaction as any change below it. Its
-- row is also the house's lock (central/infra/frame_lighting_store.py).
CREATE TABLE room_lights_change (
    one BOOLEAN PRIMARY KEY DEFAULT true CHECK (one),
    change BIGINT NOT NULL);

-- What each Player last said it drew its Frames with (contracts/room_light.py RoomLightsDrawn).
CREATE TABLE room_lights_drawn (
    player_id TEXT PRIMARY KEY REFERENCES players(id) ON DELETE CASCADE,
    drawn TEXT NULL,
    following BOOLEAN NOT NULL,
    reported_at TIMESTAMPTZ NOT NULL);

INSERT INTO room_lights (id, light, position) VALUES
    ('room', '{"kind": "fixed", "name": "Room", "colour": {"kind": "fixed", "kelvin": 6500}}'::jsonb, 0);
INSERT INTO room_lights_change (one, change) VALUES (true, 1);

-- Each Frame's wash, from its Display's software brightness, before the column goes.
CREATE TEMP TABLE upgrade_wash ON COMMIT DROP AS
    SELECT f.id AS frame_id,
           GREATEST(0.1::double precision, 1000.0::double precision
                    * (COALESCE(d.software_brightness_percent, 100)::double precision / 100.0::double precision)) AS lux
    FROM frames f
    LEFT JOIN bindings b ON b.frame_id = f.id
    LEFT JOIN output_displays seen ON seen.player_id = b.player_id AND seen.output_id = b.output_id
    LEFT JOIN displays d ON d.id = COALESCE(f.position_display_id, seen.display_id);

-- The Display's light (contracts/node_picture.py DisplayLight); the print look's sliders and the
-- Display's LUT go (the paper profile moves to the Frame's paper; a paper profile a Frame's paper
-- names cannot be removed: central/infra/lut_store.py). New Displays get UPGRADE_DISPLAY_LIGHT from app code.
ALTER TABLE displays
    ADD COLUMN matched_lux DOUBLE PRECISION NULL,   -- ranges: contracts/node_picture.py, never SQL
    ADD COLUMN panel_black DOUBLE PRECISION NULL;
UPDATE displays SET matched_lux = 1000.0, panel_black = 0.0003;
ALTER TABLE displays
    ALTER COLUMN matched_lux SET NOT NULL,
    ALTER COLUMN panel_black SET NOT NULL,
    DROP COLUMN software_brightness_percent,
    DROP COLUMN software_saturation_percent,
    DROP COLUMN software_highlight_rolloff_percent,
    DROP COLUMN software_black_floor_tenths,
    DROP COLUMN software_lut_profile,
    DROP COLUMN software_lut_amount_percent;

-- A picture session's draft is a DisplayPicture under the displays columns' names
-- (central/infra/picture_columns.py): the sliders leave it and the Display's light joins it; the
-- touched set forgets the removed settings.
UPDATE picture_sessions SET
    draft = (draft - 'software_brightness_percent' - 'software_saturation_percent'
                   - 'software_highlight_rolloff_percent' - 'software_black_floor_tenths'
                   - 'software_lut_profile' - 'software_lut_amount_percent')
            || '{"matched_lux": 1000.0, "panel_black": 0.0003}'::jsonb,
    touched_software = ARRAY(SELECT touched FROM unnest(touched_software) AS touched
                             WHERE touched NOT IN ('brightness', 'saturation', 'highlight-rolloff',
                                                   'black-floor', 'lut-amount', 'lut'));

-- Picture history entries keep what they recorded, without the removed settings. An entry from
-- before this release has no light: its Restore fills today's (central/displays/picture.py
-- `restored`). The Display has no LUT any more, so no entry names a recipe.
UPDATE picture_kept_entries SET
    picture = picture - 'software_brightness_percent' - 'software_saturation_percent'
                      - 'software_highlight_rolloff_percent' - 'software_black_floor_tenths'
                      - 'software_lut_profile' - 'software_lut_amount_percent';
UPDATE picture_sent_entries SET
    picture = picture - 'software_brightness_percent' - 'software_saturation_percent'
                      - 'software_highlight_rolloff_percent' - 'software_black_floor_tenths'
                      - 'software_lut_profile' - 'software_lut_amount_percent';
ALTER TABLE picture_kept_entries DROP COLUMN lut_recipe_id;
ALTER TABLE picture_sent_entries DROP COLUMN lut_recipe_id;

-- Each recorded picture document reads as the current contract (its software without the sliders,
-- the Display's light beside it); its digest no longer matches, so the next projection raises its
-- picture_change and the Pi is sent the new shape.
UPDATE picture_documents SET
    document = jsonb_set(document, '{software}',
                         (document->'software') - 'brightness_percent' - 'saturation_percent'
                         - 'highlight_rolloff_percent' - 'black_floor_tenths' - 'lut' - 'lut_amount_percent')
               || '{"light": {"matched_lux": 1000.0, "black": 0.0003}}'::jsonb,
    digest = 'upgraded-099';

-- A Frame's lighting (contracts/frame_lighting.py FrameLighting) replaces its mat, on its binding.
ALTER TABLE frames ADD COLUMN lighting JSONB NULL CHECK (jsonb_typeof(lighting) = 'object');
UPDATE frames SET lighting = jsonb_build_object(
    'mat_depth_mm', COALESCE((mat->>'depth_mm')::double precision, 0.0),
    'lights', jsonb_build_array(jsonb_build_object(
        'kind', 'wash', 'light', 'room', 'reference', jsonb_build_object('kind', 'at-full'),
        'lux', COALESCE((SELECT w.lux FROM upgrade_wash w WHERE w.frame_id = frames.id), 1000.0))),
    'paper', '{"whiteness": 0.9, "ink_density": 3.5228787452803374, "profile": null}'::jsonb);
ALTER TABLE frames ALTER COLUMN lighting SET NOT NULL, DROP COLUMN mat;

-- Position's trial carries the draft's lighting: the active ones end (their candidate hash now
-- covers the lighting; a console begins the next at once), and every trial's mat becomes lighting.
UPDATE node_calibration_trials SET state = 'ended' WHERE state = 'active';
ALTER TABLE node_calibration_trials
    ADD COLUMN lighting JSONB NULL CHECK (jsonb_typeof(lighting) = 'object'),
    ADD COLUMN saved_lighting JSONB NULL CHECK (saved_lighting IS NULL OR jsonb_typeof(saved_lighting) = 'object');
UPDATE node_calibration_trials SET
    lighting = jsonb_build_object(
        'mat_depth_mm', COALESCE((mat->>'depth_mm')::double precision, 0.0),
        'lights', jsonb_build_array(jsonb_build_object(
            'kind', 'wash', 'light', 'room', 'reference', jsonb_build_object('kind', 'at-full'),
            'lux', COALESCE((SELECT w.lux FROM upgrade_wash w WHERE w.frame_id = node_calibration_trials.frame_id), 1000.0))),
        'paper', '{"whiteness": 0.9, "ink_density": 3.5228787452803374, "profile": null}'::jsonb),
    saved_lighting = CASE WHEN saved_mat IS NULL THEN NULL ELSE jsonb_build_object(
        'mat_depth_mm', COALESCE((saved_mat->>'depth_mm')::double precision, 0.0),
        'lights', jsonb_build_array(jsonb_build_object(
            'kind', 'wash', 'light', 'room', 'reference', jsonb_build_object('kind', 'at-full'),
            'lux', COALESCE((SELECT w.lux FROM upgrade_wash w WHERE w.frame_id = node_calibration_trials.frame_id), 1000.0))),
        'paper', '{"whiteness": 0.9, "ink_density": 3.5228787452803374, "profile": null}'::jsonb) END;
ALTER TABLE node_calibration_trials
    ALTER COLUMN lighting SET NOT NULL,
    DROP COLUMN mat,
    DROP COLUMN saved_mat;

-- Position history: each entry's mat becomes the lighting it upgrades to, and a new Frame's first
-- entry records its creation (central/fleet/position_history.py KEPT_CREATED).
ALTER TABLE position_entries ADD COLUMN lighting JSONB NULL CHECK (jsonb_typeof(lighting) = 'object');
UPDATE position_entries SET lighting = jsonb_build_object(
    'mat_depth_mm', COALESCE((mat->>'depth_mm')::double precision, 0.0),
    'lights', jsonb_build_array(jsonb_build_object(
            'kind', 'wash', 'light', 'room', 'reference', jsonb_build_object('kind', 'at-full'),
            'lux', COALESCE((SELECT w.lux FROM upgrade_wash w WHERE w.frame_id = position_entries.frame_id), 1000.0))),
    'paper', '{"whiteness": 0.9, "ink_density": 3.5228787452803374, "profile": null}'::jsonb);
ALTER TABLE position_entries ALTER COLUMN lighting SET NOT NULL, DROP COLUMN mat,
    DROP CONSTRAINT position_entries_kind_check,
    ADD CONSTRAINT position_entries_kind_check CHECK (kind IN ('kept-created', 'kept-saved', 'kept-profile-changed',
                                                              'kept-unrecorded', 'sent-draft', 'sent-back'));

-- A binding carries its Frame's lighting (contracts/models.py OutputBinding, `lighting` required, no
-- `mat`): a Player's last configuration and each offered plan's bindings stored before this release
-- read as the current contract (as 093 did for the mat): the Frame's upgraded lighting, or for a
-- Frame gone since, its mat's upgrade.
UPDATE player_configurations SET configuration = jsonb_set(configuration, '{bindings}', (
    SELECT jsonb_agg(CASE WHEN jsonb_typeof(e.binding) = 'object'
                          THEN (e.binding - 'mat' - 'lighting') || jsonb_build_object('lighting', COALESCE(
                              (SELECT f.lighting FROM frames f WHERE f.id = e.binding->>'frame_id'),
                              jsonb_build_object(
                              'mat_depth_mm', COALESCE((e.binding->'mat'->>'depth_mm')::double precision, 0.0),
                              'lights', '[{"kind": "wash", "light": "room", "lux": 1000.0, "reference": {"kind": "at-full"}}]'::jsonb,
                              'paper', '{"whiteness": 0.9, "ink_density": 3.5228787452803374, "profile": null}'::jsonb)))
                          ELSE e.binding END
                     ORDER BY e.ordinal)
    FROM jsonb_array_elements(configuration->'bindings') WITH ORDINALITY AS e(binding, ordinal)))
WHERE jsonb_typeof(configuration->'bindings') = 'array'
  AND jsonb_path_exists(configuration, '$.bindings[*] ? (exists(@.mat) || !(exists(@.lighting)))');

UPDATE plan_offers SET manifest = jsonb_set(manifest, '{bindings}', (
    SELECT jsonb_agg(CASE WHEN jsonb_typeof(e.binding) = 'object'
                          THEN (e.binding - 'mat' - 'lighting') || jsonb_build_object('lighting', COALESCE(
                              (SELECT f.lighting FROM frames f WHERE f.id = e.binding->>'frame_id'),
                              jsonb_build_object(
                              'mat_depth_mm', COALESCE((e.binding->'mat'->>'depth_mm')::double precision, 0.0),
                              'lights', '[{"kind": "wash", "light": "room", "lux": 1000.0, "reference": {"kind": "at-full"}}]'::jsonb,
                              'paper', '{"whiteness": 0.9, "ink_density": 3.5228787452803374, "profile": null}'::jsonb)))
                          ELSE e.binding END
                     ORDER BY e.ordinal)
    FROM jsonb_array_elements(manifest->'bindings') WITH ORDINALITY AS e(binding, ordinal)))
WHERE jsonb_typeof(manifest->'bindings') = 'array'
  AND jsonb_path_exists(manifest, '$.bindings[*] ? (exists(@.mat) || !(exists(@.lighting)))');
