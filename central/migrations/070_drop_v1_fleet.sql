-- Decision 0019 (owner, 2026-10-09, "Do it in this PR"): Central keeps no V1 check-in, no V1 fleet
-- status and no V1 Player `.deb` delivery. Each object below lost its last reader in the same
-- change. Kept: app_release_poll (the release listing's ETag), devices, fleet_device_lifecycle
-- and its devices trigger, fleet_effect_gate (the rollout gate), fleet_t0_daily_quotas (node boot
-- offers' quota), assets, asset_references, job_outcomes and every node_* table.
--
--   fleet_os_observations                           POST /v1/appliance/check-ins and
--                                                   /v2/appliance/check-ins
--                                                   (FleetService.record_check_in) and the
--                                                   fleet status (FleetService.status).
--   fleet_generation_acceptances and                the fleet status's accepted fallback
--   fleet_guard_generation_acceptance()             (GET /v1/operator/fleet).
--   fleet_os_command_sessions, fleet_app_attempts,  the fleet status's management read
--   fleet_os_attempt_reports, fleet_app_fences,     (management_status.py), the registry's
--   fleet_guard_app_attempt_snapshot(),             retirement revocation and the rollout
--   fleet_guard_attempt_issuing_session(),          gate's attempt barrier.
--   fleet_guard_os_attempt_report() and the views
--   fleet_generation_current_app_attempts and
--   fleet_generation_current_os_command_sessions
--   fleet_accepted_artifacts, fleet_artifact_abi    none left (036's V1 fleet foundation).
--   app_releases, app_release_policy                the release sync's legacy record (its
--                                                   claim and os-image reference), the
--                                                   promotion, GET /v1/app/manifest,
--                                                   GET /v1/app/package/{sha256}.deb and
--                                                   GET /v1/operator/app/releases.
--   app_packages, app_package_policy                none left (014's served `.deb`, carried
--                                                   into app_releases by 023).
--   'player-deb' assets, references, outcomes       FetchPackage and its handler.
--   'os-image' references owned by a release tag   the release sync's legacy record; a node
--                                                   deployment references its own base
--                                                   tarball (owner 'node-deployment:<id>').

DROP VIEW fleet_generation_current_app_attempts;
DROP VIEW fleet_generation_current_os_command_sessions;

DROP TABLE fleet_generation_acceptances;
DROP FUNCTION fleet_guard_generation_acceptance();
DROP TABLE fleet_app_fences;
DROP TABLE fleet_os_attempt_reports;
DROP FUNCTION fleet_guard_os_attempt_report();
DROP TABLE fleet_app_attempts;
DROP FUNCTION fleet_guard_app_attempt_snapshot();
DROP FUNCTION fleet_guard_attempt_issuing_session();
DROP TABLE fleet_os_command_sessions;
DROP TABLE fleet_os_observations;
DROP TABLE fleet_accepted_artifacts;
DROP TABLE fleet_artifact_abi;

DROP TABLE app_release_policy;
DROP TABLE app_releases;
DROP TABLE app_package_policy;
DROP TABLE app_packages;

DELETE FROM asset_references WHERE kind = 'os-image' AND owner NOT LIKE 'node-deployment:%';
DELETE FROM assets WHERE kind = 'player-deb';  -- its references go by ON DELETE CASCADE
DELETE FROM job_outcomes WHERE job_name = 'player_deb.fetch';
ALTER TABLE assets DROP CONSTRAINT assets_kind_check;
ALTER TABLE assets ADD CONSTRAINT assets_kind_check
    CHECK (kind IN ('os-image', 'sealed-environment', 'library-thumbnail'));
