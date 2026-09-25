-- Central idempotent jobs, rule 2 (docs/central-idempotent-jobs.md §4, §6, §8): a release row is
-- written only by an observation whose upstream version is not older than the stored one.
--
-- 1. `app_releases.upstream_changed_at` / `upstream_asset_id`: the manifest asset's GitHub
--    `(updated_at, id)` of the observation the row holds, compared as a row value, set only
--    from a manifest that was read, valid and complete. Both NULL (never so observed) or both
--    set; the CHECK refuses a half-set pair. NULL is applied over by any observation, so every
--    row existing today is stamped by the next sync.
-- 2. `app_release_poll.etag_stored_at`: when the ETag was stored. The sync trusts it for one hour
--    only, so a stale equal-version observation is repaired by the next full listing.
-- 3. The ETag is cleared, so the first sync after this migration lists every release in full and
--    stamps every row with its version.
--
-- Forward-only (central/db.py). ROLLBACK: revert the code; it neither reads nor writes the new
-- columns. Its unguarded writes keep the stamp they found, so the first full listing after a roll
-- forward (an equal version re-applies) repairs them.
ALTER TABLE app_releases
    ADD COLUMN upstream_changed_at DOUBLE PRECISION,
    ADD COLUMN upstream_asset_id BIGINT,
    ADD CONSTRAINT app_releases_upstream_version_pair
        CHECK ((upstream_changed_at IS NULL) = (upstream_asset_id IS NULL));

ALTER TABLE app_release_poll ADD COLUMN etag_stored_at DOUBLE PRECISION;

UPDATE app_release_poll SET etag = NULL;
