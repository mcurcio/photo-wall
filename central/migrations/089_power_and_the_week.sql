-- Power and the week (roadmap 2a + 2b). Plain statements: no trigger and no stored procedure.

-- Settings › House (#2-#5): one row. No row is written here: Central seeds it at start from its own zone
-- (central/infra/house_store.py seed_house); with no row every reader answers that same seed value.
-- `zone_confirmed` is false until the operator saves Settings › House: Night off switches no display
-- before then, since Central's own zone need not be the house's.
CREATE TABLE house_settings (
  singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
  zone TEXT NOT NULL,
  latitude DOUBLE PRECISION CHECK (latitude BETWEEN -90 AND 90),
  longitude DOUBLE PRECISION CHECK (longitude BETWEEN -180 AND 180),
  units TEXT NOT NULL CHECK (units IN ('cm', 'in')),
  clock_format TEXT NOT NULL CHECK (clock_format IN ('12h', '24h')),
  updated_at DOUBLE PRECISION NOT NULL,
  zone_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
  CHECK ((latitude IS NULL) = (longitude IS NULL)));

-- Night off (#80-#83, #90): one row, validated by central.power.model.NightOff in app code. Seeded on:
-- 23:00 to 07:00, every day, every Frame there is now (chosen one by one), displays off, holds until
-- the next scheduled change; in force once the house's zone is confirmed (house_settings above).
CREATE TABLE night_off (
  singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
  settings JSONB NOT NULL,
  revision INTEGER NOT NULL CHECK (revision >= 1),
  updated_at DOUBLE PRECISION NOT NULL);
INSERT INTO night_off (singleton, settings, revision, updated_at)
SELECT TRUE, jsonb_build_object(
    'off', jsonb_build_object('at', 'clock', 'time', '23:00'),
    'on', jsonb_build_object('at', 'clock', 'time', '07:00'),
    'days', '["mon","tue","wed","thu","fri","sat","sun"]'::jsonb,
    'frames', jsonb_build_object('kind', 'frames',
                                 'frame_ids', COALESCE((SELECT jsonb_agg(id ORDER BY id COLLATE "C") FROM frames), '[]'::jsonb)),
    'darkness', 'display-off',
    'hold_lasts', 'next-scheduled-change'),
  1, EXTRACT(EPOCH FROM clock_timestamp());

-- Today's Programs end at deploy (frame "At deploy"; no backwards compatibility): they were single windows
-- timed in the browser's zone, which Central never recorded. Runs already admitted keep their snapshot end.
UPDATE runtime_state SET snapshot = jsonb_set(snapshot, '{programs}', '{}'::jsonb), revision = revision + 1;
