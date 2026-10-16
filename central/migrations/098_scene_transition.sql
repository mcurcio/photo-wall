-- A Scene's fade is one Transition (central/runtime.py `Scene.transition`: a style and seconds),
-- never a Contribution's `fade_in_seconds` / `fade_out_seconds`, which Runtime now derives
-- (`_transition_at`). Every Scene in runtime_state's snapshot moves, child Scenes at any depth
-- included (a leftover fade field fails the model at boot); the seconds are the first body
-- Contribution's fade in plus fade out, which the console always wrote as equal halves (no
-- longer than each photo shows for, the model's bound):
--   * stored Scenes (`scenes`) become a cross-fade at those seconds (owner, 2026-10-15, asked
--     whether saved Scenes become cross-fade: "Yes"), at the next revision, so the Default
--     Scene restarts on it and a Program's next window plays it; the seconds are capped at each
--     photo's seconds less 5 (PREPARE_SECONDS, the bound a save checks: `Scene.check_transition`),
--     a cut when nothing is left, so a short-photo Scene neither holds a third photo per screen
--     while it dissolves nor is refused when saved unchanged;
--   * the copies Runs and queued Show now activations captured (`runs[].scene`,
--     `queue[].scene`) become fade out and in at those seconds, keeping their revision, which
--     derives exactly the fields they carried, so their offered layers stay as they are.
-- Migration helpers only, dropped below.
CREATE FUNCTION pg_temp.without_fades(contributions jsonb) RETURNS jsonb LANGUAGE sql AS $$
    SELECT COALESCE((SELECT jsonb_agg(c - 'fade_in_seconds' - 'fade_out_seconds' ORDER BY n)
                     FROM jsonb_array_elements(contributions) WITH ORDINALITY AS o(c, n)),
                    '[]'::jsonb)
$$;
CREATE FUNCTION pg_temp.with_transition(scene jsonb, style text, next_revision boolean)
    RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
    first jsonb := scene->'contributions'->0;
    photo_seconds numeric := COALESCE((scene->>'cycle_seconds')::numeric, 30);
    seconds numeric := GREATEST(0, LEAST(
        COALESCE((first->>'fade_in_seconds')::numeric, 0)
            + COALESCE((first->>'fade_out_seconds')::numeric, 0),
        CASE WHEN style = 'cross_fade' THEN photo_seconds - 5 ELSE photo_seconds END));
BEGIN
    RETURN scene || jsonb_build_object(
        'transition', jsonb_build_object('style', style, 'seconds', seconds),
        'contributions', pg_temp.without_fades(scene->'contributions'),
        'outro_contributions', pg_temp.without_fades(scene->'outro_contributions'),
        'children', COALESCE((
            SELECT jsonb_agg(child || jsonb_build_object(
                'scene', pg_temp.with_transition(child->'scene', style, next_revision)) ORDER BY n)
            FROM jsonb_array_elements(scene->'children') WITH ORDINALITY AS c(child, n)),
            '[]'::jsonb))
        || CASE WHEN next_revision
                THEN jsonb_build_object('revision', COALESCE((scene->>'revision')::int, 1) + 1)
                ELSE '{}'::jsonb END;
END
$$;
UPDATE runtime_state SET snapshot = jsonb_set(jsonb_set(jsonb_set(snapshot,
    '{scenes}', COALESCE((SELECT jsonb_object_agg(id,
                                 pg_temp.with_transition(scene, 'cross_fade', TRUE))
                          FROM jsonb_each(snapshot->'scenes') AS s(id, scene)), '{}'::jsonb)),
    '{runs}', COALESCE((SELECT jsonb_object_agg(id, jsonb_set(run, '{scene}',
                                pg_temp.with_transition(run->'scene', 'fade_out_and_in', FALSE)))
                        FROM jsonb_each(snapshot->'runs') AS r(id, run)), '{}'::jsonb)),
    '{queue}', COALESCE((SELECT jsonb_agg(jsonb_set(item, '{scene}',
                                pg_temp.with_transition(item->'scene', 'fade_out_and_in', FALSE))
                                ORDER BY n)
                         FROM jsonb_array_elements(snapshot->'queue') WITH ORDINALITY AS q(item, n)),
                        '[]'::jsonb)),
    revision = revision + 1;
DROP FUNCTION pg_temp.with_transition(jsonb, text, boolean);
DROP FUNCTION pg_temp.without_fades(jsonb);
