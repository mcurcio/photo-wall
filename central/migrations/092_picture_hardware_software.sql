-- The picture split (decision 0022): a Display's own settings and Photo Wall's, each on its own.
-- The rules live in app code (central/displays/picture.py, hardware.py, central/infra/display_store.py);
-- there is no trigger and no stored procedure here. No backwards compatibility in the data.

-- The two groups on the Display. Hardware: only the display settings set in Photo Wall, by MCCS
-- code ({"10": 46}). Software: Photo Wall's brightness and contrast 0-150 % (100 changes nothing);
-- colour temperature and gamma keep their columns. hardware_restore: a Revert's one-time
-- instruction to put a display setting back ({"10": 60}), never a stored value; it rides the
-- picture document until the Pi answers it.
ALTER TABLE displays
    ADD COLUMN hardware_picture JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(hardware_picture) = 'object'),
    ADD COLUMN hardware_restore JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(hardware_restore) = 'object'),
    ADD COLUMN software_brightness_percent INTEGER NOT NULL DEFAULT 100 CHECK (software_brightness_percent BETWEEN 0 AND 150),
    ADD COLUMN software_contrast_percent INTEGER NOT NULL DEFAULT 100 CHECK (software_contrast_percent BETWEEN 0 AND 150);

-- 1c's picture adjustment counted 50 % as no change; now 100 % does: twice the old value, capped
-- at 150 %. A value that acted on the display is forgotten (the display keeps it).
UPDATE displays SET software_brightness_percent = LEAST(150, 2 * brightness)
    WHERE brightness_acts_on = 'picture-adjustment' AND brightness IS NOT NULL;
UPDATE displays SET software_contrast_percent = LEAST(150, 2 * contrast)
    WHERE contrast_acts_on = 'picture-adjustment' AND contrast IS NOT NULL;
ALTER TABLE displays DROP COLUMN brightness_acts_on, DROP COLUMN brightness,
                     DROP COLUMN contrast_acts_on, DROP COLUMN contrast;

-- Open picture sessions are dropped: their drafts are 1c's shape. A session's draft is a
-- DisplayPicture under the displays columns' names.
DELETE FROM picture_sessions;
ALTER TABLE picture_sessions DROP COLUMN before_brightness, DROP COLUMN before_contrast, DROP COLUMN touched,
    ADD COLUMN began_at_change BIGINT NOT NULL CHECK (began_at_change >= 0),
    ADD COLUMN hardware_before JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN touched_hardware TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN touched_software TEXT[] NOT NULL DEFAULT '{}';

-- Each Output's picture document, on its own key with its own change number.
CREATE TABLE picture_documents (
    player_id TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    output_id TEXT NOT NULL,
    picture_change BIGINT NOT NULL CHECK (picture_change >= 1),
    digest TEXT NOT NULL,
    document JSONB NOT NULL,
    PRIMARY KEY (player_id, output_id));

-- The numbering carries on; the '' digest makes the next read rewrite the document.
INSERT INTO picture_documents (player_id, output_id, picture_change, digest, document)
    SELECT player_id, output_id, picture_change, '', '{}'::jsonb FROM output_documents WHERE picture_change IS NOT NULL;
ALTER TABLE output_documents DROP COLUMN picture_change, DROP COLUMN picture_digest;

-- The Output document and report lose their picture part (the display line's schema major 4).
UPDATE output_documents SET document = document - 'picture_change' - 'picture';
UPDATE output_displays SET report = report - 'picture';

-- Each Output's latest picture report, as the Pi put it.
CREATE TABLE picture_reports (
    player_id TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    output_id TEXT NOT NULL,
    report JSONB NOT NULL,
    reported_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (player_id, output_id));
