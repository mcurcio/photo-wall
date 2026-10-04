-- Node releases arrive by themselves (docs: auto-ingest design §8).
--
-- 1. `node_release_observations`: Central's latest view of one tag, written only by the release
--    sync. `manifest_sha256` is the last GOOD manifest it ingested (a deployment exists for it),
--    `problem` the latest refusal; a tag whose newer upload was refused keeps both. Mutable under
--    the same "not older" upstream-version guard as `app_releases` (029), with the node
--    manifest asset's own `(updated_at, id)` as the version.
CREATE TABLE node_release_observations (
    tag TEXT PRIMARY KEY,
    is_prerelease BOOLEAN NOT NULL,
    manifest_sha256 TEXT REFERENCES node_release_catalog(manifest_sha256),
    problem TEXT,
    upstream_changed_at DOUBLE PRECISION,
    upstream_asset_id BIGINT,
    observed_at DOUBLE PRECISION NOT NULL,
    CHECK (problem IS NOT NULL OR manifest_sha256 IS NOT NULL),
    CHECK ((upstream_changed_at IS NULL) = (upstream_asset_id IS NULL))
);

-- 2. The selection's previous deployment (one slot): kept downloaded so Undo is instant.
ALTER TABLE node_boot_policy
    ADD COLUMN previous_deployment_id UUID REFERENCES node_deployments(deployment_id);

-- 3. Force the next sync to list in full (the 029 precedent), so every already-listed node
--    release is observed and ingested once.
UPDATE app_release_poll SET etag_stored_at = NULL;

-- `node_release_verifications` stays in place, no longer written or read; dropped later.
