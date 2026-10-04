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

-- 1b. Backfill, so the release read (which lists observations) is not empty after the upgrade:
--    per tag, the newest catalogued manifest whose deployment the console already published
--    (the deployment id is `deployment_id_for(manifest_sha256, with_app)`,
--    `central/content_catalog/deployment.py`, computed here in SQL). The upstream version is
--    NULL, which the not-older guard lets any later observation replace, and the prerelease flag
--    is `app_releases`' (else the tag's semver); the first sync's full listing (3. below)
--    re-observes every listed tag with its real version and flag. A catalogued manifest with no
--    published deployment is NOT backfilled: an observation's manifest always has a deployment
--    (the ingest writes both in one transaction), and that tag's first sync ingests it.
INSERT INTO node_release_observations(tag, is_prerelease, manifest_sha256, problem,
                                      upstream_changed_at, upstream_asset_id, observed_at)
SELECT DISTINCT ON (c.tag) c.tag,
       COALESCE(r.is_prerelease, position('-' IN c.tag) > 0),
       c.manifest_sha256, NULL, NULL, NULL, c.discovered_at
FROM (
    SELECT catalog.*,
           (substr(catalog.manifest_sha256, 1, 12) || '8' || substr(catalog.manifest_sha256, 14, 3)
            || to_hex(8 | CASE WHEN jsonb_typeof(convert_from(catalog.document, 'UTF8')::jsonb
                                                 -> 'app_environment') = 'object'
                               THEN 2 ELSE 0 END
                        | (('x' || substr(catalog.manifest_sha256, 17, 1))::bit(4)::int & 1))
            || substr(catalog.manifest_sha256, 18, 15))::uuid AS deployment_id
    FROM node_release_catalog AS catalog
) AS c
JOIN node_deployments AS d USING (deployment_id)
LEFT JOIN app_releases AS r ON r.tag = c.tag
ORDER BY c.tag, c.discovered_at DESC, c.manifest_sha256 DESC;

-- 2. The selection's previous deployment (one slot): kept downloaded so Undo is instant. Not
--    backfilled: 054 stores only the current selection and no history of it, and an offer
--    records the content it froze, not the deployment it came from, so no earlier selection is
--    derivable. It is recorded from the next selection change on.
ALTER TABLE node_boot_policy
    ADD COLUMN previous_deployment_id UUID REFERENCES node_deployments(deployment_id);

-- 3. Force the next sync to list in full (the 029 precedent), so every already-listed node
--    release is observed and ingested once.
UPDATE app_release_poll SET etag_stored_at = NULL;

-- `node_release_verifications` stays in place, no longer written or read; dropped later.
