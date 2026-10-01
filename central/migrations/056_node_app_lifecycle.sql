-- V2 online app operations: the latest stage is the device's desired state and
-- the broker's reported effects are append-only evidence. Separate from the
-- legacy V1 drain/permit ledger; never rewrites its committed rows.
CREATE TABLE node_app_operations (
    operation_id UUID PRIMARY KEY,
    -- Total stage order: the highest sequence for a device is its desired state.
    sequence BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
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
    created_at DOUBLE PRECISION NOT NULL
);
CREATE TRIGGER node_app_operation_immutable BEFORE UPDATE OR DELETE ON node_app_operations
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
