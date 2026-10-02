-- Additive V2 LAN-serial node admission. No existing offer/session gains authority.
CREATE TABLE node_boot_admissions (
    admission_id UUID PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    device_generation BIGINT NOT NULL CHECK (device_generation > 0),
    kernel_boot_id UUID NOT NULL,
    offer_id UUID NOT NULL REFERENCES fleet_boot_offers(offer_id),
    installation_audience TEXT NOT NULL,
    trust_mode TEXT NOT NULL CHECK (trust_mode = 'lan_serial'),
    admitted_at DOUBLE PRECISION NOT NULL,
    superseded_at DOUBLE PRECISION,
    UNIQUE(device_id,device_generation,kernel_boot_id)
);
CREATE UNIQUE INDEX node_current_boot ON node_boot_admissions(device_id,device_generation)
    WHERE superseded_at IS NULL;

CREATE TABLE node_producers (
    producer_id UUID PRIMARY KEY,
    admission_id UUID NOT NULL REFERENCES node_boot_admissions(admission_id),
    owner TEXT NOT NULL CHECK (owner IN
        ('host_core','app_manager','app_effect_broker','display_host','player_runtime')),
    incarnation_id UUID NOT NULL,
    producer JSONB NOT NULL,
    projection JSONB NOT NULL DEFAULT '[]',
    admitted_at DOUBLE PRECISION NOT NULL,
    UNIQUE(admission_id,owner,incarnation_id)
);

CREATE TABLE node_sessions (
    session_id UUID PRIMARY KEY,
    producer_id UUID NOT NULL REFERENCES node_producers(producer_id),
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    device_generation BIGINT NOT NULL,
    owner TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('operator_reboot','app_effect','evidence')),
    credential_sha256 TEXT NOT NULL UNIQUE CHECK (credential_sha256 ~ '^[0-9a-f]{64}$'),
    claim_sha256 TEXT NOT NULL CHECK (claim_sha256 ~ '^[0-9a-f]{64}$'),
    grant_payload BYTEA NOT NULL,
    issued_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL CHECK (expires_at > issued_at),
    revoked_at DOUBLE PRECISION,
    CHECK ((owner='host_core' AND scope='operator_reboot') OR
           (owner='app_effect_broker' AND scope='app_effect') OR
           (owner IN ('app_manager','display_host','player_runtime') AND scope='evidence'))
);
CREATE UNIQUE INDEX node_current_scope ON node_sessions(device_id,device_generation,owner)
    WHERE revoked_at IS NULL;

CREATE TABLE node_evidence (
    producer_id UUID NOT NULL REFERENCES node_producers(producer_id),
    evidence_id UUID NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('event','snapshot')),
    sequence BIGINT NOT NULL CHECK(sequence >= 0),
    payload BYTEA NOT NULL CHECK(octet_length(payload) <= 65536),
    received_at DOUBLE PRECISION NOT NULL,
    disposition TEXT NOT NULL CHECK(disposition IN ('applied','historical')),
    PRIMARY KEY(producer_id,kind,evidence_id)
);
CREATE UNIQUE INDEX node_event_sequence ON node_evidence(producer_id,sequence) WHERE kind='event';

CREATE TABLE node_reboot_commands (
    command_id UUID PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES node_sessions(session_id),
    request_sha256 TEXT NOT NULL,
    operator_audit_ref TEXT NOT NULL,
    payload BYTEA NOT NULL,
    issued_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL CHECK(expires_at > issued_at),
    gate_generation BIGINT NOT NULL,
    gate_scope_sha256 TEXT NOT NULL
);
CREATE INDEX node_reboot_poll ON node_reboot_commands(session_id,expires_at);

CREATE TABLE node_command_responses (
    command_id UUID NOT NULL REFERENCES node_reboot_commands(command_id),
    decision TEXT NOT NULL CHECK(decision IN ('received','accepted','rejected')),
    payload BYTEA NOT NULL,
    received_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(command_id,decision)
);
-- Runtime consumes evidence asynchronously; storing an event never cancels a Run.
CREATE TABLE node_reconciliation_work (
    work_id BIGSERIAL PRIMARY KEY,
    producer_id UUID NOT NULL REFERENCES node_producers(producer_id),
    evidence_id UUID NOT NULL,
    kind TEXT NOT NULL,
    received_at DOUBLE PRECISION NOT NULL,
    completed_at DOUBLE PRECISION,
    UNIQUE(producer_id,kind,evidence_id),
    FOREIGN KEY(producer_id,kind,evidence_id) REFERENCES node_evidence(producer_id,kind,evidence_id)
);

CREATE FUNCTION node_immutable_record() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'immutable node record';
END;
$$;
CREATE TRIGGER node_evidence_immutable BEFORE UPDATE OR DELETE ON node_evidence
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
CREATE TRIGGER node_command_immutable BEFORE UPDATE OR DELETE ON node_reboot_commands
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
CREATE TRIGGER node_response_immutable BEFORE UPDATE OR DELETE ON node_command_responses
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();

CREATE TABLE node_host_observations (
    producer_id UUID NOT NULL REFERENCES node_producers(producer_id),
    sequence BIGINT NOT NULL CHECK(sequence > 0),
    payload BYTEA NOT NULL CHECK(octet_length(payload) <= 16384),
    received_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(producer_id,sequence)
);
CREATE TRIGGER node_observation_immutable BEFORE UPDATE OR DELETE ON node_host_observations
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();

-- Bounded intake across replicas. Buckets are never effect authority.
CREATE TABLE node_intake_quotas (
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    day BIGINT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('session','evidence','observation','command')),
    used BIGINT NOT NULL CHECK(used > 0),
    PRIMARY KEY(device_id,day,kind)
);

CREATE TABLE node_app_links (
    nonce TEXT PRIMARY KEY CHECK(nonce ~ '^[0-9a-f]{64}$'),
    producer_id UUID NOT NULL REFERENCES node_producers(producer_id),
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    device_generation BIGINT NOT NULL,
    player_id TEXT NOT NULL REFERENCES players(id),
    authority_epoch BIGINT NOT NULL,
    payload BYTEA NOT NULL CHECK(octet_length(payload) <= 8192),
    admitted_at DOUBLE PRECISION NOT NULL,
    superseded_at DOUBLE PRECISION
);
CREATE UNIQUE INDEX node_current_app_link ON node_app_links(device_id,device_generation)
    WHERE superseded_at IS NULL;
ALTER TABLE node_reconciliation_work ADD COLUMN result TEXT,
    ADD COLUMN next_attempt_at DOUBLE PRECISION NOT NULL DEFAULT 0;

CREATE TABLE node_output_losses (
    player_id TEXT NOT NULL REFERENCES players(id),
    authority_epoch BIGINT NOT NULL,
    output_id TEXT NOT NULL,
    binding_generation BIGINT NOT NULL,
    configuration_revision BIGINT NOT NULL,
    cause_producer_id UUID NOT NULL REFERENCES node_producers(producer_id),
    cause_evidence_id UUID NOT NULL,
    cause_kind TEXT NOT NULL,
    detail JSONB NOT NULL,
    interrupted_at DOUBLE PRECISION NOT NULL,
    resolved_at DOUBLE PRECISION,
    PRIMARY KEY(player_id,authority_epoch,output_id,binding_generation),
    FOREIGN KEY(cause_producer_id,cause_kind,cause_evidence_id)
        REFERENCES node_evidence(producer_id,kind,evidence_id)
);
