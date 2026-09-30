-- An online command is one immutable handoff of an operator's frozen request.
-- Existing T0 attempts and drains remain historical; none is promoted here.
CREATE UNIQUE INDEX fleet_maintenance_command_scope
    ON fleet_maintenance_requests(request_id,device_id,device_generation,
                                  policy_source,policy_revision,target_sha256,
                                  target_size,target_base_abi);
CREATE UNIQUE INDEX fleet_attempt_command_scope
    ON fleet_app_attempts(attempt_id,command_id,drain_id,device_id,
                          device_generation,command_session_id,policy_source,
                          desired_revision,target_sha256,target_size,base_abi);

CREATE TABLE fleet_app_commands (
    command_id UUID PRIMARY KEY,
    request_id UUID NOT NULL UNIQUE,
    attempt_id UUID NOT NULL UNIQUE,
    drain_id UUID NOT NULL UNIQUE,
    player_id TEXT NOT NULL REFERENCES players(id),
    authority_epoch INTEGER NOT NULL CHECK (authority_epoch > 0),
    device_id TEXT NOT NULL,
    device_generation BIGINT NOT NULL CHECK (device_generation > 0),
    command_session_id UUID NOT NULL,
    policy_source TEXT NOT NULL CHECK (policy_source IN ('explicit','override')),
    desired_revision BIGINT NOT NULL CHECK (desired_revision > 0),
    target_sha256 TEXT NOT NULL CHECK (target_sha256 ~ '^[0-9a-f]{64}$'),
    target_size BIGINT NOT NULL CHECK (target_size BETWEEN 1 AND 268435456),
    base_abi TEXT NOT NULL CHECK (base_abi ~ '^sha256:[0-9a-f]{64}$'),
    command_bytes BYTEA NOT NULL CHECK (octet_length(command_bytes) BETWEEN 1 AND 4096),
    command_sha256 TEXT NOT NULL CHECK (command_sha256 ~ '^[0-9a-f]{64}$'),
    gate_generation BIGINT NOT NULL CHECK (gate_generation > 0),
    gate_scope_sha256 TEXT NOT NULL CHECK (gate_scope_sha256 ~ '^[0-9a-f]{64}$'),
    issued_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
        CHECK (expires_at > issued_at AND expires_at <= issued_at + 600),
    UNIQUE(command_id,attempt_id,drain_id),
    FOREIGN KEY(request_id,device_id,device_generation,policy_source,
                desired_revision,target_sha256,target_size,base_abi)
        REFERENCES fleet_maintenance_requests(request_id,device_id,device_generation,
                policy_source,policy_revision,target_sha256,target_size,target_base_abi),
    FOREIGN KEY(attempt_id,command_id,drain_id,device_id,device_generation,
                command_session_id,policy_source,desired_revision,target_sha256,
                target_size,base_abi)
        REFERENCES fleet_app_attempts(attempt_id,command_id,drain_id,device_id,
                device_generation,command_session_id,policy_source,desired_revision,
                target_sha256,target_size,base_abi)
);

-- A permit records that Central made a stop possible, even if its HTTP
-- response was lost. Its deadline never auto-releases the durable drain.
CREATE TABLE fleet_app_stop_permits (
    permit_id UUID PRIMARY KEY,
    command_id UUID NOT NULL UNIQUE,
    attempt_id UUID NOT NULL UNIQUE,
    drain_id UUID NOT NULL UNIQUE,
    ready_nonce TEXT NOT NULL CHECK (ready_nonce ~ '^[0-9a-f]{64}$'),
    ready_bytes BYTEA NOT NULL CHECK (octet_length(ready_bytes) BETWEEN 1 AND 4096),
    ready_sha256 TEXT NOT NULL CHECK (ready_sha256 ~ '^[0-9a-f]{64}$'),
    permit_bytes BYTEA NOT NULL CHECK (octet_length(permit_bytes) BETWEEN 1 AND 4096),
    permit_sha256 TEXT NOT NULL CHECK (permit_sha256 ~ '^[0-9a-f]{64}$'),
    gate_generation BIGINT NOT NULL CHECK (gate_generation > 0),
    gate_scope_sha256 TEXT NOT NULL CHECK (gate_scope_sha256 ~ '^[0-9a-f]{64}$'),
    issued_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
        CHECK (expires_at > issued_at AND expires_at <= issued_at + 30),
    FOREIGN KEY(command_id,attempt_id,drain_id)
        REFERENCES fleet_app_commands(command_id,attempt_id,drain_id)
);

