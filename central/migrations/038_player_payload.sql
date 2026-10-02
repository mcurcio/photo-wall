-- A schema-2 release may publish a data-only Player application archive alongside its
-- legacy .deb. These source facts are immutable by digest at the offer boundary; they are
-- never evidence that the archive ran or produced visible output. The legacy release path
-- reads its existing asset_* columns without reinterpretation.
ALTER TABLE app_releases
    ADD COLUMN payload_url TEXT,
    ADD COLUMN payload_sha256 TEXT CHECK(payload_sha256 ~ '^[0-9a-f]{64}$'),
    ADD COLUMN payload_size BIGINT CHECK(payload_size > 0 AND payload_size <= 268435456),
    ADD COLUMN payload_format TEXT CHECK(payload_format = 'pw-player-data-v1'),
    ADD COLUMN payload_base_abi TEXT CHECK(payload_base_abi ~ '^sha256:[0-9a-f]{64}$'),
    ADD COLUMN payload_source_manifest TEXT CHECK(payload_source_manifest = 'manifest.v2.json'),
    ADD CONSTRAINT app_releases_payload_complete CHECK (
        (payload_url IS NULL AND payload_sha256 IS NULL AND payload_size IS NULL
         AND payload_format IS NULL AND payload_base_abi IS NULL
         AND payload_source_manifest IS NULL)
        OR
        (payload_url IS NOT NULL AND payload_sha256 IS NOT NULL AND payload_size IS NOT NULL
         AND payload_format IS NOT NULL AND payload_base_abi IS NOT NULL
         AND payload_source_manifest IS NOT NULL));

CREATE INDEX app_releases_payload_sha256 ON app_releases(payload_sha256)
    WHERE payload_sha256 IS NOT NULL;

ALTER TABLE assets DROP CONSTRAINT assets_kind_check;
ALTER TABLE assets ADD CONSTRAINT assets_kind_check
    CHECK (kind IN ('os-image','player-deb','player-payload'));
