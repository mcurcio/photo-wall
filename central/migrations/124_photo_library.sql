-- Decision 0034, S2h: the photo library as one row per photo, one row per Source revision and one
-- row per connection; media pins derived.
--
-- * A photo is one `library_assets` row: its current revision (was `asset_revisions`: its id, its
--   upright size, duration and Immich's `updatedAt`), its measured Immich copies as one document
--   keyed by copy (was `asset_copies`) and its key content (was `photo_key_content`). A photo whose
--   revision changes keeps one row: the new revision replaces the old, with its copies and key
--   content cleared (they were measured on the old one), and every pool row naming the old revision
--   follows it (`ON UPDATE CASCADE`). Where a photo had several revisions, the newest (as the pool
--   check's `known_revisions` read it: latest change, then id) is kept and every pool row moves to it.
-- * A Source revision is one `media_sources` row carrying its name and revision number (was
--   `media_source_name_versions`) and, on its name's newest revision, the name's head (`head`,
--   `deleted`, `renamed_to`; was `media_source_names`). A name is unique among heads by the index
--   `media_sources_current_name`, and a (name, revision) pair once. A draft is its own row (its ref
--   the draft's, no query, its pool in `pool_key`, `draft` holding the name it edits; was
--   `photo_drafts`).
-- * A connection's lists are one document per kind on its row (was `library_lists` and
--   `library_list_items`): {kind: {status, error, observed_at, checked_at, items: [{ref, search, item}]}}.
-- * `media_references` goes: an offer or a secured lock naming a variant is its pin, and a transfer
--   is protected by the variant's `assets.last_served_at`, stamped at the grant.
-- * `authored_candidates` goes: a Scene keeps its photo ids and planning reads their current facts.
-- * `library_connections`, `media_sources`, `photo_pools` and `media_recipes` carry audit fields.
-- Plain statements, no trigger or function, forward-only.

-- 1. One row per photo ---------------------------------------------------------------------------

ALTER TABLE library_assets
    ADD COLUMN asset_id TEXT UNIQUE,
    ADD COLUMN revision TEXT,
    ADD COLUMN width INTEGER CHECK (width > 0),
    ADD COLUMN height INTEGER CHECK (height > 0),
    ADD COLUMN duration DOUBLE PRECISION CHECK (duration > 0),
    ADD COLUMN revision_updated_at TEXT,
    ADD COLUMN copies JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(copies) = 'object'),
    ADD COLUMN key_analysis_version TEXT,
    ADD COLUMN key_status TEXT CHECK (key_status IN ('measured', 'failed')),
    ADD COLUMN key JSONB,
    ADD COLUMN key_failure TEXT CHECK (key_failure IN ('decode', 'timeout')),
    ADD COLUMN key_measured_at DOUBLE PRECISION,
    ADD COLUMN key_retry_at DOUBLE PRECISION;

WITH newest AS (
    SELECT DISTINCT ON (connection_ref, upstream_id) *
    FROM asset_revisions ORDER BY connection_ref, upstream_id, changed_at DESC, asset_id)
UPDATE library_assets l SET
    asset_id = n.asset_id, revision = n.revision, width = n.width, height = n.height,
    duration = n.duration, revision_updated_at = n.updated_at,
    copies = COALESCE((SELECT jsonb_object_agg(c.copy, jsonb_build_object(
                           'media_type', c.media_type, 'width', c.width, 'height', c.height,
                           'measured_at', c.measured_at))
                       FROM asset_copies c WHERE c.asset_id = n.asset_id), '{}'::jsonb),
    key_analysis_version = k.analysis_version, key_status = k.status, key = k.key,
    key_failure = k.failure, key_measured_at = k.measured_at, key_retry_at = k.retry_at
FROM newest n LEFT JOIN photo_key_content k ON k.asset_id = n.asset_id
WHERE (l.connection_ref, l.upstream_id) = (n.connection_ref, n.upstream_id);

-- A pool row naming an older revision of a photo names its kept one.
UPDATE photo_pool_rows r SET asset_id = l.asset_id
FROM asset_revisions a JOIN library_assets l USING (connection_ref, upstream_id)
WHERE r.asset_id = a.asset_id AND l.asset_id <> a.asset_id;

ALTER TABLE library_assets
    ADD CONSTRAINT library_assets_revision CHECK (
        (asset_id IS NULL) = (revision IS NULL) AND (asset_id IS NULL) = (width IS NULL)
        AND (asset_id IS NULL) = (height IS NULL)
        AND (asset_id IS NOT NULL OR revision_updated_at IS NULL)),
    ADD CONSTRAINT library_assets_key CHECK (
        (key_status IS NULL OR asset_id IS NOT NULL)
        AND (key_status IS NULL) = (key_analysis_version IS NULL)
        AND (key_status IS NULL) = (key_measured_at IS NULL)
        AND (key_status IS NULL OR (key_status = 'measured') = (key IS NOT NULL))
        AND (key_status IS NULL OR (key_status = 'failed') = (key_failure IS NOT NULL
                                                              AND key_retry_at IS NOT NULL)));

