-- Roadmap 1b (slice C1): Displays recognised from each Output's EDID, and the Output document
-- Central writes per HDMI port. The rules live in app code (central/displays/model.py,
-- central/infra/display_store.py); there is no trigger and no stored procedure here.

-- A Display is keyed by exactly one of a usable serial (maker, product, serial: it follows the
-- display anywhere) or the Frame it feeds (maker, product, frame_id: no usable serial).
CREATE TABLE displays (
    id UUID PRIMARY KEY,
    maker TEXT NOT NULL CHECK (maker ~ '^[A-Z]{3}$'),
    product INTEGER NOT NULL CHECK (product BETWEEN 0 AND 65535),
    name TEXT NOT NULL,
    serial TEXT NULL,
    frame_id TEXT NULL REFERENCES frames(id) ON DELETE CASCADE,
    modes JSONB NOT NULL DEFAULT '[]',
    power_method TEXT NULL CHECK (power_method IN ('hdmi-cec', 'ddc-ci', 'signal-off')),
    switch_input_on_power_on BOOLEAN NOT NULL DEFAULT TRUE,
    never_off_on_other_input BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen_at DOUBLE PRECISION NOT NULL,
    CHECK ((serial IS NULL) <> (frame_id IS NULL))
);
CREATE UNIQUE INDEX displays_by_serial ON displays (maker, product, serial) WHERE serial IS NOT NULL;
CREATE UNIQUE INDEX displays_by_frame ON displays (maker, product, frame_id) WHERE frame_id IS NOT NULL;

-- A serial two Outputs reported at once: never usable again for that maker and product.
CREATE TABLE shared_serials (
    maker TEXT,
    product INTEGER,
    serial TEXT,
    found_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (maker, product, serial)
);

-- What each Output last reported: `identity` is the last non-null identity (kept through an
-- unplug), `display_id` the Display recognised there (NULL: pending or never seen), `report` the
-- latest report as received.
CREATE TABLE output_displays (
    player_id TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    output_id TEXT NOT NULL,
    connected BOOLEAN NOT NULL,
    identity JSONB NULL,
    display_id UUID NULL REFERENCES displays(id) ON DELETE SET NULL,
    report JSONB NOT NULL,
    reported_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (player_id, output_id)
);

-- Central's last Output document per Output, written only by the document source: `change` rises
-- only when the body's digest differs.
CREATE TABLE output_documents (
    player_id TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    output_id TEXT NOT NULL,
    change BIGINT NOT NULL CHECK (change >= 1),
    digest TEXT NOT NULL,
    document JSONB NOT NULL,
    PRIMARY KEY (player_id, output_id)
);
