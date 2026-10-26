-- Room lights keep their brightness and follow their colour from Home Assistant (decision 0028,
-- amended 2026-10-25). A reference's facts stand until it states another: per reference, the last
-- stated brightness (or reading, or colour), when it was stated, and separately since when it can't
-- tell. Colour is its own fact. Colour sensors are their own setup item. Each light's colour is a
-- source: typed, its bulb, or a colour sensor. Galleria's birth (its version and the entity kinds it
-- publishes). The rules live in app code (central/room_light/policy.py, central/infra/); no trigger,
-- no stored procedure, every range pydantic's. No backwards compatibility in the data.
--
-- Upgrade data: a reported light's colour becomes "its bulb", its typed kelvin the old fixed kelvin
-- (6500 for an old reported colour, whose channel shares have no kelvin); a measured or fixed light's
-- fixed kelvin becomes typed. A light word that said "unavailable" keeps no state and can't tell
-- since it was heard. No old word carries a colour over: main's Galleria sent Home Assistant's rgb
-- in every mode (a colour worked out from color_temp too), so a bulb's colour stays "typed until its
-- bulb says" until Galleria states one in the new shape. A reading word likewise.
--
-- The new tables carry their audit fields (created and last changed: when and by whom), written by
-- central/infra/room_light_store.py: by the operator (a colour sensor), by the reference's source (a
-- colour sensor's facts) or by Galleria (its birth). There are no accounts, so "whom" is the actor.

-- Each light's colour source (central/room_light/model.py ReportedColourSource, SensedColourSource).
UPDATE room_lights SET light = jsonb_set(light, '{colour}', CASE
        WHEN light->'colour'->>'kind' = 'fixed' AND light->>'kind' = 'reported'
            THEN jsonb_build_object('kind', 'its-bulb', 'typed_kelvin', light->'colour'->'kelvin')
        WHEN light->>'kind' = 'reported'
            THEN '{"kind": "its-bulb", "typed_kelvin": 6500}'::jsonb
        ELSE jsonb_build_object('kind', 'typed', 'kelvin', light->'colour'->'kelvin') END);

-- Colour sensors by id (central/room_light/model.py ColourSensor, as JSON).
CREATE TABLE colour_sensors (
    id TEXT PRIMARY KEY,
    sensor JSONB NOT NULL CHECK (jsonb_typeof(sensor) = 'object'),
    created_at TIMESTAMPTZ NOT NULL,
    created_by TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL,
    changed_by TEXT NOT NULL);

-- A light reference's facts: its last stated Dimmer and when (NULL: never stated), its last stated
-- colour and when (NULL: never), since when it can't tell (NULL: it tells), when it was last heard.
ALTER TABLE light_words
    ADD COLUMN state JSONB NULL CHECK (state IS NULL OR jsonb_typeof(state) = 'object'),
    ADD COLUMN state_at TIMESTAMPTZ NULL,
    ADD COLUMN colour JSONB NULL CHECK (colour IS NULL OR jsonb_typeof(colour) = 'object'),
    ADD COLUMN colour_at TIMESTAMPTZ NULL,
    ADD COLUMN cant_tell_since TIMESTAMPTZ NULL;
UPDATE light_words SET
    state = CASE WHEN word->>'kind' IN ('dimmer', 'colour-dimmer')
                 THEN jsonb_build_object('kind', 'dimmer', 'on', word->'on', 'brightness', word->'brightness') END,
    state_at = CASE WHEN word->>'kind' IN ('dimmer', 'colour-dimmer') THEN heard_at END,
    cant_tell_since = CASE WHEN word->>'kind' = 'unavailable' THEN heard_at END;
ALTER TABLE light_words DROP COLUMN word;

-- A Sensor reference's facts: its last stated reading (contracts/room_light.py Measured) and when.
ALTER TABLE reading_words
    ADD COLUMN reading JSONB NULL CHECK (reading IS NULL OR jsonb_typeof(reading) = 'object'),
    ADD COLUMN reading_at TIMESTAMPTZ NULL,
    ADD COLUMN cant_tell_since TIMESTAMPTZ NULL;
UPDATE reading_words SET
    reading = CASE WHEN word->>'kind' = 'measured' THEN word END,
    reading_at = CASE WHEN word->>'kind' = 'measured' THEN heard_at END,
    cant_tell_since = CASE WHEN word->>'kind' = 'unavailable' THEN heard_at END;
ALTER TABLE reading_words DROP COLUMN word;

-- A colour sensor reference's facts: its last stated colour (contracts/room_light.py KelvinColour).
CREATE TABLE colour_words (
    source TEXT NOT NULL,
    address TEXT NOT NULL,
    colour JSONB NULL CHECK (colour IS NULL OR jsonb_typeof(colour) = 'object'),
    colour_at TIMESTAMPTZ NULL,
    cant_tell_since TIMESTAMPTZ NULL,
    heard_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    created_by TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL,
    changed_by TEXT NOT NULL,
    PRIMARY KEY (source, address));

-- Galleria's birth on Central's hub (contracts/home_assistant.py GalleriaBirth) and when Central
-- heard it: one row, none before a Galleria that sends one.
CREATE TABLE galleria_birth (
    one BOOLEAN PRIMARY KEY DEFAULT true CHECK (one),
    birth JSONB NOT NULL CHECK (jsonb_typeof(birth) = 'object'),
    heard_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    created_by TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL,
    changed_by TEXT NOT NULL);

-- Every Pi draws the new document shape once Central puts it (the change rises).
UPDATE room_lights_change SET change = change + 1 WHERE one;
