CREATE TABLE media_settings (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
    recipe_id TEXT,
    max_bytes BIGINT NOT NULL CHECK(max_bytes>0),
    worker_seen DOUBLE PRECISION,
    worker_error TEXT
);
CREATE TABLE media_sources (
    source_ref TEXT PRIMARY KEY,
    spec JSONB NOT NULL,
    generation BIGINT NOT NULL DEFAULT 0,
    next_refresh DOUBLE PRECISION NOT NULL DEFAULT 0,
    refresh_started DOUBLE PRECISION,
    last_success DOUBLE PRECISION,
    status TEXT NOT NULL DEFAULT 'unavailable',
    diagnostics JSONB NOT NULL DEFAULT '[]',
    counts JSONB NOT NULL DEFAULT '{}'
);
CREATE TABLE asset_revisions (
    asset_id TEXT PRIMARY KEY,
    metadata JSONB NOT NULL,
    first_seen DOUBLE PRECISION NOT NULL,
    last_seen DOUBLE PRECISION NOT NULL
);
CREATE TABLE source_members (
    source_ref TEXT NOT NULL REFERENCES media_sources(source_ref),
    asset_id TEXT NOT NULL REFERENCES asset_revisions(asset_id),
    PRIMARY KEY(source_ref,asset_id)
);
CREATE TABLE media_jobs (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES asset_revisions(asset_id),
    recipe_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','running','publishing','retry','ready','failed','cleanup')),
    attempt INTEGER NOT NULL DEFAULT 0,
    attempt_token TEXT,
    lease_until DOUBLE PRECISION,
    reserved_bytes BIGINT NOT NULL DEFAULT 0 CHECK(reserved_bytes>=0),
    retry_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    failure_code TEXT,
    earliest_start DOUBLE PRECISION NOT NULL,
    variant_sha TEXT REFERENCES media_blobs(digest),
    updated_at DOUBLE PRECISION NOT NULL,
    UNIQUE(asset_id,recipe_id)
);
CREATE INDEX media_jobs_queue ON media_jobs(state,retry_at,earliest_start);
