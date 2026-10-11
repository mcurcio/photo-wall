-- Roadmap 4 (run ledger .claude/runs/photo-4.md, slice C1): the photo library in Central's
-- database. Connections hold their Immich key, written by the operator API and read only by
-- central/infra/photo_library_store.py's worker side (C1); each connection's health, counts and
-- lists; pools keyed by their query's canonical form, their rows and check marks; drafts.
--
-- Photos are stored normalized (follow-ups 2026-10-11, slice A2; owner: "a normalized database,
-- with each upstream immich asset being a primary resource"): one `library_assets` row per Immich
-- asset holds the asset's own facts (kind, taken time, the original file's); its children, keyed
-- to it one-to-many, hold each version of it: `asset_revisions` (one per edit Immich made, its
-- upright size), `asset_copies` (each Immich copy Photo Wall downloaded and measured, a child of
-- the revision) and `media_jobs` (each copy Photo Wall prepared, `made_from` naming the Immich
-- copy it was made from). A pool row names a revision and copies none of its facts.
--
-- Plain tables and data only: no trigger, stored procedure or function (logic lives in Python).
-- Forward-only (central/db.py). Rollback is manual: drop the nine tables and the added columns.

CREATE TABLE library_connections (
  connection_ref TEXT PRIMARY KEY CHECK (connection_ref ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
  name TEXT NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 120),
  url TEXT NULL,                    -- policy.normalise_url's form; NULL: a cut-over row (C12)
  api_key TEXT NULL,                -- written by the API, read only by PgPhotoLibrary's worker side
  CHECK ((url IS NULL) = (api_key IS NULL)),
  immich_user JSONB NULL,           -- ImmichUser, bound by the first passing contact
  health JSONB NOT NULL DEFAULT '{"state":"not_checked"}',   -- LibraryHealth
  counts JSONB NULL,                -- LibraryCounts of the last passing contact
  last_contact BIGINT NOT NULL DEFAULT 0,      -- the last contact number handed out
  applied_contact BIGINT NOT NULL DEFAULT 0,   -- the last contact number applied (newest wins)
  contact_started_at DOUBLE PRECISION NULL,    -- database time of the last begin_contact
  full_size_copies BOOLEAN NULL,    -- Immich's last answer on full-size copies; NULL: not told
  created_at DOUBLE PRECISION NOT NULL,
  CHECK (applied_contact <= last_contact));

CREATE TABLE library_lists (
  connection_ref TEXT NOT NULL REFERENCES library_connections ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('albums','people','places','tags')),
  status TEXT NOT NULL CHECK (status IN ('ok','unavailable','permission','incompatible')),
  error TEXT NULL CHECK (error IS NULL OR error ~ '^[a-z_]{1,64}$'),
  observed_at DOUBLE PRECISION NULL, checked_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (connection_ref, kind),
  CHECK ((status = 'ok') = (error IS NULL)),
  CHECK (status <> 'ok' OR observed_at IS NOT NULL));

CREATE TABLE library_list_items (
  connection_ref TEXT NOT NULL, kind TEXT NOT NULL,
  item_ref TEXT NOT NULL,           -- album, person or tag id; a place: "country\u001fstate\u001fcity"
  search_text TEXT NOT NULL,        -- NFKC-casefolded name, path or place words
  item JSONB NOT NULL,              -- the served LibraryAlbum, LibraryPerson, Place or LibraryTag
  PRIMARY KEY (connection_ref, kind, item_ref),
  FOREIGN KEY (connection_ref, kind) REFERENCES library_lists ON DELETE CASCADE);

