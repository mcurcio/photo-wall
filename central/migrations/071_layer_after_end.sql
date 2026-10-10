-- Central plans what a Frame keeps after a layer (`after_end`, contracts/models.py AfterEnd)
-- in place of the yes/no `retain_on_expiry`, in every stored Scene, Run and queued Scene
-- (runtime_state), offered plan (plan_offers) and assignment lock (assignment_locks): yes is
-- "keep_this_photo", no is "leave_as_is". The key occurs only on Contributions and Layers, and
-- a JSONB text never holds an unescaped quote inside a string, so the text replacement touches
-- nothing else.
UPDATE runtime_state SET snapshot = replace(replace(snapshot::text,
    '"retain_on_expiry": true', '"after_end": "keep_this_photo"'),
    '"retain_on_expiry": false', '"after_end": "leave_as_is"')::jsonb;
UPDATE plan_offers SET manifest = replace(replace(manifest::text,
    '"retain_on_expiry": true', '"after_end": "keep_this_photo"'),
    '"retain_on_expiry": false', '"after_end": "leave_as_is"')::jsonb;
UPDATE assignment_locks SET layer = replace(replace(layer::text,
    '"retain_on_expiry": true', '"after_end": "keep_this_photo"'),
    '"retain_on_expiry": false', '"after_end": "leave_as_is"')::jsonb;

-- A Scene's ending (its outro on a Frame, "Black" or "Fades out") now keeps nothing after it,
-- as the console writes it: each outro Contribution of a stored Scene, Run or queued Scene that
-- changed nothing. Nested child Scenes are left as stored (the console authors none). A
-- migration helper only, dropped below.
CREATE FUNCTION pg_temp.ending_keeps_nothing(scene jsonb) RETURNS jsonb LANGUAGE sql AS $$
    SELECT CASE WHEN jsonb_typeof(scene->'outro_contributions') = 'array'
        THEN jsonb_set(scene, '{outro_contributions}', COALESCE((
            SELECT jsonb_agg(CASE
                WHEN c->>'after_end' = 'leave_as_is' AND c->>'kind' IN ('media', 'black')
                THEN jsonb_set(c, '{after_end}', '"keep_nothing"') ELSE c END ORDER BY n)
            FROM jsonb_array_elements(scene->'outro_contributions') WITH ORDINALITY AS o(c, n)
        ), '[]'::jsonb))
        ELSE scene END
$$;
UPDATE runtime_state SET snapshot = jsonb_set(jsonb_set(jsonb_set(snapshot,
    '{scenes}', COALESCE((SELECT jsonb_object_agg(id, pg_temp.ending_keeps_nothing(scene))
                          FROM jsonb_each(snapshot->'scenes') AS s(id, scene)), '{}'::jsonb)),
    '{runs}', COALESCE((SELECT jsonb_object_agg(id, jsonb_set(run, '{scene}',
                                pg_temp.ending_keeps_nothing(run->'scene')))
                        FROM jsonb_each(snapshot->'runs') AS r(id, run)), '{}'::jsonb)),
    '{queue}', COALESCE((SELECT jsonb_agg(jsonb_set(item, '{scene}',
                                pg_temp.ending_keeps_nothing(item->'scene')) ORDER BY n)
                         FROM jsonb_array_elements(snapshot->'queue') WITH ORDINALITY AS q(item, n)),
                        '[]'::jsonb));
DROP FUNCTION pg_temp.ending_keeps_nothing(jsonb);
