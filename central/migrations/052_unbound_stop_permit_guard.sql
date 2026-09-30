-- A newly committed unbound stop must have its immutable permit in the same
-- transaction. Defer the check because the issuer commits the drain before it
-- inserts the permit. Existing committed drains are historical and are never
-- revalidated by merely installing this trigger or updating their metadata.
CREATE FUNCTION fleet_require_unbound_stop_permit() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE final_drain equipment_drains%ROWTYPE;
BEGIN
    SELECT * INTO final_drain FROM equipment_drains WHERE player_id=NEW.player_id;
    IF final_drain.player_id IS NULL OR final_drain.phase<>'stop_committed' THEN
        RETURN NULL;
    END IF;
    IF final_drain.snapshot->>'admission_scope' IS DISTINCT FROM 'unbound_canary'
       OR final_drain.fleet_drain_id IS NULL
       OR NOT EXISTS (
           SELECT 1 FROM fleet_app_stop_permits permit
           JOIN fleet_app_commands command
             ON command.command_id=permit.command_id
            AND command.attempt_id=permit.attempt_id
            AND command.drain_id=permit.drain_id
           JOIN fleet_app_attempts attempt
             ON attempt.attempt_id=command.attempt_id
            AND attempt.command_id=command.command_id
            AND attempt.drain_id=command.drain_id
           WHERE permit.drain_id=final_drain.fleet_drain_id
             AND command.player_id=final_drain.player_id
             AND command.authority_epoch=final_drain.authority_epoch
             AND final_drain.attempt_id=attempt.attempt_id::text
             AND final_drain.boot_id=attempt.kernel_boot_id::text
             AND attempt.phase='stop_committed'
       ) THEN
        RAISE EXCEPTION 'new unbound stop requires its same-transaction fleet permit'
            USING ERRCODE='23514', CONSTRAINT='fleet_unbound_stop_permit_required';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER fleet_unbound_stop_insert_permit
    AFTER INSERT ON equipment_drains DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    WHEN (NEW.phase='stop_committed'
          AND NEW.snapshot->>'admission_scope'='unbound_canary')
    EXECUTE FUNCTION fleet_require_unbound_stop_permit();

CREATE CONSTRAINT TRIGGER fleet_unbound_stop_update_permit
    AFTER UPDATE ON equipment_drains DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    WHEN (OLD.phase IS DISTINCT FROM 'stop_committed'
          AND NEW.phase='stop_committed'
          AND (OLD.snapshot->>'admission_scope'='unbound_canary'
               OR NEW.snapshot->>'admission_scope'='unbound_canary'))
    EXECUTE FUNCTION fleet_require_unbound_stop_permit();
