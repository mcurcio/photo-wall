-- Decision 0034, S2e part 2. A release is one row, keyed by its tag: its upstream observation
-- (`node_release_observations`), its catalogue entry (`node_release_catalog`) and its deployment
-- (`node_deployments`, the canonical document inline) fold into `node_releases`. A base's manager
-- pins (`node_base_managers`) and the environment references (`node_environment_catalog`) were
-- each exactly what the deployment writer computed from a deployment, so they are read from the
-- stored deployment documents instead (central/infra/node_releases.py write_deployment,
-- environment_reference_in). `node_deployment_assets` repeated `asset_references`' owner
-- 'node-deployment:<hex>': a deployment's assets are found by that owner. `assets`,
-- `asset_references` and `job_outcomes` gain their audit fields; `asset_references.added_at` and
-- `job_outcomes.updated_at` already were the created and changed times, so they are renamed. A
-- row's writer before this release is unknown. Written only by central/infra/node_releases.py,
-- central/infra/asset_records.py and central/infra/outcomes.py. Plain statements, no trigger or
-- function, forward-only.
CREATE TABLE node_releases (
    tag TEXT PRIMARY KEY,
    is_prerelease BOOLEAN NOT NULL,
    problem TEXT,
    upstream_changed_at DOUBLE PRECISION,
    upstream_asset_id BIGINT,
    observed_at DOUBLE PRECISION NOT NULL,
    manifest_sha256 TEXT UNIQUE,
    revision TEXT,
    document BYTEA CHECK (octet_length(document) <= 32768),
    asset_locators JSONB,
    discovered_at DOUBLE PRECISION,
    deployment_id UUID UNIQUE,
    deployment_document BYTEA CHECK (octet_length(deployment_document) <= 16384),
    published_at DOUBLE PRECISION,
    created_at DOUBLE PRECISION NOT NULL,
    created_by TEXT NOT NULL,
    changed_at DOUBLE PRECISION NOT NULL,
    changed_by TEXT NOT NULL,
    CHECK (problem IS NOT NULL OR manifest_sha256 IS NOT NULL),
    CHECK ((upstream_changed_at IS NULL) = (upstream_asset_id IS NULL)),
    CHECK ((manifest_sha256 IS NULL) = (document IS NULL)),
    CHECK ((manifest_sha256 IS NULL) = (revision IS NULL)),
    CHECK ((manifest_sha256 IS NULL) = (asset_locators IS NULL)),
    CHECK ((manifest_sha256 IS NULL) = (discovered_at IS NULL)),
    CHECK ((deployment_id IS NULL) = (deployment_document IS NULL)),
    CHECK ((deployment_id IS NULL) = (published_at IS NULL)),
    CHECK (deployment_id IS NULL OR manifest_sha256 IS NOT NULL));

-- A deployment's release is the catalogue entry whose manifest built it: its id is the manifest
-- digest's first 128 bits with hexit 13 set to '8' and hexit 17 to 8 | 2 (with an app) | its own
-- low bit (central/content_catalog/deployment.py deployment_id_for); either app bit matches.
-- A catalogue entry no observation names leaves is_prerelease NULL and fails the insert.
INSERT INTO node_releases (tag, is_prerelease, problem, upstream_changed_at, upstream_asset_id,
                           observed_at, manifest_sha256, revision, document, asset_locators,
                           discovered_at, deployment_id, deployment_document, published_at,
                           created_at, created_by, changed_at, changed_by)
SELECT coalesce(o.tag, c.tag), o.is_prerelease, o.problem, o.upstream_changed_at,
       o.upstream_asset_id, o.observed_at, c.manifest_sha256, c.revision, c.document,
       c.asset_locators, c.discovered_at, d.deployment_id, d.document, d.published_at,
       least(o.observed_at, c.discovered_at, d.published_at), 'unknown',
       greatest(o.observed_at, c.discovered_at, d.published_at), 'unknown'
FROM node_release_observations o
FULL JOIN node_release_catalog c ON c.manifest_sha256 = o.manifest_sha256
LEFT JOIN node_deployments d ON c.manifest_sha256 IS NOT NULL
    AND replace(d.deployment_id::text, '-', '') IN (
        overlay(overlay(left(c.manifest_sha256, 32) PLACING '8' FROM 13 FOR 1)
                PLACING to_hex(8 | (('x' || substr(c.manifest_sha256, 17, 1))::bit(4)::int & 1))
                FROM 17 FOR 1),
        overlay(overlay(left(c.manifest_sha256, 32) PLACING '8' FROM 13 FOR 1)
                PLACING to_hex(10 | (('x' || substr(c.manifest_sha256, 17, 1))::bit(4)::int & 1))
                FROM 17 FOR 1));

-- A deployment no catalogue entry built would be lost: its NOT NULL fails here instead.
ALTER TABLE node_deployments ADD COLUMN released BOOLEAN;
UPDATE node_deployments d SET released = true
FROM node_releases r WHERE r.deployment_id = d.deployment_id;
ALTER TABLE node_deployments ALTER COLUMN released SET NOT NULL;

ALTER TABLE devices
    DROP CONSTRAINT devices_deployment_id_fkey,
    DROP CONSTRAINT devices_previous_deployment_id_fkey,
    ADD FOREIGN KEY (deployment_id) REFERENCES node_releases(deployment_id),
    ADD FOREIGN KEY (previous_deployment_id) REFERENCES node_releases(deployment_id);
-- node_app_operations stays until S4c (S2a's erratum); its deployment moves with the release.
ALTER TABLE node_app_operations
    DROP CONSTRAINT node_app_operations_deployment_id_fkey,
    ADD FOREIGN KEY (deployment_id) REFERENCES node_releases(deployment_id);
-- Both go in S4c; a digest inside a deployment document carries no foreign key.
ALTER TABLE node_app_qualifications DROP CONSTRAINT node_app_qualifications_environment_sha256_fkey;
ALTER TABLE node_environment_acceptances
    DROP CONSTRAINT node_environment_acceptances_environment_sha256_fkey;

ALTER TABLE assets
    ADD COLUMN created_by TEXT, ADD COLUMN changed_at DOUBLE PRECISION, ADD COLUMN changed_by TEXT;
UPDATE assets SET created_by = 'unknown', changed_at = created_at, changed_by = 'unknown';
ALTER TABLE assets
    ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN changed_at SET NOT NULL,
    ALTER COLUMN changed_by SET NOT NULL;

ALTER TABLE asset_references RENAME COLUMN added_at TO created_at;
ALTER TABLE asset_references
    ADD COLUMN created_by TEXT, ADD COLUMN changed_at DOUBLE PRECISION, ADD COLUMN changed_by TEXT;
UPDATE asset_references SET created_by = 'unknown', changed_at = created_at, changed_by = 'unknown';
ALTER TABLE asset_references
    ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN changed_at SET NOT NULL,
    ALTER COLUMN changed_by SET NOT NULL;

ALTER TABLE job_outcomes RENAME COLUMN updated_at TO changed_at;
ALTER INDEX job_outcomes_updated RENAME TO job_outcomes_changed;
ALTER TABLE job_outcomes
    ADD COLUMN created_at DOUBLE PRECISION, ADD COLUMN created_by TEXT, ADD COLUMN changed_by TEXT;
UPDATE job_outcomes SET created_at = changed_at, created_by = 'unknown', changed_by = 'unknown';
ALTER TABLE job_outcomes
    ALTER COLUMN created_at SET NOT NULL,
    ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN changed_by SET NOT NULL;

DROP TABLE node_deployment_assets, node_release_observations, node_release_catalog,
           node_deployments, node_base_managers, node_environment_catalog;
