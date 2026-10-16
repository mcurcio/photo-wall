-- Room light on the wall (decision 0028; owner, 2026-10-15: "at all times, i want the display to
-- appear as a natural printed photo"). Room lights and Sensors, observed, never driven; the Home
-- Assistant link's state and its refused messages; the Display's light replaces the print look's
-- sliders and its LUT (Q2 "the paper replaces them"); a Frame's lighting replaces its mat (Q3 "on the
-- Frame"). The rules live in app code (central/room_light/, central/infra/room_light_store.py,
-- central/infra/home_assistant.py, central/displays/picture.py, central/fleet/node_calibration.py);
-- there is no trigger and no stored procedure here. No backwards compatibility in the data.
--
-- Upgrade data, so every picture equals today's at neutral sliders: each Display's light is the
-- placeholder contracts/node_picture.py UPGRADE_DISPLAY_LIGHT (1000 lx matched, black 0.0003 of
-- white); one fixed room light "room" exists; each Frame's lighting is
-- central/room_light/policy.py upgrade_lighting(UPGRADE_DISPLAY_LIGHT, its mat's depth): one wash of
-- "room" at 1000 lx, dimmer-at-full, and paper of whiteness 0.9 and ink -log10(0.0003). The old mat
-- lights' relative strengths have no lux, so they are not carried (0028). The Display's software
-- brightness multiplied the linear picture, so a Frame keeps it as its wash's lux: 1000 x brightness
-- / 100 lx (0 % becomes 0.1 lx: a light is never 0 lx), from the Display its Position names, else the
-- one last seen on its bound Output; with neither, 1000 lx. The Display's LUT is not carried: its
-- name needs the recipe Central bakes with, which is app code's (0028's costs).

-- Room lights: each one's single reporter and colour (central/room_light/model.py RoomLight, as JSON),
-- its last state (contracts/room_light.py's states) and when Central recorded it; `position` keeps
-- the House > Lights order.
CREATE TABLE room_lights (
    id TEXT PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9-]{0,31}$'),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 64),
    reporter JSONB NOT NULL CHECK (jsonb_typeof(reporter) = 'object'),
    colour JSONB NOT NULL CHECK (jsonb_typeof(colour) = 'object'),
    state JSONB NOT NULL CHECK (jsonb_typeof(state) = 'object'),
    heard_at TIMESTAMPTZ NULL,
    position INTEGER NOT NULL);

-- Sensors: a light meter at one point (a Pi's wired meter, or a Home Assistant illuminance entity)
-- and its last reading (`{"lux": n}` or `{"lux": null}` for unavailable; NULL: never heard).
CREATE TABLE sensors (
    id TEXT PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9-]{0,31}$'),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 64),
    at JSONB NOT NULL CHECK (jsonb_typeof(at) = 'object'),
    reading JSONB NULL CHECK (reading IS NULL OR jsonb_typeof(reading) = 'object'),
    heard_at TIMESTAMPTZ NULL);

-- The room-lights document's `change`: raised in the same transaction as any change below it.
CREATE TABLE room_lights_change (
    one BOOLEAN PRIMARY KEY DEFAULT true CHECK (one),
    change BIGINT NOT NULL CHECK (change >= 1));

-- The worker's broker link: connected or away since when (no row: never heard; with no
-- PHOTO_WALL_MQTT_URL the worker writes no row and the page says not configured).
CREATE TABLE home_assistant_link (
    one BOOLEAN PRIMARY KEY DEFAULT true CHECK (one),
    state TEXT NOT NULL CHECK (state IN ('not-configured', 'connected', 'away')),
    since TIMESTAMPTZ NULL);

-- The latest refused messages (the writer keeps the newest 20).
CREATE TABLE home_assistant_refusals (
    id BIGSERIAL PRIMARY KEY,
    topic TEXT NOT NULL,
    reason TEXT NOT NULL,
    at TIMESTAMPTZ NOT NULL);

