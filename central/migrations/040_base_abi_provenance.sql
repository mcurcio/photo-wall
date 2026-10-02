-- A schema-2 release's base ABI is a build fact sealed inside its base tarball:
-- base-abi.json records the ABI extracted from the final squashfs and its exact
-- SHA-256. This is independent of the Player payload's requested ABI.
ALTER TABLE app_releases
    ADD COLUMN base_abi TEXT CHECK (base_abi ~ '^sha256:[0-9a-f]{64}$'),
    ADD COLUMN base_abi_squashfs_sha256 TEXT
        CHECK (base_abi_squashfs_sha256 ~ '^[0-9a-f]{64}$'),
    ADD COLUMN base_abi_source_manifest TEXT
        CHECK (base_abi_source_manifest = 'manifest.v2.json'),
    ADD CONSTRAINT app_releases_base_abi_complete CHECK (
        (base_abi IS NULL AND base_abi_squashfs_sha256 IS NULL
         AND base_abi_source_manifest IS NULL)
        OR
        (base_abi IS NOT NULL AND base_abi_squashfs_sha256 IS NOT NULL
         AND base_abi_source_manifest IS NOT NULL AND base_tarball_sha256 IS NOT NULL));
