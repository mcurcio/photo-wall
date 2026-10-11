-- Decision 0019 (owner, 2026-10-09, "Do it in this PR"): Central offers no V1 boot, keeps no V1
-- fleet intent and reads no Player payload. Each object below lost its last reader in the same
-- change; nothing on the kept list (app_releases' package columns, assets, asset_references,
-- devices, fleet_os_observations, fleet_accepted_artifacts, fleet_app_attempts,
-- fleet_os_command_sessions, fleet_os_attempt_reports, fleet_device_lifecycle,
-- fleet_t0_daily_quotas, fleet_generation_acceptances and every node_* table) is dropped.
--
--   fleet_boot_offers, fleet_offer_artifact_roots   POST /v1/netboot/offers and its byte routes
--                                                   (FleetService.create_offer, offer_asset),
--                                                   the check-in's offer match, the fleet
--                                                   status's offer, node_sessions' legacy
--                                                   adoption and node_observations' G5 read.
--                                                   The kept tables' offer_id columns stay, as
--                                                   history, without their foreign keys.
--   fleet_app_policy, fleet_device_app_overrides,   the app policy, override and base-baseline
--   fleet_base_policy, fleet_policy_revision_seq,   writes and the status's policy read
--   fleet_validate_policy_artifact()                (FleetService.set_app_policy, set_override,
--                                                   set_base_baseline), and the desired-set read
--                                                   of their targets (catalog_records).
--   fleet_maintenance_requests and its two          FleetService.request_maintenance,
--   functions                                       cancel_maintenance (fleet/maintenance_requests.py).
--   device_base_health                              POST /v1/player/base-health
--                                                   (netboot_base.record_base_health).
--   base_cache                                      the fleet status's release list and the base
--                                                   baseline check.
--   base_boot_status                                none left (019's V1 netboot status).
--   devices' pin and V1 frontier columns            the catalog's pin, unpin, netboot view and
--                                                   /v1/netboot/base serving (PgDeviceRecords),
--                                                   the base-health write, G5 and the status.
--   app_releases' payload_* and base_abi_* columns  the release sync's payload and base-ABI
--                                                   records (manifest.v2.json is not read).
--   fleet_artifact_retention_attempts (view)        the payload desired set and fallback.
--   fleet_guard_attempt_asset_reference()           guarded only 'player-payload' attempt roots.
--   'player-payload' assets, references, outcomes   FetchPlayerPayload and its handler.
--   node_offer_contexts.legacy_offer_id             node_sessions' legacy adoption. Adopted
--                                                   contexts stay as history (basis
--                                                   'legacy_adoption'): their admissions,
--                                                   producers and samples are immutable records.

-- The kept tables' foreign keys into the V1 offers.
ALTER TABLE fleet_os_observations DROP CONSTRAINT fleet_os_observations_offer_id_fkey;
ALTER TABLE fleet_os_command_sessions DROP CONSTRAINT fleet_os_command_sessions_offer_id_fkey;
ALTER TABLE fleet_app_attempts DROP CONSTRAINT fleet_app_attempts_offer_id_fkey;
ALTER TABLE node_offer_contexts DROP CONSTRAINT node_offer_contexts_check;
ALTER TABLE node_offer_contexts DROP COLUMN legacy_offer_id;
ALTER TABLE node_offer_contexts ADD CONSTRAINT node_offer_contexts_check CHECK (
    (basis = 'legacy_adoption' AND node_offer_id IS NULL)
    OR (basis = 'node_v2' AND node_offer_id = offer_id));

-- V1 boot offers and fleet intent.
DROP TABLE fleet_offer_artifact_roots;
DROP TABLE fleet_boot_offers;
DROP TABLE fleet_device_app_overrides;
DROP TABLE fleet_app_policy;
DROP FUNCTION fleet_validate_policy_artifact();
DROP TABLE fleet_base_policy;
DROP SEQUENCE fleet_policy_revision_seq;
DROP TRIGGER fleet_maintenance_device_retired ON devices;
DROP FUNCTION fleet_cancel_maintenance_on_retirement();
DROP TABLE fleet_maintenance_requests;
DROP FUNCTION fleet_guard_maintenance_request();

-- V1 netboot base serving and its frontier.
DROP TABLE device_base_health;
DROP TABLE base_cache;
DROP TABLE base_boot_status;
ALTER TABLE devices
    DROP COLUMN attached_tag,
    DROP COLUMN known_good_tag,
    DROP COLUMN known_good_at,
    DROP COLUMN last_served_tag,
    DROP COLUMN last_served_at,
    DROP COLUMN boot_outcome,
    DROP COLUMN failed_tag;

-- The Player payload and the base ABI a release recorded for it.
ALTER TABLE app_releases
    DROP COLUMN payload_url,
    DROP COLUMN payload_sha256,
    DROP COLUMN payload_size,
    DROP COLUMN payload_format,
    DROP COLUMN payload_base_abi,
    DROP COLUMN payload_source_manifest,
    DROP COLUMN base_abi,
    DROP COLUMN base_abi_squashfs_sha256,
    DROP COLUMN base_abi_source_manifest;
DROP VIEW fleet_artifact_retention_attempts;
DROP TRIGGER fleet_attempt_asset_reference_guard ON asset_references;
DROP FUNCTION fleet_guard_attempt_asset_reference();
DELETE FROM assets WHERE kind = 'player-payload';  -- its references go by ON DELETE CASCADE
DELETE FROM job_outcomes WHERE job_name = 'player_payload.fetch';
ALTER TABLE assets DROP CONSTRAINT assets_kind_check;
ALTER TABLE assets ADD CONSTRAINT assets_kind_check
    CHECK (kind IN ('os-image', 'player-deb', 'sealed-environment', 'library-thumbnail'));
