-- Roadmap 4 (run ledger .claude/runs/photo-4.md, slice I1): retire the pre-4 shapes. Pools
-- (080) replace catalog snapshots, Source members and previews; the photo library's lists replace
-- the stored tag lists; connections live in library_connections, not the worker's reported ids.
--
-- Plain statements only: no trigger, stored procedure or function. Forward-only (central/db.py).

-- 1. Hand-placed pre-4 photos (F-15). A pre-4 record (metadata without `upstream_id`) that a
-- Scene still holds by hand (authored_candidates) or that a transfer pin holds (media_references,
-- through its job's variant) becomes a revision of a `library_assets` parent with the same
-- identity: its revision is the original's SHA-1, so asset_identity(connection, upstream id,
-- sha1) is unchanged (T0 case 8). Width and height are upright: raw height and width swap when
-- EXIF orientation is 5 to 8. The parent takes the kind and taken time of the Immich asset's
-- latest record; the original's facts stay NULL until a check reads them.
UPDATE asset_revisions a SET
  connection_ref = a.metadata->>'connection_id',
  upstream_id = (a.metadata->>'upstream_id')::uuid,
  revision = a.metadata->>'original_sha1',
  width = CASE WHEN (a.metadata->>'orientation')::int >= 5 THEN (a.metadata->>'raw_height')::int
               ELSE (a.metadata->>'raw_width')::int END,
  height = CASE WHEN (a.metadata->>'orientation')::int >= 5 THEN (a.metadata->>'raw_width')::int
                ELSE (a.metadata->>'raw_height')::int END,
  duration = (a.metadata->>'duration')::double precision
WHERE a.upstream_id IS NULL AND a.metadata ? 'original_sha1'
  AND (EXISTS (SELECT 1 FROM authored_candidates c WHERE c.asset_id = a.asset_id)
       OR EXISTS (SELECT 1 FROM media_jobs j JOIN media_references r ON r.digest = j.variant_sha
                  WHERE j.asset_id = a.asset_id));

-- Every revision with an Immich id now is one step 1 converted: each Immich asset's parent.
INSERT INTO library_assets (connection_ref, upstream_id, kind, taken_at, first_seen, last_seen)
SELECT DISTINCT ON (a.connection_ref, a.upstream_id) a.connection_ref, a.upstream_id,
  a.metadata->>'kind', (a.metadata->>'captured_at')::double precision,
  min(a.first_seen) OVER (PARTITION BY a.connection_ref, a.upstream_id),
  max(a.last_seen) OVER (PARTITION BY a.connection_ref, a.upstream_id)
FROM asset_revisions a WHERE a.upstream_id IS NOT NULL
ORDER BY a.connection_ref, a.upstream_id, a.last_seen DESC;

-- 2. The pre-4 working sets.
DROP TABLE catalog_snapshots, source_members, source_previews, library_tags;

-- 3. A Source revision is its query and pool (080); the refresh bookkeeping goes.
ALTER TABLE media_sources DROP COLUMN spec, DROP COLUMN status, DROP COLUMN diagnostics,
  DROP COLUMN counts, DROP COLUMN next_refresh, DROP COLUMN last_success, DROP COLUMN generation,
  DROP COLUMN refresh_started, DROP COLUMN refresh_lease_until,
  DROP COLUMN refresh_requested_revision, DROP COLUMN refresh_completed_revision;

-- 4. Connections are library_connections' rows.
ALTER TABLE media_settings DROP COLUMN connection_ids;

-- 5. Preparation restarts from pools: every job goes (recipe 2 asks for new variants), then the
-- pre-4 records nothing holds.
DELETE FROM media_jobs;
DELETE FROM asset_revisions WHERE upstream_id IS NULL;

-- 6. A revision is a child of its Immich asset: the typed columns are the one record of it.
ALTER TABLE asset_revisions DROP COLUMN metadata,
  ALTER COLUMN connection_ref SET NOT NULL, ALTER COLUMN upstream_id SET NOT NULL,
  ALTER COLUMN revision SET NOT NULL, ALTER COLUMN width SET NOT NULL,
  ALTER COLUMN height SET NOT NULL,
  ADD FOREIGN KEY (connection_ref, upstream_id) REFERENCES library_assets,
  ADD UNIQUE (connection_ref, upstream_id, revision);
