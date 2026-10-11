-- Roadmap 1c (slice C1): picture settings on the Display, and every Frame on the Cover fit.
-- The rules live in app code (central/displays/picture.py, central/infra/display_store.py,
-- central/registry.py); there is no trigger and no stored procedure here.

-- The Display's four picture settings (#111-#114). A NULL value is "as the display has it"
-- (nothing written, nothing adjusted); a NULL acts_on is "not placed yet" (no probe seen).
ALTER TABLE displays
    ADD COLUMN brightness_acts_on TEXT NULL CHECK (brightness_acts_on IN ('display', 'picture-adjustment')),
    ADD COLUMN brightness INTEGER NULL CHECK (brightness BETWEEN 0 AND 100),
    ADD COLUMN contrast_acts_on TEXT NULL CHECK (contrast_acts_on IN ('display', 'picture-adjustment')),
    ADD COLUMN contrast INTEGER NULL CHECK (contrast BETWEEN 0 AND 100),
    ADD COLUMN colour_temperature_kelvin INTEGER NOT NULL DEFAULT 6500
        CHECK (colour_temperature_kelvin BETWEEN 3000 AND 9300 AND colour_temperature_kelvin % 100 = 0),
    ADD COLUMN gamma_tenths INTEGER NOT NULL DEFAULT 22 CHECK (gamma_tenths BETWEEN 18 AND 26);

-- One open picture session per Display (the Live model). `draft` is a DisplayPicture under the
-- stored columns' names; `idle_until` is Central's clock, compared only with itself.
CREATE TABLE picture_sessions (
    id UUID PRIMARY KEY,
    display_id UUID NOT NULL UNIQUE REFERENCES displays(id) ON DELETE CASCADE,
    frame_id TEXT NOT NULL REFERENCES frames(id) ON DELETE CASCADE,
    sequence BIGINT NOT NULL CHECK (sequence >= 1),
    draft JSONB NOT NULL,
    before_brightness INTEGER NULL CHECK (before_brightness BETWEEN 0 AND 100),
    before_contrast INTEGER NULL CHECK (before_contrast BETWEEN 0 AND 100),
    touched TEXT[] NOT NULL DEFAULT '{}',
    idle_until DOUBLE PRECISION NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);

-- The picture part's own change number: it rises only when the picture part's digest differs.
ALTER TABLE output_documents
    ADD COLUMN picture_change BIGINT NULL CHECK (picture_change >= 1),
    ADD COLUMN picture_digest TEXT NULL;

-- 1b's gain becomes the Display's brightness (central/displays/picture.py, "Brightness from 1b's
-- gain"): gain 1.0 stays unset and unplaced; any other gain is a picture-adjustment brightness of
-- round(gain * 50), on the Display the Frame's Position names, else the Display last seen on its
-- bound Output. A Frame with neither loses its gain; when two Frames give one Display a gain, the
-- higher wins.
WITH gains AS (
    SELECT COALESCE(f.position_display_id, seen.display_id) AS display_id,
           max(COALESCE((f.calibration->>'gain')::double precision, 1)) AS gain
    FROM frames f
    LEFT JOIN bindings b ON b.frame_id = f.id
    LEFT JOIN output_displays seen ON seen.player_id = b.player_id AND seen.output_id = b.output_id
    WHERE COALESCE((f.calibration->>'gain')::double precision, 1) <> 1
      AND COALESCE(f.position_display_id, seen.display_id) IS NOT NULL
    GROUP BY 1
)
UPDATE displays d SET brightness_acts_on = 'picture-adjustment', brightness = round(g.gain * 50)::integer
FROM gains g WHERE d.id = g.display_id;

-- Every Frame moves to Cover (owner q1, 2026-10-10): its corners become the opening and its
-- rotation resets to 0 (owner, 2026-10-10: "Every frame should reset to 0 rotation"); trim (crop,
-- owner q3) and gain are dropped; the revision rises, and so does the configuration revision so
-- every Player fetches the new position.
UPDATE frames SET
    calibration = jsonb_build_object(
        'revision', COALESCE((calibration->>'revision')::bigint, 1) + 1,
        'fit', jsonb_build_object(
            'mode', 'cover',
            'corners', COALESCE(calibration->'corners', '[[0, 0], [1, 0], [1, 1], [0, 1]]'::jsonb),
            'rotation', 0)),
    configuration_revision = configuration_revision + 1;

-- The stored copies of each Frame's Position (every binding in a Player's last configuration and in
-- each plan offer) take the Frame's new one, so the strict 1c models still read them; a binding
-- whose Frame is gone takes the default. Its configuration revision stays, so Central still sees a
-- change and issues the next configuration.
UPDATE player_configurations pc SET configuration = jsonb_set(pc.configuration, '{bindings}', (
    SELECT COALESCE(jsonb_agg(CASE WHEN f.calibration IS NULL THEN e.binding - 'calibration'
                                   ELSE jsonb_set(e.binding, '{calibration}', f.calibration) END
                              ORDER BY e.ordinal), '[]'::jsonb)
    FROM jsonb_array_elements(pc.configuration->'bindings') WITH ORDINALITY AS e(binding, ordinal)
    LEFT JOIN frames f ON f.id = e.binding->>'frame_id'))
WHERE jsonb_typeof(pc.configuration->'bindings') = 'array';

UPDATE plan_offers po SET manifest = jsonb_set(po.manifest, '{bindings}', (
    SELECT COALESCE(jsonb_agg(CASE WHEN f.calibration IS NULL THEN e.binding - 'calibration'
                                   ELSE jsonb_set(e.binding, '{calibration}', f.calibration) END
                              ORDER BY e.ordinal), '[]'::jsonb)
    FROM jsonb_array_elements(po.manifest->'bindings') WITH ORDINALITY AS e(binding, ordinal)
    LEFT JOIN frames f ON f.id = e.binding->>'frame_id'))
WHERE jsonb_typeof(po.manifest->'bindings') = 'array';

-- A Pi's last Output report and Central's last Output document as 1b stored them have no picture
-- part; the 1c decoders need one. A report gets "no picture seen" (what a Pi without a picture
-- document reports); a document gets the neutral picture part at picture change 1, and no picture
-- digest, so the next projection raises the change and sends the Display's real settings.
UPDATE output_displays SET report = report || '{"picture": null}'::jsonb
WHERE NOT report ? 'picture';

UPDATE output_documents SET picture_change = 1, document = document || jsonb_build_object(
    'picture_change', 1,
    'picture', jsonb_build_object(
        'brightness', jsonb_build_object('acts_on', 'picture-adjustment', 'value', 'as-the-display-has-it'),
        'contrast', jsonb_build_object('acts_on', 'picture-adjustment', 'value', 'as-the-display-has-it'),
        'colour_temperature_kelvin', 6500,
        'gamma_tenths', 22))
WHERE NOT document ? 'picture';

-- Readiness (owner q1's accepted cost; run ledger F-1): each Frame's picture changed, so none is
-- ready until its Position is checked again. This one statement is the only thing that makes a
-- migrated Frame not ready.
UPDATE frames SET position_generation = NULL;

-- A trial in flight was made against the old position shape.
UPDATE node_calibration_trials SET state = 'expired' WHERE state = 'active';

-- The V1 calibration route's preview is gone with the route.
ALTER TABLE frames DROP COLUMN preview, DROP COLUMN preview_expires;
