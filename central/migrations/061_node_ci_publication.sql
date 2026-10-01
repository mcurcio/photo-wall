-- Provisioned identities are pinned; key/record replacement is explicit reprovisioning.
CREATE TABLE node_ci_publication_heads (
    audience TEXT NOT NULL,
    record_name TEXT NOT NULL,
    record_uid TEXT NOT NULL,
    authority_sha256 TEXT NOT NULL,
    revocation_uid TEXT NOT NULL,
    revocation_generation BIGINT NOT NULL,
    revocation_sha256 TEXT NOT NULL,
    publication_generation BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY(audience,record_name)
);
CREATE TABLE node_ci_revoked_qualifications (
    audience TEXT NOT NULL,
    record_name TEXT NOT NULL,
    qualification_sha256 TEXT NOT NULL,
    PRIMARY KEY(audience,record_name,qualification_sha256)
);
CREATE TABLE node_ci_publications (
    request_id UUID PRIMARY KEY,
    audience TEXT NOT NULL,
    record_name TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    generation BIGINT NOT NULL,
    payload BYTEA NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL,
    UNIQUE(audience,record_name,generation)
);
CREATE TRIGGER node_ci_publication_immutable BEFORE UPDATE OR DELETE ON node_ci_publications
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
CREATE TRIGGER node_ci_revocation_immutable BEFORE UPDATE OR DELETE ON node_ci_revoked_qualifications
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
