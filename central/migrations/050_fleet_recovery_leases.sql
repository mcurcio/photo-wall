-- An old drain has no fleet command correlation. Only a future issuer may set
-- this while committing the matching stop; NULL historical drains cannot
-- grant cross-boot recovery authority.
ALTER TABLE equipment_drains ADD COLUMN fleet_drain_id UUID;
CREATE UNIQUE INDEX fleet_equipment_drain_id
    ON equipment_drains(fleet_drain_id) WHERE fleet_drain_id IS NOT NULL;

CREATE FUNCTION fleet_guard_equipment_drain_id() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.phase='prepared' AND OLD.snapshot->>'admission_scope'='unbound_canary' THEN
        IF ROW(NEW.player_id,NEW.attempt_id,NEW.boot_id,NEW.authority_epoch,
               NEW.prepared_at,NEW.authorization_expires_at,NEW.snapshot)
           IS DISTINCT FROM
           ROW(OLD.player_id,OLD.attempt_id,OLD.boot_id,OLD.authority_epoch,
               OLD.prepared_at,OLD.authorization_expires_at,OLD.snapshot)
           OR (NEW.phase='prepared' AND NEW IS DISTINCT FROM OLD)
           OR (NEW.phase='stop_committed'
               AND (OLD.fleet_drain_id IS NOT NULL
                    OR NEW.aborted_at IS DISTINCT FROM OLD.aborted_at))
           OR (NEW.phase='aborted'
               AND (NEW.fleet_drain_id IS DISTINCT FROM OLD.fleet_drain_id
                    OR NEW.stop_committed_at IS DISTINCT FROM OLD.stop_committed_at)) THEN
            RAISE EXCEPTION 'prepared unbound drain snapshot is immutable'
                USING ERRCODE='23514', CONSTRAINT='fleet_prepared_unbound_drain_immutable';
        END IF;
    END IF;
    IF OLD.phase='stop_committed' AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'committed drain is immutable'
            USING ERRCODE='23514', CONSTRAINT='fleet_committed_drain_immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER fleet_equipment_drain_id_guard
    BEFORE UPDATE ON equipment_drains FOR EACH ROW
    EXECUTE FUNCTION fleet_guard_equipment_drain_id();

-- Each lease is a bounded repair capability scoped to one immutable attempt
-- and one verified *current* OS command session. Replacements append history;
-- the lease id is a CAS generation marker, not a bearer credential. The
-- original issuing session stays frozen on fleet_app_attempts.
CREATE UNIQUE INDEX fleet_recovery_carrier_scope
    ON fleet_os_command_sessions(command_session_id,device_id,device_generation,
                                 kernel_boot_id,offer_id,installation_audience,trust_mode);
CREATE TABLE fleet_recovery_leases (
    lease_id UUID PRIMARY KEY,
    attempt_id UUID NOT NULL,
    lease_sequence BIGINT NOT NULL CHECK (lease_sequence > 0),
    predecessor_lease_id UUID REFERENCES fleet_recovery_leases(lease_id),
    device_id TEXT NOT NULL,
    device_generation BIGINT NOT NULL CHECK (device_generation > 0),
    issuing_session_id UUID NOT NULL,
    carrier_session_id UUID NOT NULL,
    carrier_boot_id UUID NOT NULL,
    carrier_offer_id UUID NOT NULL,
    carrier_audience TEXT NOT NULL,
    carrier_trust_mode TEXT NOT NULL CHECK (carrier_trust_mode IN ('t1','t2')),
    issued_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL CHECK (expires_at > issued_at),
    superseded_at DOUBLE PRECISION,
    revoked_at DOUBLE PRECISION,
    UNIQUE(attempt_id,lease_sequence),
    UNIQUE(lease_id,attempt_id,carrier_session_id),
    FOREIGN KEY(attempt_id,device_id,device_generation,issuing_session_id)
        REFERENCES fleet_app_attempts(attempt_id,device_id,device_generation,
                                      command_session_id),
    FOREIGN KEY(carrier_session_id,device_id,device_generation,carrier_boot_id,
                carrier_offer_id,carrier_audience,carrier_trust_mode)
        REFERENCES fleet_os_command_sessions(command_session_id,device_id,
              device_generation,kernel_boot_id,offer_id,installation_audience,trust_mode)
);
CREATE UNIQUE INDEX fleet_one_current_recovery_lease
    ON fleet_recovery_leases(attempt_id)
    WHERE superseded_at IS NULL AND revoked_at IS NULL;

