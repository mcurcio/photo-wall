-- Decision 0034, S2e part 1. A device is one row: `fleet_device_lifecycle` (its generation and
-- revocation) and `player_versions` (the version decided for it, the one before, a running
-- Install) fold into `devices`. Revocation was always written together with `retired_at`
-- (central/fleet/device_fence.py retire_in, one writer, one condition) and every reader tested
-- both, so it folds into `retired_at`. The trigger that created a device's lifecycle row goes: the
-- two inserts of a device (central/fleet/node_boot.py claim_new_device and retire_in's tombstone)
-- write generation 1 themselves. `devices` and `node_boot_offers` gain their audit fields; a row's
-- writer before this release is unknown. Written only by central/fleet/node_boot.py,
-- central/fleet/device_fence.py and central/infra/player_versions.py. Plain statements, no trigger
-- or function, forward-only.
DROP TRIGGER fleet_device_lifecycle_insert ON devices;
DROP FUNCTION fleet_initialize_device_lifecycle();

ALTER TABLE devices
    ADD COLUMN generation BIGINT,
    ADD COLUMN deployment_id UUID REFERENCES node_deployments(deployment_id),
    ADD COLUMN previous_deployment_id UUID REFERENCES node_deployments(deployment_id),
    ADD COLUMN install TEXT CHECK (install IN ('downloading', 'restarting')),
    ADD COLUMN version_changed_at DOUBLE PRECISION,
    ADD COLUMN created_at DOUBLE PRECISION,
    ADD COLUMN created_by TEXT,
    ADD COLUMN changed_at DOUBLE PRECISION,
    ADD COLUMN changed_by TEXT;

UPDATE devices SET generation = 1;   -- a device the trigger missed (none expected)
UPDATE devices d SET generation = l.generation, retired_at = coalesce(d.retired_at, l.revoked_at)
FROM fleet_device_lifecycle l WHERE l.device_id = d.device_id;

UPDATE devices d SET deployment_id = v.deployment_id,
                     previous_deployment_id = v.previous_deployment_id,
                     install = v.install,
                     version_changed_at = EXTRACT(EPOCH FROM v.changed_at)
FROM player_versions v WHERE v.device_id = d.device_id;

UPDATE devices SET created_at = first_seen, created_by = 'unknown',
                   changed_at = last_seen, changed_by = 'unknown';

ALTER TABLE devices
    ALTER COLUMN generation SET NOT NULL,
    ALTER COLUMN created_at SET NOT NULL,
    ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN changed_at SET NOT NULL,
    ALTER COLUMN changed_by SET NOT NULL,
    ADD CHECK (generation > 0),
    ADD CHECK ((deployment_id IS NULL) = (version_changed_at IS NULL)),
    ADD CHECK (deployment_id IS NOT NULL OR (previous_deployment_id IS NULL AND install IS NULL));

ALTER TABLE node_boot_offers
    ADD COLUMN created_by TEXT,
    ADD COLUMN changed_at DOUBLE PRECISION,
    ADD COLUMN changed_by TEXT;
UPDATE node_boot_offers SET created_by = 'unknown', changed_at = created_at, changed_by = 'unknown';
ALTER TABLE node_boot_offers
    ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN changed_at SET NOT NULL,
    ALTER COLUMN changed_by SET NOT NULL;

DROP TABLE player_versions, fleet_device_lifecycle;
