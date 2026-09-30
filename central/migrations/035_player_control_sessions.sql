-- Application-control negotiation is per Player authority epoch. Existing epochs
-- have no row and are treated as sealed legacy; enrollment creates an open row.
CREATE TABLE player_control_sessions (
    player_id TEXT PRIMARY KEY REFERENCES players(id) ON DELETE CASCADE,
    authority_epoch INTEGER NOT NULL CHECK (authority_epoch >= 1),
    status TEXT NOT NULL CHECK (status IN ('open', 'legacy', 'negotiated')),
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version IN (1, 2)),
    capabilities JSONB NOT NULL DEFAULT '[]',
    offered_schemas JSONB,
    offered_capabilities JSONB,
    issued_sequence BIGINT NOT NULL DEFAULT 0 CHECK (issued_sequence >= 0),
    pending_id TEXT,
    pending_digest TEXT,
    pending_expires DOUBLE PRECISION,
    applied_sequence BIGINT NOT NULL DEFAULT 0 CHECK (applied_sequence >= 0),
    applied_at DOUBLE PRECISION,
    applied_delivery_id TEXT,
    applied_digest TEXT,
    last_delivery_id TEXT,
    last_result_sequence BIGINT CHECK (last_result_sequence >= 1),
    last_result_digest TEXT,
    last_result TEXT,
    last_result_at DOUBLE PRECISION
);