CREATE TABLE photo_pools (
  pool_key TEXT PRIMARY KEY CHECK (pool_key ~ '^[a-f0-9]{64}$'),
  connection_ref TEXT NOT NULL REFERENCES library_connections,
  query JSONB NOT NULL,             -- SourceQuery, canonical
  check_number BIGINT NOT NULL DEFAULT 0,
  check_state TEXT NOT NULL DEFAULT 'never_checked'
    CHECK (check_state IN ('never_checked','checking','complete','incomplete','failed')),
  check_started_at DOUBLE PRECISION NULL, check_finished_at DOUBLE PRECISION NULL,
  progress JSONB NULL, seen_so_far BIGINT NOT NULL DEFAULT 0,
  problem TEXT NULL CHECK (problem IN ('connection','chosen_item_gone')),
  incomplete_reason TEXT NULL CHECK (incomplete_reason IN ('over_1000_at_one_instant')),
  last_complete_at DOUBLE PRECISION NULL, immich_up_to BIGINT NULL,
  check_requested BOOLEAN NOT NULL DEFAULT TRUE,
  step_at DOUBLE PRECISION NULL,    -- database time the running check's latest step began
  created_at DOUBLE PRECISION NOT NULL,
  CHECK ((check_state = 'failed') = (problem IS NOT NULL)),
  CHECK ((check_state = 'incomplete') = (incomplete_reason IS NOT NULL)));

CREATE TABLE photo_pool_rows (
  pool_key TEXT NOT NULL REFERENCES photo_pools ON DELETE CASCADE,
  upstream_id UUID NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('in_pool','waiting','left_out')),
  reason TEXT NULL CHECK (reason IN ('unreadable_answer','over_item_limits')),
  asset_id TEXT NULL REFERENCES asset_revisions(asset_id),   -- the revision; its facts live there
  seen_check BIGINT NOT NULL,
  PRIMARY KEY (pool_key, upstream_id),
  CHECK ((outcome = 'in_pool') = (asset_id IS NOT NULL)),
  CHECK ((outcome = 'left_out') = (reason IS NOT NULL)));
CREATE INDEX photo_pool_rows_in_pool ON photo_pool_rows (pool_key, asset_id) WHERE outcome = 'in_pool';
CREATE INDEX photo_pool_rows_asset ON photo_pool_rows (asset_id) WHERE asset_id IS NOT NULL;

CREATE TABLE photo_check_marks (
  pool_key TEXT NOT NULL REFERENCES photo_pools ON DELETE CASCADE,
  check_number BIGINT NOT NULL, upstream_id UUID NOT NULL,
  mark TEXT NOT NULL CHECK (mark IN ('excluded','stacked_copy')),
  PRIMARY KEY (pool_key, check_number, upstream_id, mark));

CREATE TABLE photo_drafts (
  draft_ref UUID PRIMARY KEY,
  pool_key TEXT NOT NULL REFERENCES photo_pools,
  editing_name TEXT NULL REFERENCES media_source_names(name),
  created_at DOUBLE PRECISION NOT NULL);

-- One row per Immich asset (the parent): rewritten by every check that sees it.
CREATE TABLE library_assets (
  connection_ref TEXT NOT NULL,
  upstream_id UUID NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('image','video')),
  taken_at DOUBLE PRECISION NOT NULL,
  original_media_type TEXT NULL CHECK (length(original_media_type) <= 127
    AND original_media_type ~ '^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$'),
  original_width INTEGER NULL CHECK (original_width > 0),
  original_height INTEGER NULL CHECK (original_height > 0),
  original_orientation SMALLINT NULL CHECK (original_orientation BETWEEN 1 AND 8),
  original_bytes BIGINT NULL CHECK (original_bytes > 0),
  first_seen DOUBLE PRECISION NOT NULL,
  last_seen DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (connection_ref, upstream_id));

-- A revision is a child of its Immich asset (081 sets these NOT NULL, adds the parent's foreign
-- key and drops the pre-4 `metadata` once it has converted the rows it keeps).
ALTER TABLE asset_revisions ADD COLUMN connection_ref TEXT NULL, ADD COLUMN upstream_id UUID NULL,
  ADD COLUMN revision TEXT NULL, ADD COLUMN width INTEGER NULL CHECK (width > 0),
  ADD COLUMN height INTEGER NULL CHECK (height > 0),
  ADD COLUMN duration DOUBLE PRECISION NULL CHECK (duration > 0),
  ADD COLUMN updated_at TEXT NULL;   -- KnownRevision's source (NULL on pre-4 rows)

