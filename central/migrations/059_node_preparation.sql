ALTER TABLE node_intake_quotas DROP CONSTRAINT node_intake_quotas_kind_check;
ALTER TABLE node_intake_quotas ADD CONSTRAINT node_intake_quotas_kind_check
    CHECK(kind IN ('session','evidence','observation','command','display','preparation'));
CREATE TABLE node_manager_observations (
    producer_id UUID NOT NULL REFERENCES node_producers(producer_id),
    sequence BIGINT NOT NULL CHECK(sequence>0),
    payload BYTEA NOT NULL CHECK(octet_length(payload)<=8192),
    received_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(producer_id,sequence)
);
CREATE TRIGGER node_manager_observation_immutable BEFORE UPDATE OR DELETE ON node_manager_observations
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
