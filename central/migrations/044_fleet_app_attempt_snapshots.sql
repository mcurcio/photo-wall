-- Schema-one AppAttempts freeze one same-boot policy selection and both exact
-- data-only payloads. Old rows remain schema zero and cannot gain command authority.
ALTER TABLE fleet_app_attempts
    ADD COLUMN attempt_schema SMALLINT NOT NULL DEFAULT 0 CHECK (attempt_schema IN (0,1)),
    ADD COLUMN installation_audience TEXT,
    ADD COLUMN kernel_boot_id UUID,
    ADD COLUMN policy_source TEXT,
    ADD COLUMN base_sha256 TEXT,
    ADD COLUMN base_abi TEXT,
    ADD COLUMN base_abi_source_manifest TEXT,
    ADD COLUMN target_tag TEXT,
    ADD COLUMN target_size BIGINT,
    ADD COLUMN target_format TEXT,
    ADD COLUMN target_base_abi TEXT,
    ADD COLUMN target_source_manifest TEXT,
    ADD COLUMN fallback_size BIGINT,
    ADD COLUMN fallback_base_abi TEXT,
    ADD COLUMN fallback_trust_mode TEXT,
    ADD COLUMN fallback_evidence_ref TEXT,
    ADD CONSTRAINT fleet_attempt_snapshot_complete CHECK (
        attempt_schema=0 OR (
            device_generation IS NOT NULL AND fallback_sha256 IS NOT NULL
            AND num_nonnulls(installation_audience,kernel_boot_id,policy_source,
                             base_sha256,base_abi,base_abi_source_manifest,
                             target_tag,target_size,target_format,target_base_abi,
                             target_source_manifest,fallback_size,fallback_base_abi,
                             fallback_trust_mode,fallback_evidence_ref)=15
            AND installation_audience IS NOT NULL
            AND length(installation_audience) BETWEEN 1 AND 256
            AND installation_audience<>'photo-wall-central-t0'
            AND kernel_boot_id IS NOT NULL
            AND policy_source IN ('explicit','override')
            AND base_sha256 ~ '^[0-9a-f]{64}$'
            AND target_sha256<>fallback_sha256
            AND base_abi ~ '^sha256:[0-9a-f]{64}$'
            AND base_abi_source_manifest='manifest.v2.json'
            AND length(target_tag) BETWEEN 1 AND 128
            AND target_size BETWEEN 1 AND 268435456
            AND target_format='pw-player-data-v1'
            AND target_base_abi=base_abi
            AND target_source_manifest='manifest.v2.json'
            AND fallback_size BETWEEN 1 AND 268435456
            AND fallback_base_abi=base_abi
            AND fallback_trust_mode IN ('t1','t2')
            AND length(fallback_evidence_ref) BETWEEN 1 AND 256
        ));

-- A later desired revision may queue while one immutable attempt is underway;
-- it cannot enter preparation or mutation until that attempt is released.
CREATE UNIQUE INDEX fleet_one_active_prepared_attempt
    ON fleet_app_attempts(device_id,device_generation)
    WHERE attempt_schema=1 AND revoked_at IS NULL
      AND root_released_at IS NULL AND phase<>'queued';

