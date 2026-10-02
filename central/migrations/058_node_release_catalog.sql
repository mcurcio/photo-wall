-- Immutable V2 publication observations are not legacy release policy or byte availability.
CREATE TABLE node_release_catalog (
    manifest_sha256 TEXT PRIMARY KEY,
    tag TEXT NOT NULL,
    revision TEXT NOT NULL,
    document BYTEA NOT NULL CHECK(octet_length(document)<=32768),
    asset_locators JSONB NOT NULL,
    discovered_at DOUBLE PRECISION NOT NULL,
    UNIQUE(tag,revision)
);
CREATE TRIGGER node_release_catalog_immutable BEFORE UPDATE OR DELETE ON node_release_catalog
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
CREATE TABLE node_release_verifications (
    manifest_sha256 TEXT PRIMARY KEY REFERENCES node_release_catalog(manifest_sha256),
    verified_at DOUBLE PRECISION NOT NULL,
    operator_audit_ref TEXT NOT NULL,
    asset_evidence JSONB NOT NULL
);
CREATE TRIGGER node_release_verification_immutable BEFORE UPDATE OR DELETE ON node_release_verifications
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
