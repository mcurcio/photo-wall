-- Central idempotent jobs, rule 1 (docs/central-idempotent-jobs.md §4, §8): an OS image is keyed
-- by the sha256 of its base tarball (`app_releases.base_tarball_sha256`), no longer by its tag.
-- A re-cut is then a new key, so a late fetch can only write the same bytes under the same name.
--
-- 1. Every tag-keyed `os-image` asset row goes (its references cascade).
-- 2. One row per DISTINCT tarball sha, and one reference per tag that ships it (owner = tag,
--    locator = the tarball facts, expected facts NULL), with exactly 021's validity filter.
-- 3. No produced facts are carried: they were recorded for the tag's build at the time, which a
--    re-cut may have replaced, so the first fetch of each key records its own. The files named
--    `base-<tag>.squashfs` are no longer read; they stay on disk until an operator deletes them
--    (bead 5 of the idempotent-jobs programme adds that step to docs/runbook.md). The
--    `os_image.fetch` outcome notes name the old keys, so they go too.
-- 4. Pending old-shape `photo_wall.os_image.fetch` deliveries are cancelled and running ones
--    failed: their `{"tag": ...}` kwargs no longer decode, and a stranded `doing` row would fail
--    to decode in the stalled-job rescue every minute, forever. The guard makes this a no-op on a
--    fresh install, where Central applies procrastinate's schema only AFTER the migrations (022).
-- 5. Every reference's locator names its key: `locator_sha256 = identity`, never NULL, for both
--    kinds (a `.deb` is keyed by its own sha, an OS image by its tarball's). Production tries any
--    reference of a key, and `ReleaseOrigin.download` checks the bytes against the locator's
--    sha only when it is set, so a reference to other bytes, or to unchecked ones, could install
--    them under the key. The schema now refuses such a reference.
--
-- 028 is safe to run twice: it starts by deleting every `os-image` row, and it replaces its
-- CHECK rather than adding it again.
--
-- Forward-only (central/db.py). ROLLBACK, in this order:
--   a. End the new-shape deliveries, which the old code cannot decode:
--        UPDATE procrastinate_jobs SET status = 'cancelled'
--        WHERE task_name = 'photo_wall.os_image.fetch' AND status = 'todo';
--        UPDATE procrastinate_jobs SET status = 'failed'
--        WHERE task_name = 'photo_wall.os_image.fetch' AND status = 'doing';
--   b. Drop the CHECK, which the old code's tag-keyed references violate:
--        ALTER TABLE asset_references DROP CONSTRAINT asset_references_locator_names_the_key;
--   c. Revert the code.
--   d. UPDATE app_release_poll SET etag = NULL;
--      so the next sync lists every release again and re-references each OS image under its
--      tag: one download per desired OS image.
-- ROLL FORWARD after a rollback: repeat (a) for the old-shape deliveries, then
--   DELETE FROM schema_migrations WHERE name = '028_os_image_content_key.sql';
-- and deploy this code: 028 runs again and re-keys every OS image from `app_releases`.
DELETE FROM assets WHERE kind = 'os-image';

INSERT INTO assets (kind, identity, created_at)
SELECT DISTINCT 'os-image'::text, base_tarball_sha256, EXTRACT(EPOCH FROM now())
FROM app_releases
WHERE base_tarball_url ~ '^https?://' AND length(base_tarball_url) <= 2048
  AND base_tarball_sha256 IS NOT NULL AND base_tarball_size IS NOT NULL
  AND length(tag) <= 128;
INSERT INTO asset_references (kind, identity, owner, locator_url, locator_sha256, locator_size,
                              expected_size, expected_sha256, added_at)
SELECT 'os-image', base_tarball_sha256, tag, base_tarball_url, base_tarball_sha256,
       base_tarball_size, NULL, NULL, EXTRACT(EPOCH FROM now())
FROM app_releases
WHERE base_tarball_url ~ '^https?://' AND length(base_tarball_url) <= 2048
  AND base_tarball_sha256 IS NOT NULL AND base_tarball_size IS NOT NULL
  AND length(tag) <= 128;

DELETE FROM job_outcomes WHERE job_name = 'os_image.fetch';

ALTER TABLE asset_references DROP CONSTRAINT IF EXISTS asset_references_locator_names_the_key;
ALTER TABLE asset_references ADD CONSTRAINT asset_references_locator_names_the_key
    CHECK (locator_sha256 IS NOT NULL AND locator_sha256 = identity);

DO $$
BEGIN
    IF to_regclass('procrastinate_jobs') IS NOT NULL THEN
        UPDATE procrastinate_jobs SET status = 'cancelled'
        WHERE task_name = 'photo_wall.os_image.fetch' AND status = 'todo';
        UPDATE procrastinate_jobs SET status = 'failed'
        WHERE task_name = 'photo_wall.os_image.fetch' AND status = 'doing';
    END IF;
END
$$;
