-- Decision 0034, S2b. Each one-row settings table becomes a `singletons` document, written only by
-- its repository and only when it changes (central/infra/singletons.py). Times are epoch seconds
-- on the database's clock; the writer of a row copied here is "unknown". Plain statements, no
-- trigger or function, forward-only.

-- The power policy: Night off and how long a hold lasts. Both rows always exist (089, 090). The
-- document's revision is Night off's, so an editor open on it keeps its revision.
INSERT INTO singletons (name, document, revision, created_at, created_by, changed_at, changed_by)
SELECT 'power_policy', jsonb_build_object('night_off', n.settings, 'hold_lasts', h.hold_lasts),
       n.revision, n.updated_at, 'unknown', greatest(n.updated_at, h.updated_at), 'unknown'
FROM night_off n CROSS JOIN hold_settings h;

-- The photo fit settings (086, 110): no row reads as the defaults, so none is copied as none.
INSERT INTO singletons (name, document, revision, created_at, created_by, changed_at, changed_by)
SELECT 'photo_fit', settings, 1, updated_at, 'unknown', updated_at, 'unknown'
FROM photo_fit_settings;

-- The media settings (005). `worker_seen` goes: whether the worker runs is its queue heartbeat.
INSERT INTO singletons (name, document, revision, created_at, created_by, changed_at, changed_by)
SELECT 'media', jsonb_build_object('recipe_id', recipe_id, 'max_bytes', max_bytes,
                                   'worker_error', worker_error),
       1, EXTRACT(EPOCH FROM clock_timestamp()), 'unknown', EXTRACT(EPOCH FROM clock_timestamp()),
       'unknown'
FROM media_settings;

-- The planning outcome (111): `since` was always its `changed_at`, so the row's changed_at is it.
INSERT INTO singletons (name, document, revision, created_at, created_by, changed_at, changed_by)
SELECT 'planning_outcome', jsonb_build_object('issues', issues, 'dropped', dropped),
       1, EXTRACT(EPOCH FROM created_at), created_by, EXTRACT(EPOCH FROM since), changed_by
FROM planning_outcome;

-- The release listing's ETag (017, 029).
INSERT INTO singletons (name, document, revision, created_at, created_by, changed_at, changed_by)
SELECT 'release_sync', jsonb_build_object('etag', etag, 'etag_stored_at', etag_stored_at),
       1, coalesce(etag_stored_at, EXTRACT(EPOCH FROM clock_timestamp())), 'unknown',
       coalesce(etag_stored_at, EXTRACT(EPOCH FROM clock_timestamp())), 'unknown'
FROM app_release_poll;

DROP TABLE night_off, hold_settings, photo_fit_settings, media_settings, planning_outcome,
           app_release_poll;

-- A hold's audit fields: a hold is written whole by the request that pressed it.
ALTER TABLE power_holds ADD COLUMN created_at DOUBLE PRECISION, ADD COLUMN created_by TEXT,
    ADD COLUMN changed_at DOUBLE PRECISION, ADD COLUMN changed_by TEXT;
UPDATE power_holds SET created_at = started_at, created_by = 'unknown', changed_at = started_at,
                       changed_by = 'unknown';
ALTER TABLE power_holds ALTER COLUMN created_at SET NOT NULL,
    ALTER COLUMN created_by SET NOT NULL, ALTER COLUMN changed_at SET NOT NULL,
    ALTER COLUMN changed_by SET NOT NULL;
