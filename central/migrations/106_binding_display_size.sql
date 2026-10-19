-- A binding carries its Display's size, not the Frame profile (frame box run,
-- .claude/runs/frame-box/page.md, bead fb-9; owner, 2026-10-19: "diagonal inches is a weird concept
-- when we already have width and height"): every stored binding, in each Player's last configuration
-- and in each offered plan, loses `profile` and gains `display_mm` (contracts/models.py
-- OutputBinding). The size is not known here: it is null, and Central's next configuration fills it
-- from the Display seen on the Output (its revision rises then, because the content differs). No
-- CHECK, function, trigger or stored procedure. No backwards compatibility: no binding keeps a
-- profile; take a pg_dump before deploying.
--
-- Old form: {..., "profile": {"width_px", "height_px", "diagonal_inches", "video"}, ...}.
-- New form: {..., "display_mm": null, ...}. A binding already holding `display_mm` keeps it.
CREATE TEMP TABLE sized_bindings ON COMMIT DROP AS
    SELECT stored.source, stored.player_id, stored.authority_epoch, stored.revision,
           jsonb_agg(CASE WHEN jsonb_typeof(e.binding) = 'object'
                          THEN (e.binding - 'profile') || jsonb_build_object('display_mm', e.binding->'display_mm')
                          ELSE e.binding END
                     ORDER BY e.ordinal) AS bindings
    FROM (SELECT 'configuration' AS source, player_id, authority_epoch, revision, configuration->'bindings' AS bindings
          FROM player_configurations
          UNION ALL
          SELECT 'offer', player_id, authority_epoch, revision, manifest->'bindings'
          FROM plan_offers) AS stored
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(stored.bindings) = 'array' THEN stored.bindings ELSE '[]'::jsonb END
    ) WITH ORDINALITY AS e(binding, ordinal)
    GROUP BY stored.source, stored.player_id, stored.authority_epoch, stored.revision;

UPDATE player_configurations c SET configuration = jsonb_set(c.configuration, '{bindings}', r.bindings)
FROM sized_bindings r
WHERE r.source = 'configuration' AND r.player_id = c.player_id AND r.bindings <> c.configuration->'bindings';

UPDATE plan_offers p SET manifest = jsonb_set(p.manifest, '{bindings}', r.bindings)
FROM sized_bindings r
WHERE r.source = 'offer' AND (r.player_id, r.authority_epoch, r.revision) = (p.player_id, p.authority_epoch, p.revision)
  AND r.bindings <> p.manifest->'bindings';
