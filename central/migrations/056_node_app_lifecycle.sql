-- V2 online app operations and append-only withdrawal/discharge are separate
-- from the legacy V1 drain/permit ledger and never rewrite its committed rows.
CREATE TABLE node_app_operations (
    operation_id UUID PRIMARY KEY,
    command_id UUID NOT NULL UNIQUE,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    device_generation BIGINT NOT NULL,
    player_id TEXT NOT NULL REFERENCES players(id),
    authority_epoch BIGINT NOT NULL,
    producer_id UUID NOT NULL REFERENCES node_producers(producer_id),
    session_id UUID NOT NULL REFERENCES node_sessions(session_id),
    deployment_id UUID NOT NULL REFERENCES node_deployments(deployment_id),
    request_sha256 TEXT NOT NULL,
    operator_audit_ref TEXT NOT NULL,
    command_sha256 TEXT NOT NULL,
    command_payload BYTEA NOT NULL CHECK(octet_length(command_payload)<=16384),
    rollout_generation BIGINT NOT NULL,
    rollout_scope_sha256 TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);
CREATE TRIGGER node_app_operation_immutable BEFORE UPDATE OR DELETE ON node_app_operations
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
CREATE TABLE node_app_readiness (
    request_id UUID PRIMARY KEY,
    operation_id UUID NOT NULL REFERENCES node_app_operations(operation_id),
    sequence BIGINT NOT NULL,
    payload BYTEA NOT NULL CHECK(octet_length(payload)<=16384),
    payload_sha256 TEXT NOT NULL,
    received_at DOUBLE PRECISION NOT NULL,
    UNIQUE(operation_id,sequence)
);
CREATE TRIGGER node_app_readiness_immutable BEFORE UPDATE OR DELETE ON node_app_readiness
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
CREATE TABLE node_app_drains (
    drain_id UUID PRIMARY KEY,
    operation_id UUID NOT NULL UNIQUE REFERENCES node_app_operations(operation_id),
    player_id TEXT NOT NULL REFERENCES players(id),
    authority_epoch BIGINT NOT NULL,
    snapshot JSONB NOT NULL,
    prepared_at DOUBLE PRECISION NOT NULL
);
CREATE TRIGGER node_app_drain_immutable BEFORE UPDATE OR DELETE ON node_app_drains
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
CREATE TABLE node_app_permits (
    operation_id UUID PRIMARY KEY REFERENCES node_app_operations(operation_id),
    permit_id UUID NOT NULL UNIQUE,
    drain_id UUID NOT NULL UNIQUE REFERENCES node_app_drains(drain_id),
    ready_request_id UUID NOT NULL REFERENCES node_app_readiness(request_id),
    payload BYTEA NOT NULL CHECK(octet_length(payload)<=16384),
    issued_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);
CREATE TRIGGER node_app_permit_immutable BEFORE UPDATE OR DELETE ON node_app_permits
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
CREATE TABLE node_app_effects (
    event_id UUID PRIMARY KEY,
    operation_id UUID NOT NULL REFERENCES node_app_operations(operation_id),
    producer_id UUID NOT NULL REFERENCES node_producers(producer_id),
    sequence BIGINT NOT NULL,
    phase TEXT NOT NULL,
    payload BYTEA NOT NULL CHECK(octet_length(payload)<=16384),
    received_at DOUBLE PRECISION NOT NULL,
    UNIQUE(operation_id,producer_id,sequence)
);
CREATE TRIGGER node_app_effect_immutable BEFORE UPDATE OR DELETE ON node_app_effects
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
CREATE TABLE node_app_responses (
    command_id UUID NOT NULL REFERENCES node_app_operations(command_id),
    decision TEXT NOT NULL,
    payload BYTEA NOT NULL CHECK(octet_length(payload)<=16384),
    received_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(command_id,decision)
);
CREATE TRIGGER node_app_response_immutable BEFORE UPDATE OR DELETE ON node_app_responses
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
CREATE TABLE node_app_revalidations (
    revalidation_id UUID PRIMARY KEY,
    operation_id UUID NOT NULL REFERENCES node_app_operations(operation_id),
    quiescent_event_id UUID NOT NULL REFERENCES node_app_effects(event_id),
    carrier_session_id UUID NOT NULL REFERENCES node_sessions(session_id),
    payload BYTEA NOT NULL CHECK(octet_length(payload)<=16384),
    control_floor BIGINT NOT NULL,
    issued_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);