ALTER TABLE photo_pool_rows DROP CONSTRAINT photo_pool_rows_asset_id_fkey,
    ADD CONSTRAINT photo_pool_rows_asset_id_fkey FOREIGN KEY (asset_id)
        REFERENCES library_assets (asset_id) ON UPDATE CASCADE;

-- 2. One row per Source revision: its name, its name's head, a draft its own row ------------------

ALTER TABLE media_sources
    ADD COLUMN name TEXT,
    ADD COLUMN revision BIGINT CHECK (revision >= 1),
    ADD COLUMN head BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN deleted BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN renamed_to TEXT,
    ADD COLUMN draft JSONB CHECK (jsonb_typeof(draft) = 'object'),
    ADD COLUMN created_at DOUBLE PRECISION,
    ADD COLUMN created_by TEXT,
    ADD COLUMN changed_at DOUBLE PRECISION,
    ADD COLUMN changed_by TEXT;

UPDATE media_sources s SET name = v.name, revision = v.revision
FROM media_source_name_versions v WHERE v.source_ref = s.source_ref;
UPDATE media_sources s SET head = TRUE, deleted = n.deleted, renamed_to = n.renamed_to,
    name = COALESCE(s.name, n.name), revision = COALESCE(s.revision, n.revision)
FROM media_source_names n WHERE n.current_ref = s.source_ref;
UPDATE media_sources SET created_at = EXTRACT(EPOCH FROM now()), created_by = 'unknown',
                         changed_at = EXTRACT(EPOCH FROM now()), changed_by = 'unknown';

INSERT INTO media_sources(source_ref, refresh_active, query, pool_key, draft, created_at,
                          created_by, changed_at, changed_by)
SELECT d.draft_ref::text, FALSE, NULL, d.pool_key, jsonb_build_object('editing_name', d.editing_name),
       d.created_at, 'unknown', d.created_at, 'unknown'
FROM photo_drafts d;

ALTER TABLE media_sources
    ALTER COLUMN created_at SET NOT NULL, ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN changed_at SET NOT NULL, ALTER COLUMN changed_by SET NOT NULL,
    ADD CONSTRAINT media_sources_name_revision UNIQUE (name, revision),
    ADD CONSTRAINT media_sources_versioned CHECK ((name IS NULL) = (revision IS NULL)),
    ADD CONSTRAINT media_sources_head CHECK (
        (NOT head OR name IS NOT NULL) AND (head OR (NOT deleted AND renamed_to IS NULL))),
    ADD CONSTRAINT media_sources_draft CHECK (
        draft IS NULL OR (name IS NULL AND query IS NULL AND pool_key IS NOT NULL));
CREATE UNIQUE INDEX media_sources_current_name ON media_sources (name) WHERE head;

-- 3. One row per connection: its lists ------------------------------------------------------------

ALTER TABLE library_connections
    ADD COLUMN lists JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(lists) = 'object'),
    ADD COLUMN created_by TEXT,
    ADD COLUMN changed_at DOUBLE PRECISION,
    ADD COLUMN changed_by TEXT;

UPDATE library_connections c SET lists = COALESCE((
    SELECT jsonb_object_agg(l.kind, jsonb_build_object(
        'status', l.status, 'error', l.error, 'observed_at', l.observed_at,
        'checked_at', l.checked_at,
        'items', COALESCE((SELECT jsonb_agg(jsonb_build_object(
                               'ref', i.item_ref, 'search', i.search_text, 'item', i.item)
                               ORDER BY i.item_ref)
                           FROM library_list_items i
                           WHERE i.connection_ref = l.connection_ref AND i.kind = l.kind),
                          '[]'::jsonb)))
    FROM library_lists l WHERE l.connection_ref = c.connection_ref), '{}'::jsonb),
    created_by = 'unknown', changed_at = created_at, changed_by = 'unknown';

ALTER TABLE library_connections
    ALTER COLUMN created_by SET NOT NULL, ALTER COLUMN changed_at SET NOT NULL,
    ALTER COLUMN changed_by SET NOT NULL;

-- 4. Audit fields on pools and recipes ------------------------------------------------------------

ALTER TABLE photo_pools ADD COLUMN created_by TEXT, ADD COLUMN changed_at DOUBLE PRECISION,
    ADD COLUMN changed_by TEXT;
UPDATE photo_pools SET created_by = 'unknown', changed_at = created_at, changed_by = 'unknown';
ALTER TABLE photo_pools ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN changed_at SET NOT NULL, ALTER COLUMN changed_by SET NOT NULL;

ALTER TABLE media_recipes ADD COLUMN created_by TEXT, ADD COLUMN changed_at DOUBLE PRECISION,
    ADD COLUMN changed_by TEXT;
UPDATE media_recipes SET created_by = 'unknown', changed_at = created_at, changed_by = 'unknown';
ALTER TABLE media_recipes ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN changed_at SET NOT NULL, ALTER COLUMN changed_by SET NOT NULL;

-- 5. The derived pin's lookup and the tables that go ----------------------------------------------

CREATE INDEX plan_offers_valid ON plan_offers (valid_until);

DROP TABLE photo_key_content, asset_copies, media_source_name_versions, photo_drafts,
           media_source_names, library_list_items, library_lists, media_references,
           authored_candidates;
DROP TABLE asset_revisions;