CREATE FUNCTION fleet_guard_recovery_lease_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE previous fleet_recovery_leases%ROWTYPE;
BEGIN
    -- Serialize even a direct SQL writer with the service's attempt lock.
    PERFORM 1 FROM fleet_app_attempts WHERE attempt_id=NEW.attempt_id FOR UPDATE;
    SELECT * INTO previous FROM fleet_recovery_leases
    WHERE attempt_id=NEW.attempt_id ORDER BY lease_sequence DESC LIMIT 1 FOR UPDATE;
    IF previous.lease_id IS NULL THEN
        IF NEW.lease_sequence<>1 OR NEW.predecessor_lease_id IS NOT NULL THEN
            RAISE EXCEPTION 'initial recovery lease requires sequence one'
                USING ERRCODE='23514', CONSTRAINT='fleet_recovery_lease_lineage';
        END IF;
    ELSIF previous.revoked_at IS NOT NULL
       OR previous.superseded_at IS NULL
       OR NEW.predecessor_lease_id IS DISTINCT FROM previous.lease_id
       OR NEW.lease_sequence<>previous.lease_sequence+1
       OR NEW.issued_at<previous.issued_at THEN
        RAISE EXCEPTION 'recovery lease replacement violates lineage or revocation'
            USING ERRCODE='23514', CONSTRAINT='fleet_recovery_lease_lineage';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER fleet_recovery_lease_insert_guard
    BEFORE INSERT ON fleet_recovery_leases FOR EACH ROW
    EXECUTE FUNCTION fleet_guard_recovery_lease_insert();

CREATE FUNCTION fleet_guard_recovery_lease() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'recovery lease history cannot be deleted'
            USING ERRCODE='23514', CONSTRAINT='fleet_recovery_lease_immutable';
    END IF;
    IF ROW(OLD.lease_id,OLD.attempt_id,OLD.lease_sequence,OLD.predecessor_lease_id,OLD.device_id,
           OLD.device_generation,OLD.issuing_session_id,OLD.carrier_session_id,
           OLD.carrier_boot_id,OLD.carrier_offer_id,OLD.carrier_audience,
           OLD.carrier_trust_mode,OLD.issued_at,OLD.expires_at)
       IS DISTINCT FROM
       ROW(NEW.lease_id,NEW.attempt_id,NEW.lease_sequence,NEW.predecessor_lease_id,NEW.device_id,
           NEW.device_generation,NEW.issuing_session_id,NEW.carrier_session_id,
           NEW.carrier_boot_id,NEW.carrier_offer_id,NEW.carrier_audience,
           NEW.carrier_trust_mode,NEW.issued_at,NEW.expires_at)
       OR (OLD.superseded_at IS NOT NULL
           AND NEW.superseded_at IS DISTINCT FROM OLD.superseded_at)
       OR (OLD.revoked_at IS NOT NULL
           AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at)
       OR (NEW.superseded_at IS NOT NULL AND NEW.superseded_at < OLD.issued_at)
       OR (NEW.revoked_at IS NOT NULL AND NEW.revoked_at < OLD.issued_at) THEN
        RAISE EXCEPTION 'recovery lease snapshot is immutable'
            USING ERRCODE='23514', CONSTRAINT='fleet_recovery_lease_immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER fleet_recovery_lease_guard
    BEFORE UPDATE OR DELETE ON fleet_recovery_leases FOR EACH ROW
    EXECUTE FUNCTION fleet_guard_recovery_lease();

-- Claim-only observations have their own sequence per attempted repair and
-- carrier session. Renewing a lease cannot reset sequence; a new boot can.
CREATE TABLE fleet_recovery_observations (
    attempt_id UUID NOT NULL,
    carrier_session_id UUID NOT NULL,
    report_sequence BIGINT NOT NULL CHECK (report_sequence > 0),
    lease_id UUID NOT NULL,
    report_json TEXT NOT NULL CHECK (octet_length(report_json) BETWEEN 1 AND 4096),
    received_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(attempt_id,carrier_session_id,report_sequence),
    FOREIGN KEY(lease_id,attempt_id,carrier_session_id)
        REFERENCES fleet_recovery_leases(lease_id,attempt_id,carrier_session_id)
);
CREATE INDEX fleet_recovery_observations_lease_latest
    ON fleet_recovery_observations(lease_id,report_sequence DESC);
CREATE FUNCTION fleet_guard_recovery_observation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE latest_sequence BIGINT;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'recovery observation history is append-only'
            USING ERRCODE='23514', CONSTRAINT='fleet_recovery_observation_immutable';
    END IF;
    PERFORM 1 FROM fleet_app_attempts WHERE attempt_id=NEW.attempt_id FOR UPDATE;
    SELECT max(report_sequence) INTO latest_sequence FROM fleet_recovery_observations
    WHERE attempt_id=NEW.attempt_id AND carrier_session_id=NEW.carrier_session_id;
    IF latest_sequence IS NOT NULL AND NEW.report_sequence <= latest_sequence THEN
        RAISE EXCEPTION 'recovery observation sequence went backward'
            USING ERRCODE='23514', CONSTRAINT='fleet_recovery_observation_sequence';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER fleet_recovery_observation_guard
    BEFORE INSERT OR UPDATE OR DELETE ON fleet_recovery_observations FOR EACH ROW
    EXECUTE FUNCTION fleet_guard_recovery_observation();
