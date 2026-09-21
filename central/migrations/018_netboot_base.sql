-- 0012 bead 1 (TRACER): auto-mirroring netboot base images. Additive over 016's
-- app_releases catalog and 001/010's players registry. Three concerns:
--   1. base facts per release row (the download coordinates + the base build id);
--   2. base_cache -- one disposable, per-version-tag row for the squashfs bytes;
--   3. devices -- the canonical per-device row (pin, known-good, current-boot
--      record) auto-created at the unauthenticated netboot seam.
--
-- The cache is keyed by the release TAG, never by content sha (0012 decision 2b):
-- each version owns its own row and its own base-<tag>.squashfs file, so two
-- releases with identical bytes get two files and there is no shared mutable
-- squashfs_sha256 key for discovery and fetch to race on. squashfs_sha256 is an
-- integrity/`Digest` attribute only, filled at first cache.
--
-- No down-migration machinery exists (central/db.py applies forward-only,
-- checksum-pinned migrations in filename order). Rollback is manual:
--   DROP TABLE device_base_health; DROP TABLE devices; DROP TABLE base_cache;
--   ALTER TABLE app_releases DROP COLUMN base_revision, DROP COLUMN
--     base_tarball_sha256, DROP COLUMN base_tarball_size, DROP COLUMN
--     base_tarball_url;
-- plus deleting this file and removing base-*.squashfs from BASE_ROOT. 016 and
-- the served .deb bytes are unaffected. sha256 stays a corruption check only
-- (0009 home-LAN ruling; no signing anywhere).

-- 1. Base facts on the release row (per version; base OS + .deb are independently
--    versioned, so these live beside the .deb asset facts, never dedup'd).
ALTER TABLE app_releases ADD COLUMN base_revision TEXT;              -- manifest `revision` (git sha), informational
ALTER TABLE app_releases ADD COLUMN base_tarball_sha256 TEXT CHECK(base_tarball_sha256 ~ '^[0-9a-f]{64}$');
ALTER TABLE app_releases ADD COLUMN base_tarball_size BIGINT CHECK(base_tarball_size > 0);
ALTER TABLE app_releases ADD COLUMN base_tarball_url TEXT;          -- browser_download_url, joined via manifest filename

-- 2. Per-version cache state. Created at discovery; NEVER deleted (GC sets
--    'evicted'). The file base-<tag>.squashfs exists on disk only while 'cached'.
CREATE TABLE base_cache (
    tag TEXT PRIMARY KEY REFERENCES app_releases(tag),
    squashfs_sha256 TEXT CHECK(squashfs_sha256 ~ '^[0-9a-f]{64}$'),  -- integrity/Digest only, filled at first cache
    size BIGINT CHECK(size > 0),                                     -- extracted squashfs size, set at first cache
    state TEXT NOT NULL CHECK(state IN ('caching','cached','evicted','failed')),
    error TEXT,                                                      -- last failure code (bounded, sanitized)
    eviction_reason TEXT,                                            -- why GC last evicted this tag (observability)
    updated_at DOUBLE PRECISION NOT NULL
);

-- 3. The canonical per-device row. Auto-created (empty) at the unauthenticated
--    netboot seam; the home for the pin and the known-good tag. players
--    (post-enrollment) joins it by the shared device_id. Every FK into
--    app_releases(tag) is a version this device pins / ran healthy / was served.
CREATE TABLE devices (
    device_id TEXT PRIMARY KEY,                                     -- device-<64hex> from the serial (equipment_device_id)
    serial TEXT,                                                    -- raw serial as seen (_SAFE_SERIAL-bounded), operator display
    attached_tag TEXT REFERENCES app_releases(tag),                -- the PIN; NULL => latest-verified (or bootstrap)
    known_good_tag TEXT REFERENCES app_releases(tag),              -- last ran while base-healthy; rollback target + frontier input
    known_good_at DOUBLE PRECISION,                                -- when it last went base-healthy on that tag
    last_served_tag TEXT REFERENCES app_releases(tag),             -- bytes actually served this boot (written on a 200 only)
    boot_outcome TEXT CHECK(boot_outcome IN ('pending','healthy','failed')),
    failed_tag TEXT REFERENCES app_releases(tag),                  -- sticky rollback marker (bead 2 writes it; bead 1 never does)
    last_served_at DOUBLE PRECISION,                               -- when last_served_tag was served; poll-sweep clock
    first_seen DOUBLE PRECISION NOT NULL,
    last_seen DOUBLE PRECISION NOT NULL,
    retired_at DOUBLE PRECISION                                    -- retired devices drop out of the frontier + GC keep-set
);

-- Per-device base-health monotonicity, keyed per authority epoch. The direct
-- analogue of player_feedback (003): known-good only ever ADVANCES, so a
-- reordered check-in (a POST + a future websocket twin) can never regress it.
-- Separate from `devices` (whose columns are frozen per the 0012 r8 schema) and
-- from player_feedback (which is plan-offer-gated frame coordination, unreachable
-- by an enrolled-but-unbound device). See .claude/errata.md 0012 E3.
CREATE TABLE device_base_health (
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    authority_epoch BIGINT NOT NULL,
    sequence BIGINT NOT NULL,
    PRIMARY KEY(device_id, authority_epoch)
);
