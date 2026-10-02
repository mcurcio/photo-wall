-- Authenticated per-Output observations and explicit control decisions remain distinct.
CREATE TABLE node_display_exchanges (
    producer_id UUID NOT NULL REFERENCES node_producers(producer_id),
    request_id UUID NOT NULL,
    session_id UUID NOT NULL REFERENCES node_sessions(session_id),
    output_id TEXT NOT NULL,
    sampled_boottime_ms BIGINT NOT NULL CHECK(sampled_boottime_ms>=0),
    request BYTEA NOT NULL CHECK(octet_length(request)<=16384),
    response BYTEA NOT NULL CHECK(octet_length(response)<=16384),
    decision_id UUID NOT NULL UNIQUE,
    received_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(producer_id,request_id)
);
CREATE INDEX node_display_output_latest ON node_display_exchanges(producer_id,output_id,sampled_boottime_ms DESC);
CREATE TRIGGER node_display_exchange_immutable BEFORE UPDATE OR DELETE ON node_display_exchanges
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
CREATE TABLE node_display_handoffs (
    decision_id UUID PRIMARY KEY REFERENCES node_display_exchanges(decision_id),
    receipt_request_id UUID NOT NULL,
    received_at DOUBLE PRECISION NOT NULL
);
CREATE TRIGGER node_display_handoff_immutable BEFORE UPDATE OR DELETE ON node_display_handoffs
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
ALTER TABLE node_intake_quotas DROP CONSTRAINT node_intake_quotas_kind_check;
ALTER TABLE node_intake_quotas ADD CONSTRAINT node_intake_quotas_kind_check
    CHECK(kind IN ('session','evidence','observation','command','display'));

CREATE TABLE node_calibration_trials (
    trial_id UUID PRIMARY KEY,
    frame_id TEXT NOT NULL REFERENCES frames(id),
    generation BIGINT NOT NULL CHECK(generation>0),
    player_id TEXT NOT NULL REFERENCES players(id),
    authority_epoch BIGINT NOT NULL,
    producer_id UUID NOT NULL REFERENCES node_producers(producer_id),
    baseline JSONB NOT NULL,
    calibration_revision BIGINT NOT NULL,
    sequence BIGINT NOT NULL CHECK(sequence>0),
    calibration JSONB NOT NULL,
    candidate_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('active','saved','ended','expired','invalidated')),
    created_at DOUBLE PRECISION NOT NULL,
    touched_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL,
    hard_expires_at DOUBLE PRECISION NOT NULL,
    presented_sequence BIGINT,
    presented_sha256 TEXT,
    presentation_request_id UUID,
    presented_at DOUBLE PRECISION,
    saved_calibration JSONB,
    UNIQUE(frame_id,generation)
);
CREATE UNIQUE INDEX node_active_calibration_trial ON node_calibration_trials(frame_id) WHERE state='active';

CREATE TABLE node_display_withdrawals (
    decision_id UUID PRIMARY KEY REFERENCES node_display_exchanges(decision_id),
    receipt_request_id UUID NOT NULL,
    received_at DOUBLE PRECISION NOT NULL
);
CREATE TRIGGER node_display_withdrawal_immutable BEFORE UPDATE OR DELETE ON node_display_withdrawals
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
