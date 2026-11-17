-- Decision 0034, S2d. Room lights: the setup is one document, `singletons` 'room_light_setup'
-- (central/infra/room_light_rows.py _RoomLightSetup: lights by id with their position, Sensors and
-- colour sensors by id, the room-lights document's change number), whose row is also the house's
-- lock; facts are kept for named references only, in one table, `room_light_facts` (one row per
-- (kind, source, address): the kind's model as JSON beside when its source last stated it);
-- refused reports become `events` (kind 'refusal', kept a week); Galleria's birth becomes the
-- `singletons` document 'galleria_birth'; what each Player drew with is read from its line in
-- `node_states`, never copied. paper_profiles gains its audit fields. Written only by
-- central/infra/room_light_store.py, central/infra/events.py and central/infra/paper_profile_store.py.
-- Times are epoch seconds on the database's clock. Plain statements, no trigger or function,
-- forward-only.
CREATE TABLE room_light_facts (
    kind TEXT NOT NULL CHECK (kind IN ('light', 'reading', 'colour')),
    source TEXT NOT NULL,
    address TEXT NOT NULL,
    facts JSONB NOT NULL CHECK (jsonb_typeof(facts) = 'object'),   -- LightFacts | ReadingFacts | ColourFacts, without heard_at
    heard_at DOUBLE PRECISION NOT NULL,                             -- when its source last stated it
    created_at DOUBLE PRECISION NOT NULL, created_by TEXT NOT NULL,
    changed_at DOUBLE PRECISION NOT NULL, changed_by TEXT NOT NULL,
    PRIMARY KEY (kind, source, address));

-- The setup, one document; its writer before this release is unknown.
INSERT INTO singletons (name, document, revision, created_at, created_by, changed_at, changed_by)
SELECT 'room_light_setup',
       jsonb_build_object(
           'lights', coalesce((SELECT jsonb_object_agg(id, jsonb_build_object('light', light, 'position', position))
                               FROM room_lights), '{}'::jsonb),
           'sensors', coalesce((SELECT jsonb_object_agg(id, sensor) FROM sensors), '{}'::jsonb),
           'colour_sensors', coalesce((SELECT jsonb_object_agg(id, sensor) FROM colour_sensors), '{}'::jsonb),
           'change', change),
       1, EXTRACT(EPOCH FROM clock_timestamp()), 'unknown', EXTRACT(EPOCH FROM clock_timestamp()), 'unknown'
FROM room_lights_change WHERE one;

-- Facts, for the references the setup names only: a light's reporter (its colour source "its bulb"
-- reads the same facts), a Sensor's reference, a colour sensor's reference.
INSERT INTO room_light_facts (kind, source, address, facts, heard_at, created_at, created_by, changed_at, changed_by)
SELECT 'light', w.source, w.address,
       jsonb_build_object(
           'state', CASE WHEN w.state IS NULL THEN NULL
                         ELSE jsonb_build_object('value', w.state, 'at', EXTRACT(EPOCH FROM w.state_at)) END,
           'colour', CASE WHEN w.colour IS NULL THEN NULL
                          ELSE jsonb_build_object('value', w.colour, 'at', EXTRACT(EPOCH FROM w.colour_at)) END,
           'doubt', CASE WHEN w.doubt_reason IS NULL THEN NULL
                         ELSE jsonb_build_object('reason', w.doubt_reason, 'since', EXTRACT(EPOCH FROM w.doubt_since)) END),
       EXTRACT(EPOCH FROM w.heard_at), EXTRACT(EPOCH FROM w.created_at), w.created_by,
       EXTRACT(EPOCH FROM w.changed_at), w.changed_by
FROM light_words w
WHERE EXISTS (SELECT 1 FROM room_lights l WHERE l.light->>'kind' = 'reported'
                AND l.light->'reporter'->>'source' = w.source AND l.light->'reporter'->>'address' = w.address);

INSERT INTO room_light_facts (kind, source, address, facts, heard_at, created_at, created_by, changed_at, changed_by)
SELECT 'reading', w.source, w.address,
       jsonb_build_object(
           'reading', CASE WHEN w.reading IS NULL THEN NULL
                           ELSE jsonb_build_object('value', w.reading, 'at', EXTRACT(EPOCH FROM w.reading_at)) END,
           'doubt', CASE WHEN w.doubt_reason IS NULL THEN NULL
                         ELSE jsonb_build_object('reason', w.doubt_reason, 'since', EXTRACT(EPOCH FROM w.doubt_since)) END),
       EXTRACT(EPOCH FROM w.heard_at), EXTRACT(EPOCH FROM w.created_at), w.created_by,
       EXTRACT(EPOCH FROM w.changed_at), w.changed_by
FROM reading_words w
WHERE EXISTS (SELECT 1 FROM sensors s
              WHERE s.sensor->'at'->>'source' = w.source AND s.sensor->'at'->>'address' = w.address);

INSERT INTO room_light_facts (kind, source, address, facts, heard_at, created_at, created_by, changed_at, changed_by)
SELECT 'colour', w.source, w.address,
       jsonb_build_object(
           'colour', CASE WHEN w.colour IS NULL THEN NULL
                          ELSE jsonb_build_object('value', w.colour, 'at', EXTRACT(EPOCH FROM w.colour_at)) END,
           'doubt', CASE WHEN w.doubt_reason IS NULL THEN NULL
                         ELSE jsonb_build_object('reason', w.doubt_reason, 'since', EXTRACT(EPOCH FROM w.doubt_since)) END),
       EXTRACT(EPOCH FROM w.heard_at), EXTRACT(EPOCH FROM w.created_at), w.created_by,
       EXTRACT(EPOCH FROM w.changed_at), w.changed_by
FROM colour_words w
WHERE EXISTS (SELECT 1 FROM colour_sensors s
              WHERE s.sensor->'at'->>'source' = w.source AND s.sensor->'at'->>'address' = w.address);

-- Galleria's birth, with its audit columns.
INSERT INTO singletons (name, document, revision, created_at, created_by, changed_at, changed_by)
SELECT 'galleria_birth', jsonb_build_object('birth', birth, 'heard_at', EXTRACT(EPOCH FROM heard_at)),
       1, EXTRACT(EPOCH FROM created_at), created_by, EXTRACT(EPOCH FROM changed_at), changed_by
FROM galleria_birth WHERE one;

-- The refusal log joins the one event log (kept a week).
INSERT INTO events (kind, body, recorded_at, recorded_by)
SELECT 'refusal', jsonb_build_object('source', source, 'address', address, 'reason', reason),
       EXTRACT(EPOCH FROM at), 'central'
FROM report_refusals ORDER BY id;

-- Paper profiles' audit fields; their writer before this release is unknown.
ALTER TABLE paper_profiles
    ADD COLUMN created_at DOUBLE PRECISION NULL, ADD COLUMN created_by TEXT NULL,
    ADD COLUMN changed_at DOUBLE PRECISION NULL, ADD COLUMN changed_by TEXT NULL;
UPDATE paper_profiles SET created_at = added_at, created_by = 'unknown', changed_at = added_at, changed_by = 'unknown';
ALTER TABLE paper_profiles
    ALTER COLUMN created_at SET NOT NULL, ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN changed_at SET NOT NULL, ALTER COLUMN changed_by SET NOT NULL;

DROP TABLE room_lights, sensors, colour_sensors, room_lights_change, light_words, reading_words,
           colour_words, room_lights_drawn, report_refusals, galleria_birth;