CREATE TRIGGER node_app_revalidation_immutable BEFORE UPDATE OR DELETE ON node_app_revalidations
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
CREATE TABLE node_app_discharges (
    operation_id UUID PRIMARY KEY REFERENCES node_app_operations(operation_id),
    drain_id UUID NOT NULL REFERENCES node_app_drains(drain_id),
    basis TEXT NOT NULL CHECK(basis IN ('authorized_no_effect','operational_target','operational_fallback')),
    evidence JSONB NOT NULL,
    discharged_at DOUBLE PRECISION NOT NULL
);
CREATE TRIGGER node_app_discharge_immutable BEFORE UPDATE OR DELETE ON node_app_discharges
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
CREATE VIEW active_node_app_drains AS
    SELECT d.* FROM node_app_drains d WHERE NOT EXISTS
        (SELECT 1 FROM node_app_discharges x WHERE x.operation_id=d.operation_id)
    OR EXISTS (SELECT 1 FROM node_app_discharges x WHERE x.operation_id=d.operation_id
        AND x.basis='authorized_no_effect' AND EXISTS(SELECT 1 FROM node_app_effects e
            WHERE e.operation_id=d.operation_id AND e.phase IN ('intent_stop','stopped','starting_new',
                'running','target_failed','fallback_starting','fallback_running','effect_unknown')));

-- Explicit representative-media qualification. Samples are observations, not
-- time renewed by a repeated read. Acceptance scope never advances a fleet frontier.
CREATE TABLE node_app_qualifications (
    qualification_id UUID PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    device_generation BIGINT NOT NULL,
    environment_sha256 TEXT NOT NULL REFERENCES node_environment_catalog(environment_sha256),
    operator_audit_ref TEXT NOT NULL,
    opened_at DOUBLE PRECISION NOT NULL
);
CREATE TRIGGER node_app_qualification_immutable BEFORE UPDATE OR DELETE ON node_app_qualifications
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
CREATE TABLE node_app_qualification_samples (
    qualification_id UUID NOT NULL REFERENCES node_app_qualifications(qualification_id),
    sample_id UUID NOT NULL,
    witness JSONB NOT NULL,
    cohort JSONB NOT NULL,
    base_content_key TEXT NOT NULL,
    observed_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(qualification_id,sample_id)
);
CREATE TRIGGER node_app_qualification_sample_immutable BEFORE UPDATE OR DELETE ON node_app_qualification_samples
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
CREATE TABLE node_environment_acceptances (
    acceptance_id UUID PRIMARY KEY,
    qualification_id UUID NOT NULL UNIQUE REFERENCES node_app_qualifications(qualification_id),
    device_id TEXT NOT NULL,
    device_generation BIGINT NOT NULL,
    base_content_key TEXT NOT NULL,
    environment_sha256 TEXT NOT NULL REFERENCES node_environment_catalog(environment_sha256),
    cohort JSONB NOT NULL,
    evidence JSONB NOT NULL,
    accepted_at DOUBLE PRECISION NOT NULL
);
CREATE TRIGGER node_environment_acceptance_immutable BEFORE UPDATE OR DELETE ON node_environment_acceptances
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();

-- Runtime and Registry consult both owner ledgers; legacy writers still own only
-- their original table and cannot discharge a V2 fence.
CREATE VIEW active_runtime_drains AS
    SELECT player_id,snapshot FROM active_equipment_drains
    UNION ALL SELECT player_id,snapshot FROM active_node_app_drains;

CREATE TABLE node_app_stage_cancellations (
    operation_id UUID PRIMARY KEY REFERENCES node_app_operations(operation_id),
    event_id UUID NOT NULL REFERENCES node_app_effects(event_id),
    cancelled_at DOUBLE PRECISION NOT NULL
);
CREATE TRIGGER node_app_stage_cancel_immutable BEFORE UPDATE OR DELETE ON node_app_stage_cancellations
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
