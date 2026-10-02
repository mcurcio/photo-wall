-- New sealed environments use the existing content cache/worker, never V1 payload rows.
ALTER TABLE assets DROP CONSTRAINT assets_kind_check;
ALTER TABLE assets ADD CONSTRAINT assets_kind_check
    CHECK(kind IN ('os-image','player-deb','player-payload','sealed-environment'));

CREATE TABLE node_deployments (
    deployment_id UUID PRIMARY KEY,
    document BYTEA NOT NULL CHECK(octet_length(document)<=16384),
    published_at DOUBLE PRECISION NOT NULL
);
CREATE TRIGGER node_deployment_immutable BEFORE UPDATE OR DELETE ON node_deployments
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
CREATE TABLE node_base_managers (
    base_content_key TEXT PRIMARY KEY,
    document JSONB NOT NULL
);
CREATE TRIGGER node_base_managers_immutable BEFORE UPDATE OR DELETE ON node_base_managers
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
CREATE TABLE node_boot_policy (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
    revision BIGINT NOT NULL CHECK(revision>0),
    deployment_id UUID NOT NULL REFERENCES node_deployments(deployment_id),
    changed_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE node_boot_offers (
    offer_id UUID PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    device_generation BIGINT NOT NULL,
    kernel_boot_id UUID NOT NULL,
    boot_nonce TEXT NOT NULL,
    request_payload BYTEA NOT NULL,
    offer_payload BYTEA,
    refusal TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    UNIQUE(device_id,kernel_boot_id),
    UNIQUE(device_id,boot_nonce),
    CHECK((offer_payload IS NULL)=(refusal IS NOT NULL))
);
CREATE TRIGGER node_boot_offer_immutable BEFORE UPDATE OR DELETE ON node_boot_offers
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
-- Permanent origin metadata is a rehydration root; no automatic GC release here.
CREATE TABLE node_deployment_assets (
    deployment_id UUID NOT NULL REFERENCES node_deployments(deployment_id),
    kind TEXT NOT NULL,
    identity TEXT NOT NULL,
    owner TEXT NOT NULL,
    PRIMARY KEY(deployment_id,kind,identity),
    FOREIGN KEY(kind,identity,owner) REFERENCES asset_references(kind,identity,owner)
);
CREATE TABLE node_offer_contexts (
    offer_id UUID PRIMARY KEY,
    basis TEXT NOT NULL CHECK(basis IN ('legacy_adoption','node_v2')),
    legacy_offer_id UUID REFERENCES fleet_boot_offers(offer_id),
    node_offer_id UUID REFERENCES node_boot_offers(offer_id),
    CHECK((basis='legacy_adoption' AND legacy_offer_id=offer_id AND node_offer_id IS NULL) OR
          (basis='node_v2' AND node_offer_id=offer_id AND legacy_offer_id IS NULL))
);
INSERT INTO node_offer_contexts(offer_id,basis,legacy_offer_id)
    SELECT DISTINCT offer_id,'legacy_adoption',offer_id FROM node_boot_admissions;
ALTER TABLE node_boot_admissions DROP CONSTRAINT node_boot_admissions_offer_id_fkey;
ALTER TABLE node_boot_admissions ADD CONSTRAINT node_boot_admissions_offer_id_fkey
    FOREIGN KEY(offer_id) REFERENCES node_offer_contexts(offer_id);
CREATE TRIGGER node_offer_context_immutable BEFORE UPDATE OR DELETE ON node_offer_contexts
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();

CREATE TABLE node_environment_catalog (
    environment_sha256 TEXT PRIMARY KEY,
    reference JSONB NOT NULL
);
CREATE TRIGGER node_environment_immutable BEFORE UPDATE OR DELETE ON node_environment_catalog
    FOR EACH ROW EXECUTE FUNCTION node_immutable_record();
