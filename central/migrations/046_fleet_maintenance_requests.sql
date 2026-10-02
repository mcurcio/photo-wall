-- Operator maintenance intent is inert. It is neither a command nor a permit to
-- drain, stop, install, or mark an application accepted. `dispatched` is a
-- terminal handoff of this intent, not execution progress. A future dispatcher
-- must own its attempt correlation and settlement in its own migration.
CREATE TABLE fleet_maintenance_requests (
    request_id UUID PRIMARY KEY,
    -- Fleet-locked insertion order is the request chronology. Wall time may step.
    request_ordinal BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE NOT NULL,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    device_generation BIGINT NOT NULL CHECK (device_generation > 0),
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    status TEXT NOT NULL CHECK (status IN ('queued','expired','canceled','dispatched')),
    policy_source TEXT NOT NULL CHECK (policy_source IN ('explicit','override')),
    policy_revision BIGINT NOT NULL CHECK (policy_revision > 0),
    target_tag TEXT NOT NULL,
    target_sha256 TEXT NOT NULL CHECK (target_sha256 ~ '^[0-9a-f]{64}$'),
    target_size BIGINT NOT NULL CHECK (target_size BETWEEN 1 AND 268435456),
    target_format TEXT NOT NULL CHECK (target_format = 'pw-player-data-v1'),
    target_base_abi TEXT NOT NULL CHECK (target_base_abi ~ '^sha256:[0-9a-f]{64}$'),
    target_source_manifest TEXT NOT NULL CHECK (target_source_manifest = 'manifest.v2.json'),
    ttl_seconds INTEGER NOT NULL CHECK (ttl_seconds BETWEEN 300 AND 86400),
    requested_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL CHECK (expires_at > requested_at),
    changed_at DOUBLE PRECISION NOT NULL,
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 64),
    CHECK (length(target_tag) BETWEEN 1 AND 128)
);

-- The fleet lock serializes writers across pods. The index is the final guard
-- if an additional writer is introduced without that lock.
CREATE UNIQUE INDEX fleet_one_queued_maintenance_request
    ON fleet_maintenance_requests(device_id,device_generation)
    WHERE status='queued';
CREATE INDEX fleet_maintenance_latest
    ON fleet_maintenance_requests(device_id,device_generation,request_ordinal DESC);

CREATE FUNCTION fleet_guard_maintenance_request() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'maintenance intent history cannot be deleted'
            USING ERRCODE='23514', CONSTRAINT='fleet_maintenance_history_retained';
    END IF;
    IF ROW(OLD.request_id,OLD.request_ordinal,OLD.device_id,OLD.device_generation,OLD.policy_source,
           OLD.policy_revision,OLD.target_tag,OLD.target_sha256,OLD.target_size,
           OLD.target_format,OLD.target_base_abi,OLD.target_source_manifest,
           OLD.ttl_seconds,OLD.requested_at,OLD.expires_at)
       IS DISTINCT FROM
       ROW(NEW.request_id,NEW.request_ordinal,NEW.device_id,NEW.device_generation,NEW.policy_source,
           NEW.policy_revision,NEW.target_tag,NEW.target_sha256,NEW.target_size,
           NEW.target_format,NEW.target_base_abi,NEW.target_source_manifest,
           NEW.ttl_seconds,NEW.requested_at,NEW.expires_at) THEN
        RAISE EXCEPTION 'maintenance request snapshot is immutable'
            USING ERRCODE='23514', CONSTRAINT='fleet_maintenance_snapshot_immutable';
    END IF;
    IF OLD.status<>'queued' OR NEW.status NOT IN ('expired','canceled','dispatched')
       OR NEW.revision<>OLD.revision+1 OR NEW.changed_at<OLD.changed_at THEN
        RAISE EXCEPTION 'invalid maintenance request transition'
            USING ERRCODE='23514', CONSTRAINT='fleet_maintenance_transition';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER fleet_maintenance_request_guard
    BEFORE UPDATE OR DELETE ON fleet_maintenance_requests FOR EACH ROW
    EXECUTE FUNCTION fleet_guard_maintenance_request();

-- Registry retirement already holds the fleet lock and device row. Closing a
-- queued request in the same transaction keeps retirement from leaving live
-- operator intent against a revoked generation.
CREATE FUNCTION fleet_cancel_maintenance_on_retirement() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.retired_at IS NULL AND NEW.retired_at IS NOT NULL THEN
        UPDATE fleet_maintenance_requests
           SET status='canceled',revision=revision+1,reason='device_retired',
               changed_at=GREATEST(changed_at,NEW.retired_at)
         WHERE device_id=NEW.device_id AND status='queued';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER fleet_maintenance_device_retired
    AFTER UPDATE OF retired_at ON devices FOR EACH ROW
    EXECUTE FUNCTION fleet_cancel_maintenance_on_retirement();
