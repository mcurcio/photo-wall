-- Command identity is a separate, commissioned OS-layer concern. T0 serial
-- claims and existing boot offers do not create command sessions.
CREATE TABLE fleet_device_lifecycle (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id),
    generation BIGINT NOT NULL CHECK (generation > 0),
    revoked_at DOUBLE PRECISION
);

-- Install the hook before backfill. CREATE TRIGGER takes a write-conflicting
-- table lock through this migration transaction, so no insert can fall into
-- a gap between the snapshot and trigger installation.
CREATE FUNCTION fleet_initialize_device_lifecycle() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO fleet_device_lifecycle(device_id,generation,revoked_at)
    VALUES(NEW.device_id,CASE WHEN NEW.retired_at IS NULL THEN 1 ELSE 2 END,NEW.retired_at)
    ON CONFLICT(device_id) DO NOTHING;
    RETURN NEW;
END;
$$;
CREATE TRIGGER fleet_device_lifecycle_insert
    AFTER INSERT ON devices FOR EACH ROW EXECUTE FUNCTION fleet_initialize_device_lifecycle();

-- Earlier Player retirement only changed `players`. Repair its canonical
-- device record before establishing generation one for active equipment.
INSERT INTO devices(device_id,first_seen,last_seen,retired_at)
SELECT p.device_id,p.registered_at,p.last_seen,p.retired_at FROM players p
WHERE p.retired_at IS NOT NULL
ON CONFLICT(device_id) DO UPDATE
SET retired_at=COALESCE(devices.retired_at,EXCLUDED.retired_at);

INSERT INTO fleet_device_lifecycle(device_id,generation,revoked_at)
SELECT device_id,CASE WHEN retired_at IS NULL THEN 1 ELSE 2 END,retired_at
FROM devices
ON CONFLICT(device_id) DO UPDATE
SET generation=CASE WHEN EXCLUDED.revoked_at IS NULL
                    THEN fleet_device_lifecycle.generation ELSE 2 END,
    revoked_at=COALESCE(fleet_device_lifecycle.revoked_at,EXCLUDED.revoked_at);

-- A new T0-created device has only an inert lifecycle row. That row confers
-- neither authentication nor command authority.

-- Inert until an authenticated T1 gateway or T2 verifier is installed. A
-- session is scoped to one installation, device lifecycle and kernel boot.
CREATE TABLE fleet_os_command_sessions (
    command_session_id UUID PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES fleet_device_lifecycle(device_id),
    device_generation BIGINT NOT NULL CHECK (device_generation > 0),
    kernel_boot_id UUID NOT NULL,
    offer_id UUID NOT NULL REFERENCES fleet_boot_offers(offer_id),
    installation_audience TEXT NOT NULL CHECK (length(installation_audience) BETWEEN 1 AND 256),
    trust_mode TEXT NOT NULL CHECK (trust_mode IN ('t1', 't2')),
    agent_key_sha256 TEXT NOT NULL CHECK (agent_key_sha256 ~ '^[0-9a-f]{64}$'),
    verifier_ref TEXT NOT NULL CHECK (length(verifier_ref) BETWEEN 1 AND 256),
    issued_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL CHECK (expires_at > issued_at),
    revoked_at DOUBLE PRECISION
);
CREATE INDEX fleet_os_command_sessions_device
    ON fleet_os_command_sessions(device_id, device_generation);
-- A new boot/agent can commission only after explicitly revoking the prior
-- session; expiry alone is not a safe replay fence.
CREATE UNIQUE INDEX fleet_one_unrevoked_os_command_session
    ON fleet_os_command_sessions(device_id, device_generation)
    WHERE revoked_at IS NULL;

-- Historical attempts have no generation and therefore cannot be admitted as
-- commands. A later attempt writer must fill this from the commissioned session.
ALTER TABLE fleet_app_attempts
    ADD COLUMN device_generation BIGINT CHECK (device_generation > 0),
    ADD COLUMN revoked_at DOUBLE PRECISION;

-- These views are structural guards only. The command admission boundary must
-- also enforce expiry, exact session and immutable attempt fields under lock.
CREATE VIEW fleet_generation_current_os_command_sessions AS
SELECT s.* FROM fleet_os_command_sessions s
JOIN fleet_device_lifecycle l ON l.device_id=s.device_id
JOIN devices d ON d.device_id=s.device_id
WHERE s.device_generation=l.generation AND s.revoked_at IS NULL
  AND l.revoked_at IS NULL AND d.retired_at IS NULL;

CREATE VIEW fleet_generation_current_app_attempts AS
SELECT a.* FROM fleet_app_attempts a
JOIN fleet_device_lifecycle l ON l.device_id=a.device_id
JOIN devices d ON d.device_id=a.device_id
WHERE a.device_generation=l.generation AND a.revoked_at IS NULL
  AND l.revoked_at IS NULL AND d.retired_at IS NULL;
