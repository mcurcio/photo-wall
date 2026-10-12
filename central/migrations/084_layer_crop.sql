-- Roadmap 5a (run ledger .claude/runs/photo-5a.md, INTEGRATE I2): a media layer always names the
-- window it draws (contracts/models.py `Layer.crop`), and a black layer names none. Before 5a every
-- Player drew the centred window, so each stored media layer with no crop (the key absent, or null
-- as 5a's additive shape wrote it) in an offered plan (plan_offers) or an assignment lock
-- (assignment_locks) gets offset 0.5 (contracts/photo_crop.py `CENTRED`): what it showed.
UPDATE assignment_locks SET layer = jsonb_set(layer, '{crop}', '{"offset": 0.5}')
WHERE COALESCE(layer->>'presentation', 'media') = 'media'
  AND jsonb_typeof(layer->'crop') IS DISTINCT FROM 'object';

UPDATE plan_offers SET manifest = jsonb_set(manifest, '{layers}', (
    SELECT jsonb_agg(CASE
        WHEN COALESCE(item->>'presentation', 'media') = 'media'
             AND jsonb_typeof(item->'crop') IS DISTINCT FROM 'object'
        THEN jsonb_set(item, '{crop}', '{"offset": 0.5}') ELSE item END ORDER BY n)
    FROM jsonb_array_elements(manifest->'layers') WITH ORDINALITY AS l(item, n)))
WHERE jsonb_typeof(manifest->'layers') = 'array' AND jsonb_array_length(manifest->'layers') > 0;
