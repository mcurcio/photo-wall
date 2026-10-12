-- Frame-sized media on the one read path (follow-up to 5a, slice T). A prepared copy of a photo is
-- a media variant: an Asset record (`assets`, kind `media-variant`) named by its inputs,
-- `<asset_id>.<recipe_id>`, whose produced facts point at its bytes, `media/blobs/<sha256>` (the
-- layout the media store already wrote, so no file moves). `PrepareMedia` makes and re-makes it;
-- the Frame media route reads it through the AssetReader. `media_blobs` and `media_jobs` are no
-- longer written; slice S5 (migration 088) drops them.

-- 1. The kind, and the media facts a variant's record carries beside its size and digest
--    (`central.kernel.assets.MediaReady`: media type, size in pixels, duration, the copy it was
--    made from).
ALTER TABLE assets DROP CONSTRAINT assets_kind_check;
ALTER TABLE assets ADD CONSTRAINT assets_kind_check
    CHECK (kind IN ('os-image', 'sealed-environment', 'library-thumbnail', 'media-variant'));
ALTER TABLE assets ADD COLUMN produced_detail JSONB NULL;
-- A variant's reference says "wanted", never where from (the planned one) or that it cannot be
-- re-made (the legacy one): neither locator resolves (`central.assets.frame_media`).
ALTER TABLE asset_references DROP CONSTRAINT asset_references_locator_names_the_key;
ALTER TABLE asset_references ADD CONSTRAINT asset_references_locator_names_the_key
    CHECK ((kind NOT IN ('library-thumbnail', 'media-variant') AND locator_sha256 IS NOT NULL
            AND locator_sha256 = identity)
           OR (kind = 'library-thumbnail' AND owner = 'library-preview'
               AND locator_url = 'http://library.invalid/' AND locator_sha256 IS NULL
               AND locator_size IS NULL AND expected_size IS NULL AND expected_sha256 IS NULL)
           OR (kind = 'media-variant'
               AND ((owner = 'frame-media' AND locator_url = 'http://library.invalid/')
                    OR (owner = 'legacy-087' AND locator_url = 'http://legacy.invalid/'))
               AND locator_sha256 IS NULL AND locator_size IS NULL AND expected_size IS NULL
               AND expected_sha256 IS NULL));

-- 2. What each recipe id names (`media.prepare.recipe_id`, the one home of the hash): the recipe
--    values and the need they size for, so a variant can be re-made from its name alone.
CREATE TABLE media_recipes (
    recipe_id TEXT PRIMARY KEY CHECK (recipe_id ~ '^[0-9a-f]{64}$'),
    recipe JSONB NOT NULL,
    need JSONB NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);

-- 3. Each ready prepared copy becomes a variant record. Its recipe was the old global one (a hash
--    with the renderer build), which names no need, so it cannot be re-made: its one reference
--    is the legacy owner (`central.assets.frame_media.LEGACY_OWNER`), whose locator never
--    resolves. Once its file is gone, the photo is prepared again under a current recipe.
INSERT INTO assets (kind, identity, produced_size, produced_sha256, produced_detail, created_at)
SELECT 'media-variant', j.asset_id || '.' || j.recipe_id, b.size, b.digest,
       jsonb_build_object('media_type', b.variant->>'media_type',
                          'width', (b.variant->>'width')::int,
                          'height', (b.variant->>'height')::int,
                          'duration', b.variant->'duration',
                          'made_from', COALESCE(j.made_from, 'legacy')),
       b.created_at
FROM media_jobs j JOIN media_blobs b ON b.digest = j.variant_sha
WHERE b.state = 'ready' AND j.recipe_id ~ '^[0-9a-f]{64}$'
  AND j.asset_id ~ '^asset-[0-9a-f]{64}$'
ON CONFLICT (kind, identity) DO NOTHING;
INSERT INTO asset_references (kind, identity, owner, locator_url, locator_sha256, locator_size,
                              expected_size, expected_sha256, added_at)
SELECT kind, identity, 'legacy-087', 'http://legacy.invalid/', NULL, NULL, NULL, NULL, created_at
FROM assets WHERE kind = 'media-variant';

-- 4. Stored layers name their variant: every media layer of an offered plan (plan_offers) or an
--    assignment lock (assignment_locks) gains `asset_id` and `recipe_id`, found through the job
--    that made its bytes (`media_jobs.variant_sha`, the 084 precedent).
CREATE TEMP TABLE media_087_names ON COMMIT DROP AS
SELECT DISTINCT ON (j.variant_sha) j.variant_sha AS sha256,
       jsonb_build_object('asset_id', j.asset_id, 'recipe_id', j.recipe_id) AS name
FROM media_jobs j
WHERE j.variant_sha IS NOT NULL AND j.recipe_id ~ '^[0-9a-f]{64}$'
ORDER BY j.variant_sha, j.id;

UPDATE assignment_locks l SET layer = jsonb_set(l.layer, '{variant}', (l.layer->'variant') || n.name)
FROM media_087_names n
WHERE jsonb_typeof(l.layer->'variant') = 'object' AND l.layer->'variant'->>'sha256' = n.sha256;

UPDATE plan_offers SET manifest = jsonb_set(manifest, '{layers}', (
    SELECT jsonb_agg(CASE
        WHEN n.name IS NOT NULL THEN jsonb_set(item, '{variant}', (item->'variant') || n.name)
        ELSE item END ORDER BY ordinal)
    FROM jsonb_array_elements(manifest->'layers') WITH ORDINALITY AS l(item, ordinal)
    LEFT JOIN media_087_names n ON jsonb_typeof(item->'variant') = 'object'
                               AND n.sha256 = item->'variant'->>'sha256'))
WHERE jsonb_typeof(manifest->'layers') = 'array' AND jsonb_array_length(manifest->'layers') > 0;

-- 5. Pins name the variant, not its bytes: a re-make keeps them.
ALTER TABLE media_references ADD COLUMN variant_key TEXT NULL;
UPDATE media_references r SET variant_key = n.name->>'asset_id' || '.' || (n.name->>'recipe_id')
FROM media_087_names n WHERE n.sha256 = r.digest;
DELETE FROM media_references WHERE variant_key IS NULL;
ALTER TABLE media_references DROP CONSTRAINT media_references_pkey;
DROP INDEX media_references_digest;
ALTER TABLE media_references DROP COLUMN digest;
ALTER TABLE media_references ALTER COLUMN variant_key SET NOT NULL;
DELETE FROM media_references a USING media_references b
WHERE a.ctid < b.ctid AND a.owner = b.owner AND a.variant_key = b.variant_key;
ALTER TABLE media_references ADD PRIMARY KEY (owner, variant_key);
CREATE INDEX media_references_variant ON media_references (variant_key, expires_at);
DROP TABLE media_087_names;
