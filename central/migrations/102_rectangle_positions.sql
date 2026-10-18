-- Every stored Position becomes a rectangle (render redesign S5, frozen page
-- .claude/runs/render-planes/s5-page.md, "The conversion" and B1): Frames, the Position history,
-- both copies in Position trials, and the bindings of each Player's last configuration and of each
-- offered plan. One column, position_entries.turn (below); no CHECK, function, trigger or stored
-- procedure; the rule of what a rectangle may be is app code (contracts/frame_fit.py
-- `FrameRectangle`, `place`). No backwards compatibility: no Cover or Corners body is left behind:
-- take a pg_dump before deploying (docs/decisions/0029-render-planes.md, "Deploying").
--
-- Each stored copy converts by its own fit, the Frame shape it was made for (a Position history
-- entry's own profile_*_px, every other copy its Frame's profile) and the mode as built of the
-- Output its Frame is bound to now: outputs.observation width_px x height_px (as
-- central/infra/display_store.py `frame_outputs_in` reads it) when at least 1 x 1, else the
-- preferred mode of the display's EDID report (output_displays.report's modes): every Pi before
-- this release enrolled its Outputs at 0 x 0, and Weston, given no mode, runs the preferred one.
-- Unknown when unbound or neither is there. Missing keys read as before: Cover, the whole screen,
-- rotation 0.
-- - A rectangle is left as it is.
-- - The turn: the rotation r's nearest quarter turn q = (floor(r / 90 + 0.5) * 90) mod 360 (half
--   up) reversed, T = (360 - q) mod 360: a screen hung turned T shows upright what the old fit
--   turned q on the mode as built (Cover at 90 becomes turn 270, "top to the left").
-- - With a known size: today's cover rectangle of the corners at q and the Frame's aspect
--   (contracts/frame_fit.py `cover_rectangle`: height greatest(du / a, dv), width a x height,
--   centred on the corners' spans), on the screen as it hangs, in whole pixels at the Frame's
--   shape, shrunk to fit and moved in to lie on that screen, so `place` accepts every answer.
-- - With no known size: centred, full width.
-- - Exact when the size is known, r is the quarter turn q itself and every corner lies within 1
--   pixel per axis of the cover rectangle's footprint: the Frame stays ready. Otherwise its
--   Position is to check (position_generation NULL, readiness position-needed): Done on the
--   Position tab once. Frames, revisions and configuration revisions rise by one, so every Player
--   is sent its Frame's new Position and a draft made before this release is refused (stale base);
--   no Position history entry records the conversion.
-- - Each Position history entry records the turn it was made for (position_entries.turn,
--   central/fleet/position_history.py `MadeFor`): a converted entry its own T, a rectangle entry
--   the Frame's turn before this conversion (the only turn S2 knew), so Restore refuses an entry
--   made for the screen hung another way.

-- Trials in flight end: their copies convert below and a console begins the next at once.
UPDATE node_calibration_trials SET state = 'ended' WHERE state = 'active';

-- Each Frame's shape and the size of the Output it is bound to now (NULL when unknown).
CREATE TEMP TABLE frame_screens ON COMMIT DROP AS
    SELECT f.id AS frame_id,
           ARRAY[(f.profile->>'width_px')::int, (f.profile->>'height_px')::int] AS shape,
           CASE WHEN (o.observation->>'width_px')::int >= 1 AND (o.observation->>'height_px')::int >= 1
                THEN ARRAY[(o.observation->>'width_px')::int, (o.observation->>'height_px')::int]
                ELSE (SELECT ARRAY[(m.mode->>'width')::int, (m.mode->>'height')::int]
                      FROM output_displays d
                      CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(d.report->'modes') = 'array'
                                                                   THEN d.report->'modes' ELSE '[]'::jsonb END)
                          WITH ORDINALITY AS m(mode, ordinal)
                      WHERE d.player_id = b.player_id AND d.output_id = b.output_id
                        AND (m.mode->>'preferred')::boolean
                        AND (m.mode->>'width')::int >= 1 AND (m.mode->>'height')::int >= 1
                      ORDER BY m.ordinal LIMIT 1) END AS screen
    FROM frames f
    LEFT JOIN bindings b ON b.frame_id = f.id
    LEFT JOIN outputs o ON o.player_id = b.player_id AND o.output_id = b.output_id;

-- Every distinct stored copy that is not a rectangle yet: (fit, shape it was made for, screen).
CREATE TEMP TABLE stored_fits ON COMMIT DROP AS
    SELECT DISTINCT copies.fit, copies.shape, copies.screen FROM (
        SELECT COALESCE(f.calibration->'fit', '{}'::jsonb) AS fit, s.shape, s.screen
        FROM frames f JOIN frame_screens s ON s.frame_id = f.id
        UNION ALL
        SELECT e.fit, ARRAY[e.profile_width_px, e.profile_height_px], s.screen
        FROM position_entries e JOIN frame_screens s ON s.frame_id = e.frame_id
        UNION ALL
        SELECT COALESCE(t.calibration->'fit', '{}'::jsonb), s.shape, s.screen
        FROM node_calibration_trials t JOIN frame_screens s ON s.frame_id = t.frame_id
        UNION ALL
        SELECT COALESCE(t.saved_calibration->'fit', '{}'::jsonb), s.shape, s.screen
        FROM node_calibration_trials t JOIN frame_screens s ON s.frame_id = t.frame_id
        WHERE t.saved_calibration IS NOT NULL) AS copies
    WHERE COALESCE(copies.fit->>'mode', 'cover') <> 'rectangle';

