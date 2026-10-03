-- Console DDD §38 shape A, G10: each library connection's tag list, and library thumbnails as
-- a fourth kind on the one asset layer.
--
-- `library_tags`: one row per connection, written only by the media worker. `tags` is the last
-- list it read (already filtered, capped and stripped of control and bidi characters) and
-- `observed_at` when it was read; a failed re-list keeps both and records `status`/`error`.
-- `checked_at` is the last attempt. Every time is the database's clock (G11). The served shape
-- is tag id, path, name and parent id: no URL, key or owner.
--
-- A thumbnail is keyed by its ORIGINAL's identity and fetched through the media worker, so its
-- one reference carries a reserved locator with no digest (`central.assets.library`): 028's
-- "the locator names the key" holds for every other kind, and this kind admits exactly that one
-- reserved reference, so no thumbnail reference can name a real address.
--
-- Forward-only (central/db.py). Rollback is manual: DROP TABLE library_tags; delete the
-- 'library-thumbnail' rows; restore 054's assets kind CHECK and 028's locator CHECK.
CREATE TABLE library_tags (
    connection_ref TEXT PRIMARY KEY CHECK (connection_ref ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    tags JSONB CHECK (tags IS NULL OR (jsonb_typeof(tags) = 'array'
                                       AND jsonb_array_length(tags) <= 5000)),
    observed_at DOUBLE PRECISION,
    status TEXT NOT NULL CHECK (status IN ('ok', 'unavailable', 'permission', 'incompatible')),
    error TEXT CHECK (error IS NULL OR error ~ '^[a-z_]{1,64}$'),
    checked_at DOUBLE PRECISION NOT NULL,
    CHECK ((tags IS NULL) = (observed_at IS NULL)),
    CHECK ((status = 'ok') = (error IS NULL)),
    CHECK (status <> 'ok' OR tags IS NOT NULL)
);

ALTER TABLE assets DROP CONSTRAINT assets_kind_check;
ALTER TABLE assets ADD CONSTRAINT assets_kind_check
    CHECK (kind IN ('os-image', 'player-deb', 'player-payload', 'sealed-environment',
                    'library-thumbnail'));

ALTER TABLE asset_references DROP CONSTRAINT asset_references_locator_names_the_key;
ALTER TABLE asset_references ADD CONSTRAINT asset_references_locator_names_the_key
    CHECK ((kind <> 'library-thumbnail' AND locator_sha256 IS NOT NULL
            AND locator_sha256 = identity)
           OR (kind = 'library-thumbnail' AND owner = 'library-preview'
               AND locator_url = 'http://library.invalid/' AND locator_sha256 IS NULL
               AND locator_size IS NULL AND expected_size IS NULL AND expected_sha256 IS NULL));
