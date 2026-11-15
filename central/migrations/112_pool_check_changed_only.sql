-- A pool check writes what changed, plus one page per step for what it saw (decision 0033,
-- central/infra/photo_library_store.py `record_step`). Catalog changes only: renames, dropped
-- columns and columns added with a non-volatile default rewrite no table, so the 21,000-photo
-- pool's three tables are not rewritten here.
--
-- Plain statements only: no trigger, stored procedure or function. Forward-only (central/db.py).

-- 1. The audit fields. A row's first and last sighting become when it was created and last
-- changed: from now on `changed_at` moves only when a stored fact differs. Their one writer is the
-- media worker's pool check (central/actor.py `library-check`), so every existing row takes it;
-- the defaults then go, so a write that stamps no actor fails.
ALTER TABLE library_assets RENAME COLUMN first_seen TO created_at;
ALTER TABLE library_assets RENAME COLUMN last_seen TO changed_at;
ALTER TABLE library_assets ADD COLUMN created_by TEXT NOT NULL DEFAULT 'library-check',
  ADD COLUMN changed_by TEXT NOT NULL DEFAULT 'library-check';
ALTER TABLE library_assets ALTER COLUMN created_by DROP DEFAULT,
  ALTER COLUMN changed_by DROP DEFAULT;

ALTER TABLE asset_revisions RENAME COLUMN first_seen TO created_at;
ALTER TABLE asset_revisions RENAME COLUMN last_seen TO changed_at;
ALTER TABLE asset_revisions ADD COLUMN created_by TEXT NOT NULL DEFAULT 'library-check',
  ADD COLUMN changed_by TEXT NOT NULL DEFAULT 'library-check';
ALTER TABLE asset_revisions ALTER COLUMN created_by DROP DEFAULT,
  ALTER COLUMN changed_by DROP DEFAULT;

-- A pool row's times were never recorded: an existing row carries this migration's time
-- (`now()` is the transaction's start, evaluated once: the default is stored, not written).
ALTER TABLE photo_pool_rows DROP COLUMN seen_check,
  ADD COLUMN created_at DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM now()),
  ADD COLUMN changed_at DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM now()),
  ADD COLUMN created_by TEXT NOT NULL DEFAULT 'library-check',
  ADD COLUMN changed_by TEXT NOT NULL DEFAULT 'library-check';
ALTER TABLE photo_pool_rows ALTER COLUMN created_at DROP DEFAULT,
  ALTER COLUMN changed_at DROP DEFAULT, ALTER COLUMN created_by DROP DEFAULT,
  ALTER COLUMN changed_by DROP DEFAULT;

-- 2. What a check saw or marked: one row per step and kind holding the ids (16 bytes an id),
-- never a row per id. Append-only within its check; a check's pages are deleted when it finishes
-- and when the pool's next check starts. Every read takes the pages as one set of ids.
CREATE TABLE photo_check_pages (
  id BIGSERIAL PRIMARY KEY,
  pool_key TEXT NOT NULL REFERENCES photo_pools ON DELETE CASCADE,
  check_number BIGINT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('seen','excluded','stacked_copy')),
  upstream_ids UUID[] NOT NULL CHECK (cardinality(upstream_ids) > 0),
  recorded_at DOUBLE PRECISION NOT NULL,
  recorded_by TEXT NOT NULL);
CREATE INDEX photo_check_pages_check ON photo_check_pages (pool_key, check_number);

DROP TABLE photo_check_marks;

-- 3. A check running now saw its earlier steps without pages and its marks are gone: it ends, and
-- the pool is checked again from the start.
UPDATE photo_pools SET
  check_state = CASE WHEN last_complete_at IS NULL THEN 'never_checked' ELSE 'complete' END,
  check_requested = TRUE, progress = NULL
WHERE check_state = 'checking';
