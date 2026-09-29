-- A single short-lived identify request per Player. The current authority epoch
-- fences requests across enrollment/session replacement; expiry is evaluated by
-- Central so no scheduled cleanup is needed for correctness.
CREATE TABLE player_output_identification (
    player_id TEXT PRIMARY KEY REFERENCES players(id) ON DELETE CASCADE,
    request_id UUID NOT NULL UNIQUE,
    output_id TEXT NOT NULL,
    authority_epoch INTEGER NOT NULL CHECK (authority_epoch >= 1),
    created_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL CHECK (expires_at > created_at),
    FOREIGN KEY (player_id, output_id) REFERENCES outputs(player_id, output_id)
);
CREATE INDEX player_output_identification_expiry ON player_output_identification(expires_at);
