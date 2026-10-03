ALTER TABLE node_intake_quotas DROP CONSTRAINT node_intake_quotas_kind_check;
ALTER TABLE node_intake_quotas ADD CONSTRAINT node_intake_quotas_kind_check
    CHECK(kind IN ('session','evidence','observation','command','display','preparation','host_facts'));
-- One row per producer (one boot's Host Management), replaced only by a higher sequence;
-- first_received_at is the receipt of the row's current values (console DDD §64).
CREATE TABLE node_host_facts (
    producer_id UUID PRIMARY KEY REFERENCES node_producers(producer_id),
    sequence BIGINT NOT NULL CHECK(sequence>0),
    payload BYTEA NOT NULL CHECK(octet_length(payload)<=2048),
    first_received_at DOUBLE PRECISION NOT NULL,
    received_at DOUBLE PRECISION NOT NULL
);
