-- A drain is an admission fence, not an OS command or a report of physical stop.
CREATE TABLE equipment_drains (
    player_id TEXT PRIMARY KEY REFERENCES players(id),
    attempt_id TEXT NOT NULL,
    boot_id TEXT NOT NULL,
    authority_epoch INTEGER NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('prepared', 'stop_committed', 'aborted')),
    prepared_at DOUBLE PRECISION NOT NULL,
    authorization_expires_at DOUBLE PRECISION NOT NULL,
    stop_committed_at DOUBLE PRECISION,
    aborted_at DOUBLE PRECISION,
    snapshot JSONB NOT NULL,
    CHECK ((phase = 'stop_committed') = (stop_committed_at IS NOT NULL)),
    CHECK ((phase = 'aborted') = (aborted_at IS NOT NULL))
);

CREATE VIEW active_equipment_drains AS
    SELECT * FROM equipment_drains WHERE phase IN ('prepared', 'stop_committed');

-- A maintenance withdrawal cancels existing assignment authority, including
-- assignments belonging to other members of an atomic coordination group.
ALTER TABLE coordination_groups ADD COLUMN drain_cancel BOOLEAN NOT NULL DEFAULT FALSE;