-- Each Immich copy of a revision Photo Wall downloaded, as measured (a cache of Immich).
CREATE TABLE asset_copies (
  asset_id TEXT NOT NULL REFERENCES asset_revisions(asset_id) ON DELETE CASCADE,
  copy TEXT NOT NULL CHECK (copy IN ('thumbnail','preview','fullsize')),
  media_type TEXT NOT NULL CHECK (length(media_type) <= 127),
  width INTEGER NOT NULL CHECK (width > 0),
  height INTEGER NOT NULL CHECK (height > 0),
  measured_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (asset_id, copy));

-- Which Immich copy a prepared copy was made from (MediaStore.publish).
ALTER TABLE media_jobs ADD COLUMN made_from TEXT NULL
  CHECK (made_from IN ('preview','fullsize','original'));

ALTER TABLE media_sources ALTER COLUMN spec DROP NOT NULL,
  ADD COLUMN query JSONB NULL, ADD COLUMN pool_key TEXT NULL REFERENCES photo_pools;

-- C12: one empty connection row per connection name in use (Sources' specs, the worker's last
-- reported ids, stored tag lists), name = ref. The owner pastes its URL and key once; until then
-- it is never contacted and its Sources keep their pools (none yet).
INSERT INTO library_connections (connection_ref, name, created_at)
SELECT used.ref, used.ref, EXTRACT(EPOCH FROM clock_timestamp())
FROM (SELECT spec->>'connection_ref' AS ref FROM media_sources
      WHERE spec IS NOT NULL AND jsonb_typeof(spec->'connection_ref') = 'string'
      UNION
      SELECT ids.ref FROM media_settings,
             jsonb_array_elements_text(COALESCE(connection_ids, '[]'::jsonb)) AS ids(ref)
      UNION
      SELECT connection_ref FROM library_tags) AS used
WHERE used.ref ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' AND length(used.ref) <= 120
ON CONFLICT DO NOTHING;

-- Each stored spec becomes a `query` (SourceQuery's JSON form):
-- favorites true -> favourites_only true (false/null -> false: finding F-6);
-- captured_from/until -> {"kind":"between","start":..,"end":..} (both null -> null);
-- media_types image -> photo + live_photo, video -> video, sorted; tags kept;
-- exclude {"albums":[],"people":[],"tags":[],"screenshots":true}; albums, people []; place null;
-- partners false. pool_key stays NULL: the store assigns it in Python on first read (one
-- canonical form: SourceQuery.pool_key).
UPDATE media_sources SET query = jsonb_build_object(
  'connection_ref', spec->'connection_ref',
  'albums', '[]'::jsonb,
  'people', '[]'::jsonb,
  'place', 'null'::jsonb,
  'taken', CASE
    WHEN COALESCE(spec->'captured_from', 'null'::jsonb) = 'null'::jsonb
     AND COALESCE(spec->'captured_until', 'null'::jsonb) = 'null'::jsonb THEN 'null'::jsonb
    ELSE jsonb_build_object('kind', 'between',
                            'start', COALESCE(spec->'captured_from', 'null'::jsonb),
                            'end', COALESCE(spec->'captured_until', 'null'::jsonb)) END,
  'favourites_only', to_jsonb(COALESCE(spec->'favorites', 'false'::jsonb) = 'true'::jsonb),
  'tags', COALESCE(spec->'tags', '[]'::jsonb),
  'media_types', (
    SELECT COALESCE(jsonb_agg(chosen.media_type ORDER BY chosen.media_type COLLATE "C"), '[]'::jsonb)
    FROM (SELECT DISTINCT unnest(CASE kinds.kind WHEN 'image' THEN ARRAY['photo', 'live_photo']
                                                 WHEN 'video' THEN ARRAY['video'] END) AS media_type
          FROM jsonb_array_elements_text(
                 COALESCE(spec->'media_types', '["image","video"]'::jsonb)) AS kinds(kind)
         ) AS chosen),
  'partners', 'false'::jsonb,
  'exclude', '{"albums":[],"people":[],"tags":[],"screenshots":true}'::jsonb)
WHERE spec IS NOT NULL AND jsonb_typeof(spec->'connection_ref') = 'string';
