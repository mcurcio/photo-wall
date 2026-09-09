CREATE TABLE IF NOT EXISTS players (
    id TEXT PRIMARY KEY,
    public_key TEXT UNIQUE NOT NULL,
    token_hash TEXT UNIQUE NOT NULL,
    authority_epoch INTEGER NOT NULL DEFAULT 1,
    registered_at DOUBLE PRECISION NOT NULL,
    last_seen DOUBLE PRECISION NOT NULL,
    retired_at DOUBLE PRECISION,
    health JSONB NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS enrollment_nonces (
    nonce TEXT PRIMARY KEY,
    public_key TEXT NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS outputs (
    player_id TEXT NOT NULL REFERENCES players(id),
    output_id TEXT NOT NULL,
    observation JSONB NOT NULL,
    PRIMARY KEY(player_id, output_id)
);
CREATE TABLE IF NOT EXISTS frames (
    id TEXT PRIMARY KEY,
    surface_id TEXT NOT NULL,
    x_mm DOUBLE PRECISION NOT NULL,
    y_mm DOUBLE PRECISION NOT NULL,
    width_mm DOUBLE PRECISION NOT NULL CHECK(width_mm > 0),
    height_mm DOUBLE PRECISION NOT NULL CHECK(height_mm > 0),
    profile JSONB NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0,
    calibration JSONB NOT NULL,
    calibration_valid BOOLEAN NOT NULL DEFAULT FALSE,
    preview JSONB,
    preview_expires DOUBLE PRECISION
);
CREATE TABLE IF NOT EXISTS bindings (
    frame_id TEXT PRIMARY KEY REFERENCES frames(id),
    player_id TEXT NOT NULL,
    output_id TEXT NOT NULL,
    UNIQUE(player_id, output_id),
    FOREIGN KEY(player_id, output_id) REFERENCES outputs(player_id, output_id)
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence BIGSERIAL PRIMARY KEY,
    occurred_at DOUBLE PRECISION NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    detail JSONB NOT NULL DEFAULT '{}'
);
