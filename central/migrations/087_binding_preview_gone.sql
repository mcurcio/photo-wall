-- Roadmap 1c (I1) took `preview` and `preview_expires` off a binding (contracts/models.py
-- `OutputBinding`, unknown fields forbidden), and 075 rewrote each stored binding's calibration
-- but left those two keys. A Player's last configuration (player_configurations) and each offered
-- plan's bindings (plan_offers.manifest) written before 1c then failed to read, and the scheduler
-- stopped on every tick (production, 2026-10-12). Every stored binding loses the two keys; nothing
-- else in a binding changes. plan_offers.groups maps an assignment to its group id and holds no
-- binding. Plain SQL: no trigger and no stored procedure.
UPDATE player_configurations SET configuration = jsonb_set(configuration, '{bindings}', (
    SELECT jsonb_agg(CASE WHEN jsonb_typeof(e.binding) = 'object'
                          THEN e.binding - 'preview' - 'preview_expires' ELSE e.binding END
                     ORDER BY e.ordinal)
    FROM jsonb_array_elements(configuration->'bindings') WITH ORDINALITY AS e(binding, ordinal)))
WHERE jsonb_typeof(configuration->'bindings') = 'array'
  AND jsonb_path_exists(configuration, '$.bindings[*] ? (exists(@.preview) || exists(@.preview_expires))');

UPDATE plan_offers SET manifest = jsonb_set(manifest, '{bindings}', (
    SELECT jsonb_agg(CASE WHEN jsonb_typeof(e.binding) = 'object'
                          THEN e.binding - 'preview' - 'preview_expires' ELSE e.binding END
                     ORDER BY e.ordinal)
    FROM jsonb_array_elements(manifest->'bindings') WITH ORDINALITY AS e(binding, ordinal)))
WHERE jsonb_typeof(manifest->'bindings') = 'array'
  AND jsonb_path_exists(manifest, '$.bindings[*] ? (exists(@.preview) || exists(@.preview_expires))');
