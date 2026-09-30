-- An applied ACK gets an unpredictable marker only after Registry accepts it.
-- Older Central pods do not know this column. Clear a marker if one of those
-- pods supersedes its delivery, resets an epoch, or records a later result.
ALTER TABLE player_control_sessions ADD COLUMN applied_ack_nonce TEXT;
ALTER TABLE player_control_sessions ADD CONSTRAINT player_control_ack_nonce_shape
    CHECK (applied_ack_nonce IS NULL OR applied_ack_nonce ~ '^[0-9a-f]{64}$');

CREATE FUNCTION player_control_invalidate_ack_nonce() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.applied_ack_nonce IS NOT NULL AND (
        NEW.schema_version <> 2 OR NEW.status <> 'negotiated'
        OR NEW.pending_id IS NOT NULL
        OR NEW.applied_delivery_id IS NULL OR NEW.applied_digest IS NULL
        OR NEW.last_result IS DISTINCT FROM 'applied'
        OR NEW.last_delivery_id IS DISTINCT FROM NEW.applied_delivery_id
        OR NEW.last_result_digest IS DISTINCT FROM NEW.applied_digest
        OR NEW.last_result_sequence IS DISTINCT FROM NEW.applied_sequence
        OR NEW.issued_sequence IS DISTINCT FROM NEW.applied_sequence
    ) THEN
        NEW.applied_ack_nonce := NULL;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER player_control_ack_nonce_guard
    BEFORE INSERT OR UPDATE ON player_control_sessions
    FOR EACH ROW EXECUTE FUNCTION player_control_invalidate_ack_nonce();
