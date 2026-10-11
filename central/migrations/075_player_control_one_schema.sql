-- Control schema 1 (the published v0.12 four-field state envelope) and the mixed-version
-- Central pods it served are gone: a Player negotiates schema 2 before it reads state, and
-- Registry's own writes keep the applied-ACK nonce current, so the guard trigger that
-- protected the nonce from an older pod's writes goes too. A row a legacy session sealed
-- reopens, so its Player's next hello negotiates.
DROP TRIGGER player_control_ack_nonce_guard ON player_control_sessions;
DROP FUNCTION player_control_invalidate_ack_nonce();

UPDATE player_control_sessions SET status='open', schema_version=2, capabilities='[]',
    offered_schemas=NULL, offered_capabilities=NULL, applied_ack_nonce=NULL
    WHERE status <> 'negotiated' OR schema_version <> 2;

ALTER TABLE player_control_sessions DROP CONSTRAINT player_control_sessions_status_check;
ALTER TABLE player_control_sessions ADD CONSTRAINT player_control_sessions_status_check
    CHECK (status IN ('open', 'negotiated'));
ALTER TABLE player_control_sessions DROP CONSTRAINT player_control_sessions_schema_version_check;
ALTER TABLE player_control_sessions ALTER COLUMN schema_version SET DEFAULT 2;
ALTER TABLE player_control_sessions ADD CONSTRAINT player_control_sessions_schema_version_check
    CHECK (schema_version = 2);
