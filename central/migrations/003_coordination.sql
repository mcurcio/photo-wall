CREATE TABLE runtime_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    revision BIGINT NOT NULL,
    snapshot JSONB NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE player_configurations (
    player_id TEXT PRIMARY KEY REFERENCES players(id),
    authority_epoch INTEGER NOT NULL,
    revision BIGINT NOT NULL,
    configuration JSONB NOT NULL
);
CREATE TABLE plan_offers (
    player_id TEXT NOT NULL REFERENCES players(id),
    authority_epoch INTEGER NOT NULL,
    revision BIGINT NOT NULL,
    plan_id TEXT NOT NULL,
    manifest JSONB NOT NULL,
    groups JSONB NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    valid_until DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(player_id, authority_epoch, revision)
);
CREATE TABLE player_feedback (
    player_id TEXT NOT NULL REFERENCES players(id),
    authority_epoch INTEGER NOT NULL,
    sequence BIGINT NOT NULL,
    received_at DOUBLE PRECISION NOT NULL,
    readiness JSONB NOT NULL,
    PRIMARY KEY(player_id, authority_epoch)
);
CREATE TABLE assignment_locks (
    player_id TEXT NOT NULL REFERENCES players(id),
    authority_epoch INTEGER NOT NULL,
    assignment_id TEXT NOT NULL,
    layer JSONB NOT NULL,
    secured_at DOUBLE PRECISION NOT NULL,
    valid_until DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(player_id, authority_epoch, assignment_id)
);
CREATE TABLE coordination_groups (
    id TEXT PRIMARY KEY,
    members JSONB NOT NULL,
    starts_at DOUBLE PRECISION NOT NULL,
    deadline DOUBLE PRECISION NOT NULL,
    valid_until DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','committed','skipped'))
);
CREATE TABLE execution_commits (
    player_id TEXT NOT NULL REFERENCES players(id),
    authority_epoch INTEGER NOT NULL,
    revision BIGINT NOT NULL,
    assignment_id TEXT NOT NULL,
    group_id TEXT NOT NULL REFERENCES coordination_groups(id),
    committed_at DOUBLE PRECISION NOT NULL,
    valid BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY(player_id, authority_epoch, revision, assignment_id),
    FOREIGN KEY(player_id, authority_epoch, revision)
      REFERENCES plan_offers(player_id, authority_epoch, revision)
);
CREATE TABLE execution_events (
    sequence BIGSERIAL PRIMARY KEY,
    occurred_at DOUBLE PRECISION NOT NULL,
    player_id TEXT,
    assignment_id TEXT,
    kind TEXT NOT NULL,
    detail JSONB NOT NULL DEFAULT '{}'
);
CREATE TABLE catalog_snapshots (
    source_ref TEXT PRIMARY KEY,
    snapshot JSONB NOT NULL
);
CREATE TABLE authored_candidates (
    asset_id TEXT PRIMARY KEY,
    candidate JSONB NOT NULL
);
CREATE TABLE media_blobs (
    digest TEXT PRIMARY KEY CHECK(digest ~ '^[a-f0-9]{64}$'),
    variant JSONB NOT NULL,
    size BIGINT NOT NULL CHECK(size > 0),
    state TEXT NOT NULL CHECK(state IN ('publishing','ready','corrupt','deleting')),
    created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE media_references (
    owner TEXT NOT NULL,
    digest TEXT NOT NULL REFERENCES media_blobs(digest),
    expires_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(owner,digest)
);
CREATE INDEX media_references_digest ON media_references(digest,expires_at);