CREATE FUNCTION fleet_guard_app_attempt_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        IF OLD.attempt_schema=1 THEN
            RAISE EXCEPTION 'immutable app attempt history cannot be deleted'
                USING ERRCODE='23514', CONSTRAINT='fleet_attempt_history_retained';
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.attempt_schema IS DISTINCT FROM OLD.attempt_schema THEN
        RAISE EXCEPTION 'app attempt schema cannot change after insert'
            USING ERRCODE='23514', CONSTRAINT='fleet_attempt_schema_immutable';
    END IF;
    IF OLD.attempt_schema=1 THEN
        IF ROW(OLD.attempt_schema,OLD.attempt_id,OLD.device_id,OLD.offer_id,OLD.desired_revision,
               OLD.target_sha256,OLD.fallback_sha256,OLD.device_generation,
               OLD.installation_audience,OLD.kernel_boot_id,OLD.policy_source,
               OLD.base_sha256,OLD.base_abi,OLD.base_abi_source_manifest,
               OLD.target_tag,OLD.target_size,OLD.target_format,OLD.target_base_abi,
               OLD.target_source_manifest,OLD.fallback_size,OLD.fallback_base_abi,
               OLD.fallback_trust_mode,OLD.fallback_evidence_ref,OLD.created_at)
           IS DISTINCT FROM
           ROW(NEW.attempt_schema,NEW.attempt_id,NEW.device_id,NEW.offer_id,NEW.desired_revision,
               NEW.target_sha256,NEW.fallback_sha256,NEW.device_generation,
               NEW.installation_audience,NEW.kernel_boot_id,NEW.policy_source,
               NEW.base_sha256,NEW.base_abi,NEW.base_abi_source_manifest,
               NEW.target_tag,NEW.target_size,NEW.target_format,NEW.target_base_abi,
               NEW.target_source_manifest,NEW.fallback_size,NEW.fallback_base_abi,
               NEW.fallback_trust_mode,NEW.fallback_evidence_ref,NEW.created_at) THEN
            RAISE EXCEPTION 'immutable app attempt snapshot changed'
                USING ERRCODE='23514', CONSTRAINT='fleet_attempt_snapshot_immutable';
        END IF;
        IF (OLD.command_id IS NOT NULL AND NEW.command_id IS DISTINCT FROM OLD.command_id)
           OR (OLD.drain_id IS NOT NULL AND NEW.drain_id IS DISTINCT FROM OLD.drain_id)
           OR (OLD.root_released_at IS NOT NULL
               AND NEW.root_released_at IS DISTINCT FROM OLD.root_released_at) THEN
            RAISE EXCEPTION 'app attempt fence changed after commitment'
                USING ERRCODE='23514', CONSTRAINT='fleet_attempt_fence_immutable';
        END IF;
        IF OLD.phase='queued' AND OLD.root_released_at IS NULL
           AND NEW.root_released_at IS NOT NULL
           AND (NEW.revoked_at IS NULL OR NEW.command_id IS NOT NULL
                OR NEW.drain_id IS NOT NULL) THEN
            RAISE EXCEPTION 'live queued attempt root cannot be released'
                USING ERRCODE='23514', CONSTRAINT='fleet_attempt_live_root';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER fleet_app_attempt_snapshot_guard
    BEFORE UPDATE OR DELETE ON fleet_app_attempts FOR EACH ROW
    EXECUTE FUNCTION fleet_guard_app_attempt_snapshot();

-- The owner is a cache root and a frozen locator. Neither the URL nor expected
-- bytes may be rewritten after publication. Deletion requires explicit release
-- of the attempt root, never a phase/timeout or command revocation alone.
CREATE FUNCTION fleet_guard_attempt_asset_reference() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE released DOUBLE PRECISION;
DECLARE attempt_row fleet_app_attempts%ROWTYPE;
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.owner LIKE 'fleet-attempt:%' THEN
            SELECT * INTO attempt_row FROM fleet_app_attempts
            WHERE attempt_id=substring(NEW.owner FROM 15)::uuid;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'attempt locator has no owner'
                    USING ERRCODE='23514', CONSTRAINT='fleet_attempt_locator_owner';
            END IF;
            -- Legacy schema-zero attempt roots predate the frozen snapshot.
            IF attempt_row.attempt_schema=1 AND (
               attempt_row.root_released_at IS NOT NULL
               OR NEW.kind<>'player-payload'
               OR NOT (
                   (NEW.identity=attempt_row.target_sha256
                    AND NEW.locator_sha256=attempt_row.target_sha256
                    AND NEW.expected_sha256=attempt_row.target_sha256
                    AND NEW.locator_size=attempt_row.target_size
                    AND NEW.expected_size=attempt_row.target_size)
                   OR
                   (NEW.identity=attempt_row.fallback_sha256
                    AND NEW.locator_sha256=attempt_row.fallback_sha256
                    AND NEW.expected_sha256=attempt_row.fallback_sha256
                    AND NEW.locator_size=attempt_row.fallback_size
                    AND NEW.expected_size=attempt_row.fallback_size))) THEN
                RAISE EXCEPTION 'attempt locator does not match active snapshot'
                    USING ERRCODE='23514', CONSTRAINT='fleet_attempt_locator_snapshot';
            END IF;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP='UPDATE' THEN
        IF OLD.owner LIKE 'fleet-attempt:%' OR NEW.owner LIKE 'fleet-attempt:%' THEN
            RAISE EXCEPTION 'immutable app attempt locator changed'
                USING ERRCODE='23514', CONSTRAINT='fleet_attempt_locator_immutable';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.owner LIKE 'fleet-attempt:%' THEN
        SELECT root_released_at INTO released FROM fleet_app_attempts
        WHERE attempt_id=substring(OLD.owner FROM 15)::uuid;
        IF released IS NULL THEN
            RAISE EXCEPTION 'app attempt bytes still retained'
                USING ERRCODE='23514', CONSTRAINT='fleet_attempt_root_active';
        END IF;
    END IF;
    RETURN OLD;
END;
$$;
CREATE TRIGGER fleet_attempt_asset_reference_guard
    BEFORE INSERT OR UPDATE OR DELETE ON asset_references FOR EACH ROW
    EXECUTE FUNCTION fleet_guard_attempt_asset_reference();
