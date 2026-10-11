-- Roadmap 4 (run ledger .claude/runs/photo-4.md, slice C1): the photo library in Central's
-- database. Connections hold their Immich key, written by the operator API and read only by
-- central/infra/photo_library_store.py's worker side (C1); each connection's health, counts and
-- lists; pools keyed by their query's canonical form, their rows and check marks; drafts.
--
-- Plain tables and data only: no trigger, stored procedure or function (logic lives in Python).
-- Forward-only (central/db.py). Rollback is manual: drop the seven tables and the added columns.

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
  asset_id TEXT NULL REFERENCES asset_revisions(asset_id),
  revision TEXT NULL, kind TEXT NULL CHECK (kind IN ('image','video')),
  taken_at DOUBLE PRECISION NULL, width INTEGER NULL, height INTEGER NULL,
  seen_check BIGINT NOT NULL,
  PRIMARY KEY (pool_key, upstream_id),
  CHECK ((outcome = 'in_pool') = (asset_id IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL
                                  AND taken_at IS NOT NULL AND kind IS NOT NULL AND revision IS NOT NULL)),
  CHECK ((outcome = 'left_out') = (reason IS NOT NULL)));
CREATE INDEX photo_pool_rows_page ON photo_pool_rows (pool_key, taken_at DESC, asset_id DESC)
  WHERE outcome = 'in_pool';
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

ALTER TABLE asset_revisions ADD COLUMN connection_ref TEXT NULL, ADD COLUMN upstream_id UUID NULL,
  ADD COLUMN updated_at TEXT NULL;   -- KnownRevision's source (NULL on pre-4 rows)
CREATE INDEX asset_revisions_upstream ON asset_revisions (connection_ref, upstream_id)
  WHERE upstream_id IS NOT NULL;

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