INSERT INTO room_lights (id, name, reporter, colour, state, heard_at, position) VALUES
    ('room', 'Room', '{"kind": "fixed"}'::jsonb, '{"kind": "fixed", "kelvin": 6500}'::jsonb,
     '{"kind": "dimmer", "on": true, "brightness": 1.0, "rgb": null}'::jsonb, NULL, 0);
INSERT INTO room_lights_change (one, change) VALUES (true, 1);

-- Each Frame's wash, from its Display's software brightness, before the column goes.
CREATE TEMP TABLE upgrade_wash ON COMMIT DROP AS
    SELECT f.id AS frame_id, GREATEST(0.1, 10.0 * d.software_brightness_percent)::double precision AS lux
    FROM frames f
    LEFT JOIN bindings b ON b.frame_id = f.id
    LEFT JOIN output_displays seen ON seen.player_id = b.player_id AND seen.output_id = b.output_id
    JOIN displays d ON d.id = COALESCE(f.position_display_id, seen.display_id);

-- The Display's light (contracts/node_picture.py DisplayLight); the print look's sliders and the
-- Display's LUT go (the paper profile moves to the Frame's paper; a LUT a Frame's paper names cannot
-- be removed: central/infra/lut_store.py). New Displays get UPGRADE_DISPLAY_LIGHT from app code.
ALTER TABLE displays
    ADD COLUMN matched_lux DOUBLE PRECISION NULL CHECK (matched_lux BETWEEN 10.0 AND 20000.0),
    ADD COLUMN panel_black DOUBLE PRECISION NULL CHECK (panel_black BETWEEN 0.00001 AND 0.05);
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
        'kind', 'wash', 'light', 'room', 'reference', 'dimmer-at-full',
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
        'lights', '[{"kind": "wash", "light": "room", "lux": 1000.0, "reference": "dimmer-at-full"}]'::jsonb,
        'paper', '{"whiteness": 0.9, "ink_density": 3.5228787452803374, "profile": null}'::jsonb),
    saved_lighting = CASE WHEN saved_mat IS NULL THEN NULL ELSE jsonb_build_object(
        'mat_depth_mm', COALESCE((saved_mat->>'depth_mm')::double precision, 0.0),
        'lights', '[{"kind": "wash", "light": "room", "lux": 1000.0, "reference": "dimmer-at-full"}]'::jsonb,
        'paper', '{"whiteness": 0.9, "ink_density": 3.5228787452803374, "profile": null}'::jsonb) END;
ALTER TABLE node_calibration_trials
    ALTER COLUMN lighting SET NOT NULL,
    DROP COLUMN mat,
    DROP COLUMN saved_mat;

-- Position history: each entry's mat becomes the lighting it upgrades to.
ALTER TABLE position_entries ADD COLUMN lighting JSONB NULL CHECK (jsonb_typeof(lighting) = 'object');
UPDATE position_entries SET lighting = jsonb_build_object(
    'mat_depth_mm', COALESCE((mat->>'depth_mm')::double precision, 0.0),
    'lights', '[{"kind": "wash", "light": "room", "lux": 1000.0, "reference": "dimmer-at-full"}]'::jsonb,
    'paper', '{"whiteness": 0.9, "ink_density": 3.5228787452803374, "profile": null}'::jsonb);
ALTER TABLE position_entries ALTER COLUMN lighting SET NOT NULL, DROP COLUMN mat;

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
                              'lights', '[{"kind": "wash", "light": "room", "lux": 1000.0, "reference": "dimmer-at-full"}]'::jsonb,
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
                              'lights', '[{"kind": "wash", "light": "room", "lux": 1000.0, "reference": "dimmer-at-full"}]'::jsonb,
                              'paper', '{"whiteness": 0.9, "ink_density": 3.5228787452803374, "profile": null}'::jsonb)))
                          ELSE e.binding END
                     ORDER BY e.ordinal)
    FROM jsonb_array_elements(manifest->'bindings') WITH ORDINALITY AS e(binding, ordinal)))
WHERE jsonb_typeof(manifest->'bindings') = 'array'
  AND jsonb_path_exists(manifest, '$.bindings[*] ? (exists(@.mat) || !(exists(@.lighting)))');
