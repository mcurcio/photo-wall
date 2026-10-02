-- Local anti-replay state for externally signed CI/guard records. This table
-- does not implement or certify the external deployment mutation guard.
CREATE TABLE node_rollout_evidence_heads (
    audience TEXT NOT NULL,
    authority_role TEXT NOT NULL CHECK(authority_role IN ('ci','guard')),
    authority_sha256 TEXT NOT NULL,
    record_name TEXT NOT NULL,
    record_uid TEXT NOT NULL,
    generation BIGINT NOT NULL CHECK(generation>0),
    payload_sha256 TEXT NOT NULL,
    PRIMARY KEY(audience,authority_role,record_name)
);