CREATE FUNCTION fleet_guard_command_history() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'fleet command and permit history is immutable'
            USING ERRCODE='23514', CONSTRAINT='fleet_command_history_immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER fleet_app_command_immutable
    BEFORE UPDATE OR DELETE ON fleet_app_commands FOR EACH ROW
    EXECUTE FUNCTION fleet_guard_command_history();
CREATE TRIGGER fleet_app_stop_permit_immutable
    BEFORE UPDATE OR DELETE ON fleet_app_stop_permits FOR EACH ROW
    EXECUTE FUNCTION fleet_guard_command_history();

CREATE FUNCTION fleet_guard_app_command_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE player_row players%ROWTYPE;
DECLARE attempt_row fleet_app_attempts%ROWTYPE;
DECLARE drain_row equipment_drains%ROWTYPE;
DECLARE request_row fleet_maintenance_requests%ROWTYPE;
BEGIN
    SELECT * INTO player_row FROM players WHERE id=NEW.player_id FOR SHARE;
    SELECT * INTO request_row FROM fleet_maintenance_requests
    WHERE request_id=NEW.request_id FOR SHARE;
    SELECT * INTO attempt_row FROM fleet_app_attempts
    WHERE attempt_id=NEW.attempt_id FOR SHARE;
    SELECT * INTO drain_row FROM equipment_drains
    WHERE player_id=NEW.player_id FOR SHARE;
    IF player_row.id IS NULL OR player_row.retired_at IS NOT NULL
       OR player_row.device_id<>NEW.device_id
       OR player_row.authority_epoch<>NEW.authority_epoch
       OR request_row.status<>'dispatched'
       OR attempt_row.phase<>'prepared'
       OR drain_row.phase<>'prepared'
       OR drain_row.attempt_id<>NEW.attempt_id::text
       OR drain_row.boot_id<>attempt_row.kernel_boot_id::text
       OR drain_row.authority_epoch<>NEW.authority_epoch
       OR drain_row.fleet_drain_id IS NOT NULL
       OR drain_row.snapshot->>'admission_scope'<>'unbound_canary' THEN
        RAISE EXCEPTION 'command lacks matching prepared unbound drain'
            USING ERRCODE='23514', CONSTRAINT='fleet_command_drain_mismatch';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER fleet_app_command_guard
    BEFORE INSERT ON fleet_app_commands FOR EACH ROW
    EXECUTE FUNCTION fleet_guard_app_command_insert();

-- The service inserts a permit only after it has committed the exact current
-- unbound drain and attempt in the same transaction. This trigger is a final
-- database guard against a second writer accidentally minting a stop right.
CREATE FUNCTION fleet_guard_stop_permit_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE command_row fleet_app_commands%ROWTYPE;
DECLARE attempt_row fleet_app_attempts%ROWTYPE;
DECLARE drain_row equipment_drains%ROWTYPE;
BEGIN
    SELECT * INTO attempt_row FROM fleet_app_attempts
    WHERE attempt_id=NEW.attempt_id FOR SHARE;
    SELECT * INTO command_row FROM fleet_app_commands
    WHERE command_id=NEW.command_id FOR SHARE;
    SELECT * INTO drain_row FROM equipment_drains
    WHERE player_id=command_row.player_id FOR SHARE;
    IF NOT FOUND OR command_row.command_id IS NULL OR attempt_row.attempt_id IS NULL
       OR command_row.attempt_id<>NEW.attempt_id
       OR command_row.drain_id<>NEW.drain_id
       OR command_row.gate_generation<>NEW.gate_generation
       OR command_row.gate_scope_sha256<>NEW.gate_scope_sha256
       OR NEW.expires_at>command_row.expires_at
       OR attempt_row.phase<>'stop_committed'
       OR attempt_row.command_id<>NEW.command_id
       OR attempt_row.drain_id<>NEW.drain_id
       OR drain_row.phase<>'stop_committed'
       OR drain_row.fleet_drain_id<>NEW.drain_id
       OR drain_row.attempt_id<>NEW.attempt_id::text
       OR drain_row.boot_id<>attempt_row.kernel_boot_id::text
       OR drain_row.authority_epoch<>command_row.authority_epoch
       OR drain_row.snapshot->>'admission_scope'<>'unbound_canary' THEN
        RAISE EXCEPTION 'stop permit lacks matching committed unbound drain'
            USING ERRCODE='23514', CONSTRAINT='fleet_stop_permit_drain_mismatch';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER fleet_app_stop_permit_guard
    BEFORE INSERT ON fleet_app_stop_permits FOR EACH ROW
    EXECUTE FUNCTION fleet_guard_stop_permit_insert();
