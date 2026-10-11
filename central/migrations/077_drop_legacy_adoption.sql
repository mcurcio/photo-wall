-- The retired V1 legacy adoption's history goes (owner, V1 state scope "all"; nothing worth
-- preserving). 069 kept each adopted context (basis 'legacy_adoption') with its admissions and
-- everything recorded under them; no code writes one, and their only effect was a read branch
-- that refused their commands. Every row hanging off an adopted admission is deleted leaf-first,
-- then the context, and node_offer_contexts admits only node offers: each context's basis is
-- 'node_v2' and it names its own node offer.
--
-- The immutable-record triggers refuse deletes, so each table's trigger is disabled for its
-- delete and enabled again in this same transaction.

CREATE TEMPORARY TABLE legacy_producers ON COMMIT DROP AS
    SELECT p.producer_id FROM node_producers p
    JOIN node_boot_admissions a ON a.admission_id = p.admission_id
    JOIN node_offer_contexts c ON c.offer_id = a.offer_id
    WHERE c.basis = 'legacy_adoption';
CREATE TEMPORARY TABLE legacy_sessions ON COMMIT DROP AS
    SELECT session_id FROM node_sessions WHERE producer_id IN (SELECT producer_id FROM legacy_producers);

ALTER TABLE node_app_responses DISABLE TRIGGER node_app_response_immutable;
DELETE FROM node_app_responses WHERE command_id IN (
    SELECT command_id FROM node_app_operations
    WHERE producer_id IN (SELECT producer_id FROM legacy_producers)
       OR session_id IN (SELECT session_id FROM legacy_sessions));
ALTER TABLE node_app_responses ENABLE TRIGGER node_app_response_immutable;

ALTER TABLE node_app_effects DISABLE TRIGGER node_app_effect_immutable;
DELETE FROM node_app_effects
    WHERE producer_id IN (SELECT producer_id FROM legacy_producers)
       OR operation_id IN (SELECT operation_id FROM node_app_operations
                           WHERE producer_id IN (SELECT producer_id FROM legacy_producers)
                              OR session_id IN (SELECT session_id FROM legacy_sessions));
ALTER TABLE node_app_effects ENABLE TRIGGER node_app_effect_immutable;

ALTER TABLE node_app_operations DISABLE TRIGGER node_app_operation_immutable;
DELETE FROM node_app_operations
    WHERE producer_id IN (SELECT producer_id FROM legacy_producers)
       OR session_id IN (SELECT session_id FROM legacy_sessions);
ALTER TABLE node_app_operations ENABLE TRIGGER node_app_operation_immutable;

ALTER TABLE node_display_handoffs DISABLE TRIGGER node_display_handoff_immutable;
DELETE FROM node_display_handoffs WHERE decision_id IN (
    SELECT decision_id FROM node_display_exchanges
    WHERE producer_id IN (SELECT producer_id FROM legacy_producers)
       OR session_id IN (SELECT session_id FROM legacy_sessions));
ALTER TABLE node_display_handoffs ENABLE TRIGGER node_display_handoff_immutable;

ALTER TABLE node_display_withdrawals DISABLE TRIGGER node_display_withdrawal_immutable;
DELETE FROM node_display_withdrawals WHERE decision_id IN (
    SELECT decision_id FROM node_display_exchanges
    WHERE producer_id IN (SELECT producer_id FROM legacy_producers)
       OR session_id IN (SELECT session_id FROM legacy_sessions));
ALTER TABLE node_display_withdrawals ENABLE TRIGGER node_display_withdrawal_immutable;

ALTER TABLE node_display_exchanges DISABLE TRIGGER node_display_exchange_immutable;
DELETE FROM node_display_exchanges
    WHERE producer_id IN (SELECT producer_id FROM legacy_producers)
       OR session_id IN (SELECT session_id FROM legacy_sessions);
ALTER TABLE node_display_exchanges ENABLE TRIGGER node_display_exchange_immutable;

ALTER TABLE node_command_responses DISABLE TRIGGER node_response_immutable;
DELETE FROM node_command_responses WHERE command_id IN (
    SELECT command_id FROM node_reboot_commands
    WHERE session_id IN (SELECT session_id FROM legacy_sessions));
ALTER TABLE node_command_responses ENABLE TRIGGER node_response_immutable;

ALTER TABLE node_reboot_commands DISABLE TRIGGER node_command_immutable;
DELETE FROM node_reboot_commands WHERE session_id IN (SELECT session_id FROM legacy_sessions);
ALTER TABLE node_reboot_commands ENABLE TRIGGER node_command_immutable;

DELETE FROM node_output_losses WHERE cause_producer_id IN (SELECT producer_id FROM legacy_producers);
DELETE FROM node_reconciliation_work WHERE producer_id IN (SELECT producer_id FROM legacy_producers);

ALTER TABLE node_evidence DISABLE TRIGGER node_evidence_immutable;
DELETE FROM node_evidence WHERE producer_id IN (SELECT producer_id FROM legacy_producers);
ALTER TABLE node_evidence ENABLE TRIGGER node_evidence_immutable;

ALTER TABLE node_host_observations DISABLE TRIGGER node_observation_immutable;
DELETE FROM node_host_observations WHERE producer_id IN (SELECT producer_id FROM legacy_producers);
ALTER TABLE node_host_observations ENABLE TRIGGER node_observation_immutable;

ALTER TABLE node_manager_observations DISABLE TRIGGER node_manager_observation_immutable;
DELETE FROM node_manager_observations WHERE producer_id IN (SELECT producer_id FROM legacy_producers);
ALTER TABLE node_manager_observations ENABLE TRIGGER node_manager_observation_immutable;

DELETE FROM node_host_facts WHERE producer_id IN (SELECT producer_id FROM legacy_producers);
DELETE FROM node_app_links WHERE producer_id IN (SELECT producer_id FROM legacy_producers);
DELETE FROM node_calibration_trials WHERE producer_id IN (SELECT producer_id FROM legacy_producers);
DELETE FROM node_sessions WHERE producer_id IN (SELECT producer_id FROM legacy_producers);
DELETE FROM node_producers WHERE producer_id IN (SELECT producer_id FROM legacy_producers);
DELETE FROM node_boot_admissions WHERE offer_id IN (
    SELECT offer_id FROM node_offer_contexts WHERE basis = 'legacy_adoption');

ALTER TABLE node_offer_contexts DISABLE TRIGGER node_offer_context_immutable;
DELETE FROM node_offer_contexts WHERE basis = 'legacy_adoption';
ALTER TABLE node_offer_contexts ENABLE TRIGGER node_offer_context_immutable;

ALTER TABLE node_offer_contexts DROP CONSTRAINT node_offer_contexts_check;
ALTER TABLE node_offer_contexts ALTER COLUMN node_offer_id SET NOT NULL;
ALTER TABLE node_offer_contexts ADD CONSTRAINT node_offer_contexts_check
    CHECK (basis = 'node_v2' AND node_offer_id = offer_id);
