-- Decision 0034, S4c part 2b-i: a release, a deployment and a boot offer name no manager root.
-- Every stored release, deployment document and boot offer names one, which the parsers no longer
-- accept, and carries Pi software that speaks the removed old path; none can be offered again. A
-- canonical deployment document (sort_keys, compact separators) cannot be rewritten byte-exactly in
-- SQL, so the rows are removed, not rewritten. The devices' two foreign keys into node_releases are
-- cleared first; no other foreign key reaches node_releases or node_boot_offers at 128. The release
-- listing's ETag goes too, so the next sync reads every tag again. Plain statements, forward-only.
UPDATE devices SET deployment_id = NULL, previous_deployment_id = NULL, install = NULL,
                   version_changed_at = NULL, changed_at = extract(epoch FROM clock_timestamp()),
                   changed_by = 'central'
 WHERE deployment_id IS NOT NULL OR previous_deployment_id IS NOT NULL;
DELETE FROM asset_references WHERE owner LIKE 'node-deployment:%';
DELETE FROM node_releases;
DELETE FROM node_boot_offers;
DELETE FROM singletons WHERE name = 'release_sync';
