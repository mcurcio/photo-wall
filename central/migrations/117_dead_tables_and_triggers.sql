-- Decision 0034, S2a. Tables with no reader, and Central's database logic (owner, 2026-10-04:
-- "not a fan of db logic"). Plain statements, forward-only.

-- The seventeen immutability triggers and their one function. Central's code never updates or
-- deletes these records; a rule in the database is not where it says so.
DROP TRIGGER node_app_effect_immutable ON node_app_effects;
DROP TRIGGER node_app_operation_immutable ON node_app_operations;
DROP TRIGGER node_app_qualification_sample_immutable ON node_app_qualification_samples;
DROP TRIGGER node_app_qualification_immutable ON node_app_qualifications;
DROP TRIGGER node_app_response_immutable ON node_app_responses;
DROP TRIGGER node_base_managers_immutable ON node_base_managers;
DROP TRIGGER node_boot_offer_immutable ON node_boot_offers;
DROP TRIGGER node_ci_publication_immutable ON node_ci_publications;
DROP TRIGGER node_ci_revocation_immutable ON node_ci_revoked_qualifications;
DROP TRIGGER node_response_immutable ON node_command_responses;
DROP TRIGGER node_deployment_immutable ON node_deployments;
DROP TRIGGER node_environment_acceptance_immutable ON node_environment_acceptances;
DROP TRIGGER node_environment_immutable ON node_environment_catalog;
DROP TRIGGER node_offer_context_immutable ON node_offer_contexts;
DROP TRIGGER node_command_immutable ON node_reboot_commands;
DROP TRIGGER node_release_catalog_immutable ON node_release_catalog;
DROP TRIGGER node_release_verification_immutable ON node_release_verifications;
DROP FUNCTION node_immutable_record();

-- Every offer context is a node offer naming itself (076's CHECK: basis 'node_v2' and
-- node_offer_id = offer_id), so an admission's offer is the boot offer itself.
ALTER TABLE node_boot_admissions DROP CONSTRAINT node_boot_admissions_offer_id_fkey;
ALTER TABLE node_boot_admissions ADD CONSTRAINT node_boot_admissions_offer_id_fkey
  FOREIGN KEY (offer_id) REFERENCES node_boot_offers(offer_id);

DROP TABLE node_ci_publication_heads, node_ci_publications, node_ci_revoked_qualifications,
           node_offer_contexts, node_release_verifications, media_orphans, audit_events,
           runtime_changes;
