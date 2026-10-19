-- Every stored Position becomes a box (frame box run, .claude/runs/frame-box/page.md, bead fb-3;
-- owner, 2026-10-19: "I need to be able to set width and height independently, along with the
-- position of that box on the display"): Frames, the Position history, both copies in Position
-- trials, and the bindings of each Player's last configuration and of each offered plan. No CHECK,
-- function, trigger or stored procedure; the rule of what a box may be is app code
-- (contracts/frame_fit.py `FrameBox`, `place_box`). No backwards compatibility: no rectangle is
-- left behind; take a pg_dump before deploying.
--
-- Old form (v0.41.0's RectangleFit): {"mode": "rectangle", "centre_x", "centre_y", "width"}, a
-- missing key read as 0.5, 0.5, 1.0, its height following from the Frame profile's aspect. New form
-- (contracts/models.py PositionBox): {"mode": "box", "left", "top", "width", "height"}, fractions of
-- the screen as it hangs. A copy already a box is left as it is.
--
-- Each copy converts by the screen's mode as built (Wb, Hb), the turn it was made for and the
-- aspect a it was made at:
-- - (Wb, Hb): central/displays/model.py `screen_px`, reading what Central reads at runtime
--   (central/infra/display_store.py SCREEN_COLUMNS): outputs.observation of the Frame's bound
--   Output when at least 1 x 1, else the first preferred mode of the Display seen on that Output
--   (displays.modes through output_displays.display_id); unknown when unbound or neither.
-- - The turn: a history entry's own (position_entries.turn), every other copy its Frame's.
-- - a: a history entry's own profile_width_px / profile_height_px, every other copy its Frame's
--   profile width_px / height_px.
-- - (W, H) = (Wb, Hb) hung by the turn (swapped at 90 and 270). Pixels as v0.41.0's `place`, each
--   half up: pw = max(1, floor(w W + 0.5)), ph = max(1, floor(pw / a + 0.5)),
--   px = floor(cx W - pw / 2 + 0.5), py = floor(cy H - ph / 2 + 0.5).
-- - On its screen (0 <= px, px + pw <= W, and likewise down): the box (px / W, py / H, pw / W,
--   ph / H), which `place_box` places on exactly those pixels: exact, the Frame stays ready.
-- - Not on it: clipped, pw' = min(pw, W), px' = min(max(px, 0), W - pw'), and likewise down; the
--   box of those; not exact.
-- - Unknown screen (or no aspect): the whole screen; not exact.
-- A Frame not converted exactly is to check (position_generation NULL, readiness position-needed:
-- Done on the Position tab once). Frames' revisions and configuration revisions rise by one, so every
-- Player is sent its Frame's box and a draft made before this release is refused (stale base); no
-- Position history entry records the conversion.

-- Trials in flight whose copies convert end: a console begins the next at once.
UPDATE node_calibration_trials SET state = 'ended'
WHERE state = 'active'
  AND (COALESCE(calibration->'fit'->>'mode', '') <> 'box'
       OR (saved_calibration IS NOT NULL AND COALESCE(saved_calibration->'fit'->>'mode', '') <> 'box'));

-- The temporary tables take names of their own: central.db applies every pending migration in one
-- transaction, where 102's (ON COMMIT DROP) still exist.
--
-- Each Frame's shape, turn and the mode as built of the Output it is bound to now (NULL: unknown).
CREATE TEMP TABLE box_screens ON COMMIT DROP AS
    SELECT f.id AS frame_id, f.turn,
           ARRAY[(f.profile->>'width_px')::int, (f.profile->>'height_px')::int] AS shape,
           CASE WHEN (o.observation->>'width_px')::int >= 1 AND (o.observation->>'height_px')::int >= 1
                THEN ARRAY[(o.observation->>'width_px')::int, (o.observation->>'height_px')::int]
                ELSE (SELECT ARRAY[(m.mode->>'width')::int, (m.mode->>'height')::int]
                      FROM output_displays seen
                      JOIN displays d ON d.id = seen.display_id
                      CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(d.modes) = 'array'
                                                                   THEN d.modes ELSE '[]'::jsonb END)
                          WITH ORDINALITY AS m(mode, ordinal)
                      WHERE seen.player_id = b.player_id AND seen.output_id = b.output_id
                        AND (m.mode->>'preferred')::boolean
                      ORDER BY m.ordinal LIMIT 1) END AS screen
    FROM frames f
    LEFT JOIN bindings b ON b.frame_id = f.id
    LEFT JOIN outputs o ON o.player_id = b.player_id AND o.output_id = b.output_id;

-- Every distinct stored copy that is not a box yet: (fit, shape it was made at, turn, screen).
CREATE TEMP TABLE box_copies ON COMMIT DROP AS
    SELECT DISTINCT copies.fit, copies.shape, copies.turn, copies.screen FROM (
        SELECT COALESCE(f.calibration->'fit', '{}'::jsonb) AS fit, s.shape, s.turn, s.screen
        FROM frames f JOIN box_screens s ON s.frame_id = f.id
        UNION ALL
        SELECT e.fit, ARRAY[e.profile_width_px, e.profile_height_px], e.turn::int, s.screen
        FROM position_entries e JOIN box_screens s ON s.frame_id = e.frame_id
        UNION ALL
        SELECT COALESCE(t.calibration->'fit', '{}'::jsonb), s.shape, s.turn, s.screen
        FROM node_calibration_trials t JOIN box_screens s ON s.frame_id = t.frame_id
        UNION ALL
        SELECT COALESCE(t.saved_calibration->'fit', '{}'::jsonb), s.shape, s.turn, s.screen
        FROM node_calibration_trials t JOIN box_screens s ON s.frame_id = t.frame_id
        WHERE t.saved_calibration IS NOT NULL) AS copies
    WHERE COALESCE(copies.fit->>'mode', '') <> 'box';

-- The conversion, once per distinct copy: its box and whether it is exact.
CREATE TEMP TABLE box_answers ON COMMIT DROP AS
    SELECT s.fit, s.shape, s.turn, s.screen,
           CASE WHEN known.ok
                THEN jsonb_build_object('mode', 'box', 'left', clipped.x / hung.w, 'top', clipped.y / hung.h,
                                        'width', clipped.pw / hung.w, 'height', clipped.ph / hung.h)
                ELSE '{"mode": "box", "left": 0.0, "top": 0.0, "width": 1.0, "height": 1.0}'::jsonb END AS answer,
           COALESCE(known.ok AND 0 <= px.x AND px.x + px.pw <= hung.w AND 0 <= px.y AND px.y + px.ph <= hung.h,
                    false) AS exact
    FROM box_copies s
    CROSS JOIN LATERAL (SELECT COALESCE(s.screen IS NOT NULL AND s.shape[1] >= 1 AND s.shape[2] >= 1, false) AS ok
                        ) AS known
    CROSS JOIN LATERAL (SELECT COALESCE((s.fit->>'centre_x')::double precision, 0.5) AS cx,
                               COALESCE((s.fit->>'centre_y')::double precision, 0.5) AS cy,
                               COALESCE((s.fit->>'width')::double precision, 1.0) AS width,
                               s.shape[1]::double precision / s.shape[2] AS a) AS given
    CROSS JOIN LATERAL (SELECT CASE WHEN s.turn IN (90, 270) THEN s.screen[2] ELSE s.screen[1] END::double precision AS w,
                               CASE WHEN s.turn IN (90, 270) THEN s.screen[1] ELSE s.screen[2] END::double precision AS h
                        ) AS hung
    CROSS JOIN LATERAL (SELECT greatest(1, floor(given.width * hung.w + 0.5)) AS pw) AS wide
    CROSS JOIN LATERAL (SELECT greatest(1, floor(wide.pw / given.a + 0.5)) AS ph) AS high
    CROSS JOIN LATERAL (SELECT floor(given.cx * hung.w - wide.pw / 2 + 0.5) AS x,
                               floor(given.cy * hung.h - high.ph / 2 + 0.5) AS y,
                               wide.pw, high.ph) AS px
    CROSS JOIN LATERAL (SELECT least(px.pw, hung.w) AS pw, least(px.ph, hung.h) AS ph) AS sized
    CROSS JOIN LATERAL (SELECT least(greatest(px.x, 0), hung.w - sized.pw) AS x,
                               least(greatest(px.y, 0), hung.h - sized.ph) AS y,
                               sized.pw, sized.ph) AS clipped;

-- Frames: the box under the next revision and the next configuration revision; the Position is to
-- check unless the conversion is exact. A box Frame is not touched.
UPDATE frames f SET
    calibration = jsonb_build_object('revision', COALESCE((f.calibration->>'revision')::bigint, 1) + 1,
                                     'fit', x.answer),
    configuration_revision = f.configuration_revision + 1,
    position_generation = CASE WHEN x.exact THEN f.position_generation END
FROM box_screens s, box_answers x
WHERE s.frame_id = f.id AND x.fit = COALESCE(f.calibration->'fit', '{}'::jsonb)
  AND x.shape = s.shape AND x.turn = s.turn AND x.screen IS NOT DISTINCT FROM s.screen;

-- The Position history and both trial copies take their boxes; their revisions are kept.
UPDATE position_entries e SET fit = x.answer
FROM box_screens s, box_answers x
WHERE s.frame_id = e.frame_id AND x.fit = e.fit
  AND x.shape = ARRAY[e.profile_width_px, e.profile_height_px] AND x.turn = e.turn
  AND x.screen IS NOT DISTINCT FROM s.screen;

UPDATE node_calibration_trials t SET calibration = jsonb_set(t.calibration, '{fit}', x.answer)
FROM box_screens s, box_answers x
WHERE s.frame_id = t.frame_id AND x.fit = COALESCE(t.calibration->'fit', '{}'::jsonb)
  AND x.shape = s.shape AND x.turn = s.turn AND x.screen IS NOT DISTINCT FROM s.screen;

UPDATE node_calibration_trials t SET saved_calibration = jsonb_set(t.saved_calibration, '{fit}', x.answer)
FROM box_screens s, box_answers x
WHERE t.saved_calibration IS NOT NULL
  AND s.frame_id = t.frame_id AND x.fit = COALESCE(t.saved_calibration->'fit', '{}'::jsonb)
  AND x.shape = s.shape AND x.turn = s.turn AND x.screen IS NOT DISTINCT FROM s.screen;

-- A binding that is not a box yet (contracts/models.py OutputBinding) carries its Frame's box (the
-- Frames' update above); a binding whose Frame is gone takes the whole screen under its own
-- revision (102's form of the same rewrite): every row's bindings, once, then each table from it.
CREATE TEMP TABLE box_bindings ON COMMIT DROP AS
    SELECT stored.source, stored.player_id, stored.authority_epoch, stored.revision,
           jsonb_agg(CASE WHEN jsonb_typeof(e.binding) = 'object'
                               AND COALESCE(e.binding->'calibration'->'fit'->>'mode', '') <> 'box'
                          THEN e.binding || jsonb_build_object('calibration', COALESCE(
                              (SELECT f.calibration FROM frames f WHERE f.id = e.binding->>'frame_id'),
                              jsonb_build_object(
                                  'revision', COALESCE((e.binding->'calibration'->>'revision')::bigint, 1),
                                  'fit', '{"mode": "box", "left": 0.0, "top": 0.0, "width": 1.0, "height": 1.0}'::jsonb)))
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
FROM box_bindings r
WHERE r.source = 'configuration' AND r.player_id = c.player_id AND r.bindings <> c.configuration->'bindings';

UPDATE plan_offers p SET manifest = jsonb_set(p.manifest, '{bindings}', r.bindings)
FROM box_bindings r
WHERE r.source = 'offer' AND (r.player_id, r.authority_epoch, r.revision) = (p.player_id, p.authority_epoch, p.revision)
  AND r.bindings <> p.manifest->'bindings';
