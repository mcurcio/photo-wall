-- Central MVP lane C: the one Asset table (design §5). An asset is keyed by (kind, identity):
-- (os-image, tag) and (player-deb, sha256). References say what SHOULD exist and where to get it;
-- produced facts (write-once) say what the file IS. The disk alone says whether it is present:
-- nothing here claims a file exists. The asset row goes with its last reference (the repository's
-- `retire`); deleting an asset row cascades to its references.
--
-- No down-migration machinery exists (central/db.py applies forward-only, checksum-pinned
-- migrations in filename order). Rollback is manual: DROP TABLE asset_references; DROP TABLE
-- assets; plus deleting this file. The legacy tables it reads are untouched.
CREATE TABLE assets (
    kind TEXT NOT NULL CHECK (kind IN ('os-image','player-deb')),
    identity TEXT NOT NULL CHECK (length(identity) BETWEEN 1 AND 256),
    produced_size BIGINT CHECK (produced_size > 0),
    produced_sha256 TEXT CHECK (produced_sha256 ~ '^[0-9a-f]{64}$'),
    last_served_at DOUBLE PRECISION,
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (kind, identity),
    CHECK ((produced_size IS NULL) = (produced_sha256 IS NULL))
);
CREATE TABLE asset_references (
    kind TEXT NOT NULL, identity TEXT NOT NULL,
    owner TEXT NOT NULL CHECK (length(owner) BETWEEN 1 AND 128),
    locator_url TEXT NOT NULL,
    locator_sha256 TEXT CHECK (locator_sha256 ~ '^[0-9a-f]{64}$'),
    locator_size BIGINT CHECK (locator_size > 0),
    expected_size BIGINT CHECK (expected_size > 0),
    expected_sha256 TEXT CHECK (expected_sha256 ~ '^[0-9a-f]{64}$'),
    added_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (kind, identity, owner),
    FOREIGN KEY (kind, identity) REFERENCES assets (kind, identity) ON DELETE CASCADE
);

-- Warm rollout: seed from the legacy catalog so files already on disk keep serving (the disk
-- layout keeps today's names). Only legacy rows the kernel types accept are seeded: an owner of at
-- most 128 chars and an http(s) locator URL of at most 2048 chars.

-- os-image references <- app_releases base tarball facts (owner = tag; the produced squashfs's
-- facts are not known up front, so expected_* stay NULL).
INSERT INTO assets (kind, identity, created_at)
SELECT 'os-image', tag, EXTRACT(EPOCH FROM now())
FROM app_releases
WHERE base_tarball_url ~ '^https?://' AND length(base_tarball_url) <= 2048
  AND base_tarball_sha256 IS NOT NULL AND base_tarball_size IS NOT NULL
  AND length(tag) <= 128;
INSERT INTO asset_references (kind, identity, owner, locator_url, locator_sha256, locator_size,
                              expected_size, expected_sha256, added_at)
SELECT 'os-image', tag, tag, base_tarball_url, base_tarball_sha256, base_tarball_size,
       NULL, NULL, EXTRACT(EPOCH FROM now())
FROM app_releases
WHERE base_tarball_url ~ '^https?://' AND length(base_tarball_url) <= 2048
  AND base_tarball_sha256 IS NOT NULL AND base_tarball_size IS NOT NULL
  AND length(tag) <= 128;

-- player-deb references <- app_releases .deb asset facts (identity = sha256, owner = tag,
-- expected = the asset facts). Tags that ship one sha share one asset row.
INSERT INTO assets (kind, identity, created_at)
SELECT DISTINCT 'player-deb'::text, asset_sha256, EXTRACT(EPOCH FROM now())
FROM app_releases
WHERE asset_url ~ '^https?://' AND length(asset_url) <= 2048
  AND asset_sha256 IS NOT NULL AND asset_size IS NOT NULL
  AND length(tag) <= 128;
INSERT INTO asset_references (kind, identity, owner, locator_url, locator_sha256, locator_size,
                              expected_size, expected_sha256, added_at)
SELECT 'player-deb', asset_sha256, tag, asset_url, asset_sha256, asset_size,
       asset_size, asset_sha256, EXTRACT(EPOCH FROM now())
FROM app_releases
WHERE asset_url ~ '^https?://' AND length(asset_url) <= 2048
  AND asset_sha256 IS NOT NULL AND asset_size IS NOT NULL
  AND length(tag) <= 128;

-- Produced facts, only for assets seeded above (i.e. that have a reference).
UPDATE assets a
SET produced_size = b.size, produced_sha256 = b.squashfs_sha256
FROM base_cache b
WHERE a.kind = 'os-image' AND a.identity = b.tag
  AND b.size IS NOT NULL AND b.squashfs_sha256 IS NOT NULL;
UPDATE assets a
SET produced_size = p.size, produced_sha256 = p.sha256
FROM app_packages p
WHERE a.kind = 'player-deb' AND a.identity = p.sha256;
