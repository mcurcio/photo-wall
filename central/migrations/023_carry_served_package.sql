-- Central MVP fix (PR #22 P0): carry the served Player `.deb` across the upgrade.
--
-- Main served GET /v1/app/manifest from `app_package_policy.current_sha256` (014), a pointer that
-- stayed on the previous package until the promoted tag's bytes were mirrored. The MVP catalog
-- reads `app_release_policy` only, so without this carry an upgraded install loses its served
-- `.deb`. The new column `last_good_tag` is that second pointer on the new model: the manifest
-- names the promoted `.deb` when it is on disk, else the last-good one when it is.
--
-- Carry rule, when a current package exists:
--   * a release whose `.deb` sha equals current_sha256 (with a complete locator) is the served
--     tag; the already-promoted tag wins a tie, then the newest by semver;
--   * it becomes `last_good_tag`; it also becomes `promoted_tag` when nothing is promoted. A
--     promoted tag that differs (a promotion whose mirror had not finished) is kept: main would
--     have converged to it, and last_good_tag keeps serving the old bytes meanwhile;
--   * NO release matches (a manually uploaded `.deb`, which the MVP can no longer serve: every
--     `.deb` comes from GitHub): nothing is written. The migration raises a WARNING (server log)
--     and, while nothing is promoted and auto-promote is suppressed (bound players + cached
--     `.deb`s), every release sync logs why the manifest answers 503 `app_unconfigured`. The
--     operator's release list shows no promoted release; promoting one resolves it.
--
-- Forward-only (central/db.py). Rollback is manual: ALTER TABLE app_release_policy DROP COLUMN
-- last_good_tag; plus deleting this file. The legacy tables are only read.
ALTER TABLE app_release_policy ADD COLUMN last_good_tag TEXT REFERENCES app_releases(tag);

DO $$
DECLARE
    current_sha TEXT;
    promoted TEXT;
    served TEXT;
BEGIN
    SELECT current_sha256 INTO current_sha FROM app_package_policy WHERE singleton;
    IF current_sha IS NULL THEN
        RETURN;  -- a fresh install, or nothing was ever served
    END IF;
    SELECT promoted_tag INTO promoted FROM app_release_policy WHERE singleton;
    SELECT tag INTO served FROM app_releases
    WHERE asset_sha256 = current_sha AND asset_url IS NOT NULL AND asset_size IS NOT NULL
    ORDER BY (tag = promoted) DESC NULLS LAST, major DESC, minor DESC, patch DESC,
             (prerelease = '') DESC, prerelease DESC
    LIMIT 1;
    IF served IS NULL THEN
        RAISE WARNING 'photo-wall 023: the served .deb % matches no GitHub release; it is no '
                      'longer served. Promote a release (POST /v1/operator/app/releases/{tag}/'
                      'promote).', current_sha;
        RETURN;
    END IF;
    INSERT INTO app_release_policy(singleton, promoted_tag, last_good_tag)
    VALUES (TRUE, served, served)
    ON CONFLICT (singleton) DO UPDATE SET last_good_tag = EXCLUDED.last_good_tag;
END
$$;
