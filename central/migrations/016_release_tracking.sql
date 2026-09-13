-- 0010 slice 1: GitHub release tracking -- the discovered release list plus the
-- operator's promoted-tag pointer. Additive over 014's app_packages registry
-- (which still holds the served bytes and the "current" pointer central serves
-- from). GitHub sourcing feeds that SAME registry + current pointer; this
-- migration only adds tracking around promotion, it does not add a second
-- serving path. The only FK edge into 014 is app_releases.mirrored_sha256 ->
-- app_packages(sha256): a release row can only ever name bytes central has
-- already registered. sha256 remains a corruption check only (0009 home-LAN
-- ruling; no signing anywhere).
--
-- No down-migration machinery exists (central/db.py applies forward-only,
-- checksum-pinned migrations in filename order). Rollback is manual:
--   DROP TABLE app_release_policy; DROP TABLE app_releases;
-- plus deleting this file before the next boot. 014 and the served bytes are
-- unaffected.
CREATE TABLE app_releases (
    -- The semver-ish tag is the identity key (the .deb's own version does not
    -- advance per release -- see 0010 -- so the tag, not the .deb version, is
    -- the ordering key).
    tag TEXT PRIMARY KEY CHECK(tag ~ '^v[0-9]+\.[0-9]+\.[0-9]+'),
    -- Parsed ordering components; derived from the tag by AppReleases.
    major BIGINT NOT NULL,
    minor BIGINT NOT NULL,
    patch BIGINT NOT NULL,
    prerelease TEXT NOT NULL DEFAULT '',          -- semver prerelease segment ('' = full release)
    is_prerelease BOOLEAN NOT NULL,               -- GitHub's own prerelease flag
    -- Player .deb asset, learned from manifest.json (schema 1) without a
    -- download. NULL columns mean "no recognizable player asset" (undeployable).
    asset_sha256 TEXT CHECK(asset_sha256 ~ '^[0-9a-f]{64}$'),
    asset_size BIGINT CHECK(asset_size > 0),
    asset_url TEXT,                               -- browser_download_url, joined via manifest filename
    mirror_state TEXT NOT NULL DEFAULT 'discovered' CHECK(mirror_state IN (
        'discovered',    -- candidate with a player asset, no bytes fetched
        'undeployable',  -- no player asset / bad manifest schema; not promotable
        'mirroring',     -- download in flight
        'mirrored',      -- bytes downloaded, verified, registered in app_packages
        'mirror_failed', -- download unreachable / corrupt / oversize; retryable
        'divergent',     -- a mirrored tag re-cut upstream to different bytes (frozen)
        'withdrawn'      -- release deleted upstream but mirrored bytes retained
    )),
    mirror_error TEXT,                            -- last failure code (bounded, sanitized)
    -- Set once the bytes are registered; the only FK edge into 014. A row
    -- holding this must never be pruned (withdrawn sub-state instead).
    mirrored_sha256 TEXT REFERENCES app_packages(sha256),
    discovered_at DOUBLE PRECISION NOT NULL,      -- matches app_packages.registered_at type
    updated_at DOUBLE PRECISION NOT NULL
);
-- "Latest" ordering key: major/minor/patch DESC, full releases above
-- prereleases of the same X.Y.Z, then prerelease DESC. Prerelease segments are
-- compared lexically (documented limitation in 0010: numeric prerelease
-- identifiers such as rc.2 vs rc.10 may misorder -- display order only).
CREATE INDEX app_releases_semver
    ON app_releases (major DESC, minor DESC, patch DESC, (prerelease = '') DESC, prerelease DESC);
-- The operator's chosen tag (singleton, mirroring app_package_policy's shape).
-- promoted_tag is NOT NULL with an FK into app_releases(tag): a delete of a
-- promoted release would otherwise strand or block the pointer, which is why
-- the poller withdraws rather than prunes such rows. Both this pointer and the
-- current pointer are advanced only under SELECT ... FOR UPDATE on this row.
CREATE TABLE app_release_policy (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
    promoted_tag TEXT NOT NULL REFERENCES app_releases(tag)
);