-- The conversion, once (the S5 page's expression, step by step): each copy's turn, rectangle and
-- exactness. Lengths are pixels of the mode as built (W, H) until `hung`, then of the screen as
-- it hangs (Wh, Hh).
CREATE TEMP TABLE fit_answers ON COMMIT DROP AS
    SELECT s.fit, s.shape, s.screen, turned.turn,
           CASE WHEN s.screen IS NULL
                THEN '{"mode": "rectangle", "centre_x": 0.5, "centre_y": 0.5, "width": 1.0}'::jsonb
                ELSE jsonb_build_object('mode', 'rectangle', 'centre_x', moved.cx / hung.wh,
                                        'centre_y', moved.cy / hung.hh, 'width', whole.w / hung.wh) END AS answer,
           COALESCE(s.screen IS NOT NULL AND given.r = quarter.q
                    AND abs(px.x0 - (spans.cx - footprint.fw / 2)) <= 1 AND abs(px.y0 - (spans.cy - footprint.fh / 2)) <= 1
                    AND abs(px.x1 - (spans.cx + footprint.fw / 2)) <= 1 AND abs(px.y1 - (spans.cy - footprint.fh / 2)) <= 1
                    AND abs(px.x2 - (spans.cx + footprint.fw / 2)) <= 1 AND abs(px.y2 - (spans.cy + footprint.fh / 2)) <= 1
                    AND abs(px.x3 - (spans.cx - footprint.fw / 2)) <= 1 AND abs(px.y3 - (spans.cy + footprint.fh / 2)) <= 1,
                    false) AS exact
    FROM stored_fits s
    CROSS JOIN LATERAL (SELECT COALESCE((s.fit->>'rotation')::double precision, 0) AS r,
                               COALESCE(s.fit->'corners', '[[0, 0], [1, 0], [1, 1], [0, 1]]'::jsonb) AS c,
                               s.shape[1]::double precision / s.shape[2] AS a,
                               s.screen[1]::double precision AS sw, s.screen[2]::double precision AS sh) AS given
    CROSS JOIN LATERAL (SELECT (floor(given.r / 90 + 0.5)::int * 90) % 360 AS q) AS quarter
    CROSS JOIN LATERAL (SELECT (360 - quarter.q) % 360 AS turn) AS turned
    CROSS JOIN LATERAL (SELECT (given.c->0->>0)::double precision * given.sw AS x0,
                               (given.c->0->>1)::double precision * given.sh AS y0,
                               (given.c->1->>0)::double precision * given.sw AS x1,
                               (given.c->1->>1)::double precision * given.sh AS y1,
                               (given.c->2->>0)::double precision * given.sw AS x2,
                               (given.c->2->>1)::double precision * given.sh AS y2,
                               (given.c->3->>0)::double precision * given.sw AS x3,
                               (given.c->3->>1)::double precision * given.sh AS y3) AS px
    CROSS JOIN LATERAL (SELECT greatest(px.x0, px.x1, px.x2, px.x3) - least(px.x0, px.x1, px.x2, px.x3) AS sx,
                               greatest(px.y0, px.y1, px.y2, px.y3) - least(px.y0, px.y1, px.y2, px.y3) AS sy,
                               (least(px.x0, px.x1, px.x2, px.x3) + greatest(px.x0, px.x1, px.x2, px.x3)) / 2 AS cx,
                               (least(px.y0, px.y1, px.y2, px.y3) + greatest(px.y0, px.y1, px.y2, px.y3)) / 2 AS cy
                        ) AS spans
    CROSS JOIN LATERAL (SELECT CASE WHEN quarter.q IN (0, 180) THEN spans.sx ELSE spans.sy END AS du,
                               CASE WHEN quarter.q IN (0, 180) THEN spans.sy ELSE spans.sx END AS dv) AS along
    CROSS JOIN LATERAL (SELECT greatest(along.du / given.a, along.dv) AS hp) AS tall
    CROSS JOIN LATERAL (SELECT given.a * tall.hp AS wp) AS cover
    CROSS JOIN LATERAL (SELECT CASE WHEN quarter.q IN (0, 180) THEN cover.wp ELSE tall.hp END AS fw,
                               CASE WHEN quarter.q IN (0, 180) THEN tall.hp ELSE cover.wp END AS fh) AS footprint
    CROSS JOIN LATERAL (SELECT CASE WHEN turned.turn IN (0, 180) THEN given.sw ELSE given.sh END AS wh,
                               CASE WHEN turned.turn IN (0, 180) THEN given.sh ELSE given.sw END AS hh,
                               CASE turned.turn WHEN 0 THEN spans.cx WHEN 90 THEN given.sh - spans.cy
                                                WHEN 180 THEN given.sw - spans.cx ELSE spans.cy END AS cx,
                               CASE turned.turn WHEN 0 THEN spans.cy WHEN 90 THEN spans.cx
                                                WHEN 180 THEN given.sh - spans.cy ELSE given.sw - spans.cx END AS cy
                        ) AS hung
    CROSS JOIN LATERAL (SELECT greatest(1, least(hung.wh, floor(hung.hh * given.a + 1e-6), floor(cover.wp + 0.5))) AS w
                        ) AS whole
    CROSS JOIN LATERAL (SELECT floor(whole.w / given.a + 0.5) AS h) AS high
    CROSS JOIN LATERAL (SELECT least(greatest(hung.cx, whole.w / 2), hung.wh - whole.w / 2) AS cx,
                               least(greatest(hung.cy, high.h / 2), hung.hh - high.h / 2) AS cy) AS moved;

-- The turn each history entry was made for: a rectangle entry the Frame's turn now (before the
-- Frames' update below), a converted one its own (with its rectangle, below).
ALTER TABLE position_entries ADD COLUMN turn SMALLINT,
    DROP CONSTRAINT position_entries_kind_check,
    ADD CONSTRAINT position_entries_kind_check CHECK (kind IN ('kept-created', 'kept-saved', 'kept-profile-changed',
                                                               'kept-turn-changed', 'kept-unrecorded',
                                                               'sent-draft', 'sent-back'));
UPDATE position_entries e SET turn = f.turn FROM frames f WHERE f.id = e.frame_id;

-- Frames: the new Position under the next revision, the turn, the next configuration revision;
-- the Position is to check unless the conversion is exact. A rectangle Frame is not touched.
UPDATE frames f SET
    calibration = jsonb_build_object('revision', COALESCE((f.calibration->>'revision')::bigint, 1) + 1,
                                     'fit', x.answer),
    turn = x.turn,
    configuration_revision = f.configuration_revision + 1,
    position_generation = CASE WHEN x.exact THEN f.position_generation END
FROM frame_screens s, fit_answers x
WHERE s.frame_id = f.id AND x.fit = COALESCE(f.calibration->'fit', '{}'::jsonb)
  AND x.shape = s.shape AND x.screen IS NOT DISTINCT FROM s.screen;

-- The Position history and both trial copies take their answers; their revisions are kept.
UPDATE position_entries e SET fit = x.answer, turn = x.turn
FROM frame_screens s, fit_answers x
WHERE s.frame_id = e.frame_id AND x.fit = e.fit
  AND x.shape = ARRAY[e.profile_width_px, e.profile_height_px] AND x.screen IS NOT DISTINCT FROM s.screen;
ALTER TABLE position_entries ALTER COLUMN turn SET NOT NULL;

UPDATE node_calibration_trials t SET calibration = jsonb_set(t.calibration, '{fit}', x.answer)
FROM frame_screens s, fit_answers x
WHERE s.frame_id = t.frame_id AND x.fit = COALESCE(t.calibration->'fit', '{}'::jsonb)
  AND x.shape = s.shape AND x.screen IS NOT DISTINCT FROM s.screen;

UPDATE node_calibration_trials t SET saved_calibration = jsonb_set(t.saved_calibration, '{fit}', x.answer)
FROM frame_screens s, fit_answers x
WHERE t.saved_calibration IS NOT NULL
  AND s.frame_id = t.frame_id AND x.fit = COALESCE(t.saved_calibration->'fit', '{}'::jsonb)
  AND x.shape = s.shape AND x.screen IS NOT DISTINCT FROM s.screen;

-- A binding that is not a rectangle yet (contracts/models.py OutputBinding) carries its Frame's
-- new Position; a binding whose Frame is gone takes the centred full-width rectangle under its own
-- revision (099's form of the same rewrite): every row's bindings, once, then each table from it.
CREATE TEMP TABLE binding_rewrites ON COMMIT DROP AS
    SELECT stored.source, stored.player_id, stored.authority_epoch, stored.revision,
           jsonb_agg(CASE WHEN jsonb_typeof(e.binding) = 'object'
                               AND COALESCE(e.binding->'calibration'->'fit'->>'mode', 'cover') <> 'rectangle'
                          THEN e.binding || jsonb_build_object('calibration', COALESCE(
                              (SELECT f.calibration FROM frames f WHERE f.id = e.binding->>'frame_id'),
                              jsonb_build_object(
                                  'revision', COALESCE((e.binding->'calibration'->>'revision')::bigint, 1),
                                  'fit', '{"mode": "rectangle", "centre_x": 0.5, "centre_y": 0.5, "width": 1.0}'::jsonb)))
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
FROM binding_rewrites r
WHERE r.source = 'configuration' AND r.player_id = c.player_id AND r.bindings <> c.configuration->'bindings';

UPDATE plan_offers p SET manifest = jsonb_set(p.manifest, '{bindings}', r.bindings)
FROM binding_rewrites r
WHERE r.source = 'offer' AND (r.player_id, r.authority_epoch, r.revision) = (p.player_id, p.authority_epoch, p.revision)
  AND r.bindings <> p.manifest->'bindings';
