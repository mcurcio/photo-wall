-- A version per Pi (the Home Assistant system design, Q2 A, 2026-10-16: "No stored wall-wide release;
-- Central keeps only a version per Pi"). Each enrolled Pi's version (central/infra/player_versions.py
-- PgPlayerVersions, the one writer): the deployment it boots, the one before it, and an install's
-- progress ('downloading', 'restarting'; NULL: none running). A Pi with no row boots the newest ready
-- stable release (central/content_catalog/boot_policy.py newest_ready_stable). The rules are app code;
-- no trigger, no stored procedure, no CHECK beyond the columns' types and references.
--
-- Upgrade data: every enrolled device takes the wall's stored selection (node_boot_policy) as its
-- version, and its previous selection as its previous one, so every Pi boots exactly what it booted
-- before; then the wall-wide selection goes. No backwards compatibility.
CREATE TABLE player_versions (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id),
    deployment_id UUID NOT NULL REFERENCES node_deployments(deployment_id),
    previous_deployment_id UUID NULL REFERENCES node_deployments(deployment_id),
    install TEXT NULL,
    changed_at TIMESTAMPTZ NOT NULL);

INSERT INTO player_versions (device_id, deployment_id, previous_deployment_id, install, changed_at)
SELECT d.device_id, p.deployment_id, p.previous_deployment_id, NULL, to_timestamp(p.changed_at)
FROM devices d
JOIN fleet_device_lifecycle l ON l.device_id = d.device_id
CROSS JOIN node_boot_policy p
WHERE d.retired_at IS NULL AND l.revoked_at IS NULL;

DROP TABLE node_boot_policy;
