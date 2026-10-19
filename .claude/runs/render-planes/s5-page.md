# Render planes, S5 (Positions are rectangles only): frozen page

Branch `feat/render-planes` at 9e23d30b (milestone 2 green). Plan: `/Volumes/Dock/tmp/node-render-arch/plan.md` v3 §4 S5 and §6. S1's, S2's and S3's pages (`s1-page.md`, `s2-page.md`, `s3-page.md`) are frozen; this page changes only what it names. Process: `~/.claude/skills/implementation-workflow/SKILL.md`. S4 (`media/prepare.py`, HEVC) is built at the same time on the same branch: no S5 bead owns anything under `media/`, nor `central/media_repository.py`, `tests/test_prepare.py`, `tests/test_media_recipes.py`, `tests/test_media_worker.py`, `tests/test_media_variant_remake.py`, `tests/test_wall_copy_sizing.py` (S4's page), `player/decode.py`, `scripts/demo_wall.py`, `scripts/immich_actions.py` or `debian/`; expect rebases.

The interface commit that carries this page holds every NEW S5 symbol as code with no bodies (JS and TS stubs throw `Error("render-planes S5 bead Bn")`): `central/console/src/placement.js` (`hanging`, `place`, `fillScreen`), `central/console/src/patterns/rectangle-editor.tsx` (`RectangleEditor`, its props), `central/console/src/domain/frame-turn.tsx` (`FrameTurn`, its props), `central/console/src/framesApi.js` `turnFrame`, and two complete pieces of vocabulary: `central/console/src/domain/turn-words.ts` (`Turn`, `TURNS`, `TURN_WORDS`) and `FrameTurnChange` in the console contract (`central/console_contract.py` `CONSOLE_BODIES`, `src/generated/contract.ts` regenerated). **Changes to existing signatures are stated here and made by the bead that owns them, with every caller and test, in one commit.** A bead that finds a signature or a rule here wrong appends to `.claude/errata.md` and STOPS.

## What S5 delivers

- **Every stored Position is a rectangle** (migration 102): Frames, Players' last configurations, plan offers, Position history and both copies in Position trials (trials in flight end). Each becomes today's cover rectangle at the Frame's shape, upright on its screen as it hangs, shrunk (and moved in) to lie on that screen; a Cover or Corners quarter turn becomes the Frame's turn. With no known screen size: centred, full width. Frames already upright at their shape stay ready; every other Frame's Position is to check (readiness `position-needed`).
- **Corners and Cover are deleted**: `CornersFit`, `CoverFit`, the `FrameFit` union, `cover_rectangle`, `picture_size`, `convex_clockwise` and their vocabulary in `contracts/`; geometry's Cover and Corners references in `player/geometry.py` (S3 decision 7); the four-corner editor (`QuadEditor`, `convex.js`) in the console. `Calibration.fit` is a `RectangleFit`.
- **The console edits rectangles**: the Position tab draws the screen as it hangs and the picture as one upright rectangle (move, resize at the Frame's shape, nudge, Fill the screen, exact pixels), refusing a change that would leave the screen where it is made, by the same pixel rule as Central and the Player.
- **The turn control**: the Hardware tab's "How the screen hangs" sets the Frame's turn (S2's `PUT /v1/operator/frames/{frame_id}/turn`), one Frame per screen (owner, gate Q4, 2026-10-18: several Frames per screen "later").
- **Position history shows rectangles**: each entry's centre and width; the Fit and corner rows go.

## Decisions and departures, with their cost

1. **The turn is the Position's quarter turn reversed.** A Cover or Corners rotation `r` turned the picture `r` degrees clockwise on the mode as built; a screen hung turned `T` clockwise shows an upright picture when `r + T` is a whole turn. So `q = (floor(r / 90 + 0.5) * 90) mod 360` (half up: 45 goes to 90) and `T = (360 - q) mod 360`: Cover at 90 becomes turn 270 ("top to the left"), at 270 turn 90. Checked against today's `player/geometry.py` `output_to_picture` (Cover at 90 on 3840 x 2160: output (0.1, 0.2) is picture (0.2, 0.9), which the hung screen at 270 gives). Cost: an off-axis rotation (7.5) loses its angle (plan §7 "gives up: off-axis turns"); its Frame is to check.
2. **Each stored copy converts by its own rotation and shape; the Frame's turn comes from its current Position.** The conversion is one pure function of (the stored fit, the Frame shape it was made for, the Output's mode as built): Position history entries use their own `profile_*_px`, trials and bindings their Frame's profile. A history entry made at another quarter turn than the Frame's current one becomes a rectangle for that other hanging (fractions of the other hung screen). Cost: restoring such an entry gives a rectangle meant for the other hanging, which the editor shows and Save checks; rare (a turn change between entries).
3. **With no known screen size every Frame is to check, as plan §6 says.** Cost, and a finding (errata): Central knows an Output's size only from the Player's enrollment, and every real Pi enrolled 0 x 0 until S3's Player (`measured`) runs, so on the live wall migration 102 makes every Frame centred, full width and `position-needed`: the wall shows nothing on a Frame until its Position is checked once (Done on the Position tab). The cheap alternative, not taken without the owner: take the Frame profile as the screen as it hangs when the size is unknown (a full-screen Frame stays ready).
4. **Positions in pixels are judged by the one `place` rule everywhere.** Central (`contracts/frame_fit.py` `place`), the Player (`place`, S1), the console (`src/placement.js`, a mirror held to the Python by a conformance test, as `convex.js` was) and migration 102 (its shrink takes whole pixels so `place` accepts every answer). Cost: a second copy of `place` in JavaScript, tested, and the migration's own SQL (a migration cannot import Python).
5. **The editor refuses an off-screen change where it is made; a trial edit is not refused by Central.** S2 put the off-screen check at Save only (`commit_calibration_trial_in`); an off-screen draft sent to the Pi is refused by the Player (`failed`), so the console never sends one (its `place` is exact, with the reported size and the turn). With no reported size the console steps on a 1920 x 1080 screen as today and Central cannot check either. Cost: none new; the gap is S2's.
6. **`RectangleFit` keeps `mode: "rectangle"`**, now the only form: a stored or sent body names its form, and the console bead (B2), which lands while Cover and Corners still exist, sends the same body before and after their deletion (B6). `FrameFit` (the union's name) goes; every reader names `RectangleFit`.
7. **The turn control lives on the Hardware tab**, beside the Frame profile: how the screen hangs is part of putting the screen up (the console's one-time bring-up, apart from the show), and the Position tab names it in one line with a way there (design language P1: a setting is changed only in its home). Applied the moment it is chosen (no Done); the Position is then to check (S2 decision 2).
8. **Six beads, not three** (plan v3: 90, 90, 60; W5 2 h). Green-alone fixes the order: the console must edit rectangles (B2) before Cover and Corners are refused (B6), and every test and reference that builds them must move first (B3, B4). Four beads run at once, then two: about 1.5 h + 1.25 h of wall clock against W5's 2 h (S6 beside).
9. **Migration 102 records no history entry.** The Position history shows each converted entry as a rectangle and adds nothing for the conversion; the Frame's revision and configuration revision rise (as 075 did), so every Player is sent its new Position and a draft made before the deploy is refused (stale base).

## Rules for every bead

- Siblings share one checkout (or the orchestrator gives G2 its own worktree per bead: see Parallel groups). Keep to your owned files; derived outputs go outside the repo (`$TMPDIR/render-planes-s5-<bead>/`: pytest `--basetemp`). The console's `dist/` and `storybook-static/` are written only by the console bead running (B2, then B5); B6 checks the console with no-emit typecheck and lint only.
- Land: `git add -A -- <owned> && git commit -m '<message>' -- <owned> && git push origin HEAD:feat/render-planes`, Conventional Commits, trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. A commit that meets a sibling's `index.lock` waits and retries. If the push is rejected: `git fetch origin && git rebase origin/feat/render-planes` (your own unpushed commit only), re-run your checks, push.
- Run your changed tests: unit `.venv/bin/python -m pytest -q -n 4 --dist worksteal <tests>`; DB `docker compose -f tests/integration/compose.test-database.yml up -d --wait` then `.venv/bin/python scripts/test_local.py -q -m db -n 4 --dist loadgroup <tests>`; browser `PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q <tests> -n 4 --browser chromium` (built console: `npm --prefix central/console run build`); console `PATH=/Users/matt/.nvm/versions/node/v22.23.2/bin:$PATH npm --prefix central/console run typecheck` and `run lint` (`npm ci` there first if `node_modules` is missing; npm cache on the Dock volume). Static: `.venv/bin/python -m ruff check .`, `.venv/bin/lint-imports`, `python3 scripts/check_docs.py`. When Docker does not answer `docker info` in a minute, push and let CI's `db`, browser and `console-catalog` legs judge; record NOT RUN. CI is the full gate; no local full verify.
- Catalog baselines (a story with none fails CI): a bead that adds or changes stories rewrites only its own with `python3 scripts/catalog_baselines.py -k <its story ids>` (Docker) and commits those PNGs; when Docker hangs, NOT RUN, and the end fix commits CI's actuals from the `console-catalog-<sha>` artifact.
- Console pages: the procedure is `.claude/skills/console-ux/SKILL.md`; every string passes `docs/design-language.md`'s glossary and forbidden words. Docs (design language §4 catalogue, console design §5, decision record, code map) are S6's; keep every module docstring and JSDoc you touch true.
- Migrations are forward-only: never edit 001-101. `.venv/bin/python`; revert `uv.lock` if anything rewrites it.

## Shared primitives

| Primitive | Home | Responsibility | Consumers in S5 | Scheduled |
|---|---|---|---|---|
| `place`, `FrameRectangle`, `Placement` | `contracts/frame_fit.py` (S1, frozen) | a rectangle becomes whole pixels, or is refused | Central's Frame output and Save, the Player, the migration test's oracle, `placement.js`'s conformance | several Frames per screen |
| `hanging` | `contracts/display_turn.py` (S1, frozen) | the screen's size as it hangs | `frame_placement`, the migration test | |
| `placement.js` `hanging`, `place`, `fillScreen` | `central/console/src/placement.js` (new, stubs committed) | the console's mirror of `hanging` and `place`; the largest centred rectangle | `frame-position.tsx` (B2), `frame-profile.tsx` (B5) | the editor of several Frames per screen |
| `RectangleEditor` | `central/console/src/patterns/rectangle-editor.tsx` (new, stub committed) | draws a screen and one rectangle in its pixels; reports moves, resizes, arrows; no rules | `frame-position.tsx` | several Frames per screen (one rectangle each) |
| `Turn`, `TURNS`, `TURN_WORDS` | `central/console/src/domain/turn-words.ts` (new, complete) | how the screen hangs, in words | `frame-turn.tsx`, `frame-position.tsx`, `frame-profile.tsx` | |
| `FrameTurnChange` (TS) | `src/generated/contract.ts` from `central/registry.py` (S2) | the turn command's body | `framesApi.js` `turnFrame`, `frame-turn.tsx` | |
| `TRIAL_REFUSAL_WORDS.position_off_screen` | `central/console/src/domain/trial-refusals.ts` (S2) | the off-screen words | Central's Save refusal and the editor's own refusal (B2) | |
| the conversion of a stored Position | `central/migrations/102_rectangle_positions.sql` (B1) | one expression, applied once per distinct stored fit | every stored copy | none (a migration runs once) |

## The conversion (frozen; B1 writes it as one SQL expression, the rest of the page depends on it)

Inputs of one stored copy: its fit JSON `f` (any of `{}`, a Cover or Corners body with missing keys, or a rectangle), the Frame shape `a = width_px / height_px` it was made for, and the mode as built `(W, H)` of the Output its Frame is bound to (`outputs.observation` `width_px`, `height_px` through `bindings`, as `frame_outputs_in` reads them; unknown when unbound, missing or below 1). Missing keys read as the pre-102 defaults: mode `cover`, corners `[[0,0],[1,0],[1,1],[0,1]]`, rotation 0.

- `f->>'mode' = 'rectangle'`: unchanged, turn unchanged, not to check (rows S2-S4 wrote).
- Otherwise `r` = rotation, `q = (floor(r / 90 + 0.5) * 90) mod 360`, **turn** `T = (360 - q) mod 360`.
- Size unknown: fit `{"mode": "rectangle", "centre_x": 0.5, "centre_y": 0.5, "width": 1.0}`, **to check**.
- Size known: the stored corners `(X_i, Y_i)` (fractions of the mode as built) in pixels `x_i = X_i * W`, `y_i = Y_i * H`; spans `sx = max x - min x`, `sy = max y - min y`; centre `cx = (min x + max x) / 2`, `cy` likewise. `(du, dv) = (sx, sy)` when `q` is 0 or 180, else `(sy, sx)`. Cover rectangle: `hp = greatest(du / a, dv)`, `wp = a * hp` (this is `cover_rectangle(corners, q, (W, H), a)`'s width and height and centre, exactly). Screen as it hangs: `(Wh, Hh) = (W, H)` for T 0 and 180, `(H, W)` for 90 and 270. Centre as it hangs: T 0 `(cx, cy)`; T 90 `(H - cy, cx)`; T 180 `(W - cx, H - cy)`; T 270 `(cy, W - cx)`. Whole pixels: `w = greatest(1, least(Wh, floor(Hh * a + 1e-6), floor(wp + 0.5)))`, `h = floor(w / a + 0.5)`; moved in: `cxh' = least(greatest(cxh, w / 2), Wh - w / 2)`, `cyh' = least(greatest(cyh, h / 2), Hh - h / 2)`. Fit `{"mode": "rectangle", "centre_x": cxh' / Wh, "centre_y": cyh' / Hh, "width": w / Wh}`.
- **Exact** (stays ready) when the size is known, `r = q` (a quarter turn, nothing off-axis), and each corner lies within 1 pixel on each axis of the matching corner of the cover rectangle's footprint on the mode as built (the axis-aligned box centred `(cx, cy)` of size `(wp, hp)` for q 0 and 180, `(hp, wp)` for 90 and 270; corners top-left, top-right, bottom-right, bottom-left of the mode as built). Otherwise to check.

Self-checked by the code architect over the grid below (1,260 cases): every answer is accepted by `place` on `hanging((W, H), T)` with exactly `(w, h)` pixels, and `(wp, hp, cx, cy)` equal `cover_rectangle`'s within 1e-6.

Worked values (`a`, Output, fit → turn, fit, exact):
- E1: 1920 x 1080 Frame, Cover whole screen at 0, Output 3840 x 2160 → turn 0, (0.5, 0.5, 1.0), exact (stays ready).
- E2: 1080 x 1920 Frame, Cover whole screen at 90, Output 3840 x 2160 → turn 270, (0.5, 0.5, 1.0), exact.
- E3: 1600 x 1200 Frame, Cover whole screen at 0, Output 1920 x 1080 → turn 0, (0.5, 0.5, 0.75) (1440 x 1080 px), to check.
- E4: 1920 x 1080 Frame, Cover corners (0.25, 0.25)-(0.75, 0.75), Output 1920 x 1080 → turn 0, (0.5, 0.5, 0.5), exact.
- E5: any Frame unbound or on an Output reported 0 x 0, Cover at 270 → turn 90, (0.5, 0.5, 1.0), to check.
- E6: Cover at 7.5 → turn 0, to check whatever its corners.

## B1: migration 102 (about 90 minutes; G1)

**Owns:** `central/migrations/102_rectangle_positions.sql` (new), `tests/test_migration_102_rectangle_positions.py` (new), `tests/fixtures/cover_rectangles.json` (new, derived), `tests/test_migration_101_frame_turn.py`.

**The migration** (data only; no column, no CHECK, no function, trigger or stored procedure: the owner's "no DB logic"; temp tables `ON COMMIT DROP` as 099 does):
1. `UPDATE node_calibration_trials SET state = 'ended' WHERE state = 'active'` (trials in flight end).
2. A temp table of every distinct (fit, shape, mode as built) over: `frames.calibration->'fit'` with `frames.profile`; `position_entries.fit` with the entry's `profile_width_px`, `profile_height_px`; `node_calibration_trials.calibration->'fit'` and `saved_calibration->'fit'` (when not NULL) with their Frame's profile; each copy's size through its Frame's binding and Output now.
3. One `SELECT` over it writes the answers (fit, turn, exact) by the conversion above: the one expression (LATERAL steps for readability are fine; it appears once).
4. Frames: `calibration = {"revision": COALESCE(revision, 1) + 1, "fit": <answer>}`, `turn = <answer's turn>`, `configuration_revision = configuration_revision + 1`, `position_generation = NULL` unless exact (unchanged when exact). A Frame whose fit is already a rectangle is left as it is (no revision rise).
5. `position_entries.fit`, `node_calibration_trials.calibration->'fit'` and `saved_calibration->'fit'` (revisions kept) take their answers.
6. `player_configurations.configuration->'bindings'` and `plan_offers.manifest->'bindings'`: every binding object's `calibration` becomes its Frame's new `frames.calibration`; a binding whose Frame is gone takes `{"revision": <its revision or 1>, "fit": {"mode": "rectangle", "centre_x": 0.5, "centre_y": 0.5, "width": 1.0}}` (099's form of the same rewrite).
7. Header comment: what it converts, the turn rule, exactness, "Position to check", no backwards compatibility, the S5 page.

**The fixture** `tests/fixtures/cover_rectangles.json`: today's answers, recorded once before B6 deletes them, by running today's `contracts.frame_fit.cover_rectangle(corners, q, (W, H), width_px / height_px)` over the grid: corners full screen, (0.1,0.1)-(0.9,0.9), (0.05,0.2)-(0.6,0.95), the skewed quad ((0.1,0.05),(0.95,0.1),(0.9,0.9),(0.02,0.85)) and (0,0)-(0.2,0.3); q in 0, 90, 180, 270; Outputs 3840 x 2160, 1920 x 1080, 1080 x 1920, 1280 x 1024; shapes 1920x1080, 1080x1920, 1600x1200, 1200x1600, 1000x1000, 1500x1000, 1000x1500 (560 records: inputs and `centre_x`, `centre_y`, `width`, `height`). A `"made_by"` key names the commit and the call. The generator is not committed (its function is deleted by design).

**Acceptance** (DB tier; seeds are raw SQL and JSON on a schema `migrate_before(db, "102")` built, never today's models, so the test outlives B6):
- The grid: one Player and Output per Output size with that observation, one Frame bound to each, and per (corners, rotation, shape) a `position_entries` row (rotations 0, 90, 180, 270, 7.5, 45, 135.2, 300, 359.9 as Cover; 0, 90, 180, 270 as Corners); plus one unbound Frame with every case. After `db.migrate()`, each entry's fit `RectangleFit.model_validate`s, `place(fit.rectangle(), hanging((W, H), T), a)` accepts it, and against the fixture's record for (corners, q, Output, shape): when the recorded rectangle fits the hung screen, the placement's size is the record's within 1 px and its centre, mapped back to the mode as built by T, the record's within 1 px; when it does not, the placement keeps the shape (`h == floor(w / a + 0.5)`), fills the limiting side (`w == Wh` or `h == Hh`) and its centre is the record's clamped, within 1 px. The unbound Frame's entries are all (0.5, 0.5, 1.0).
- E1-E6 as Frames (bound, positioned and READY before through `position_generation = generation`): turn, fit, `frame_readiness_in` (READY for E1, E2, E4; POSITION_NEEDED otherwise), revision and configuration revision each one higher; Frames rows with `calibration '{}'` (as test_migration_099 seeds) convert as Cover on the whole screen at 0.
- An active trial is `ended`, a saved one keeps its state; both its copies are rectangles; a Player configuration's binding equals its Frame's new calibration; a plan offer's binding of a gone Frame is the centred full-width rectangle; `PlayerConfiguration.model_validate` and `Plan.model_validate` read both rows.
- `tests/test_migration_101_frame_turn.py`: seeds its Cover-at-90 Frame as raw JSON (not `CoverFit`), applies migrations through 101 only (`migrate_before(registry.db, "102")` after `before_101`), and asserts what it asserts today; its 101 document test unchanged.
- Every `tests/test_*migration*.py`, `tests/test_upgrade_*.py` and `tests/test_display_picture_settings.py` still pass (the latter's 075 cases apply single files, not 102; B6 owns their CoverFit lines).

**Mutation probe:** make the turn `q` instead of `360 - q`: E2's turn is 90 and the grid's 90 and 270 cases map back wrong; the test fails.

## B2: the console edits rectangles (about 90 minutes; G1)

**Owns:** `central/console/src/placement.js`, `central/console/src/patterns/rectangle-editor.tsx`, `central/console/src/patterns/rectangle-editor.stories.tsx` (new), `central/console/src/patterns/quad-editor.tsx` and `quad-editor.stories.tsx` (deleted), `central/console/src/convex.js` (deleted), `central/console/src/useDraft.js`, `central/console/src/liveAdjustment.js` (the restore's fit line and the typedef docs only), `central/console/src/domain/frame-position.tsx`, `central/console/src/domain/frame-position.stories.tsx`, `central/console/src/domain/position-history.tsx`, `central/console/src/domain/position-history.stories.tsx`, `central/console/src/pages/frame-page.tsx` (the `Frame` type, the Position tab, `PositionHistory`'s props, the Hardware tab's `FrameDraftFit` fallback), `tests/test_console_placement.py` (new), `tests/test_console_convex.py` (deleted), `tests/test_console_routes_r4.py` (`FRAME_PAGE_IMPORTS["frame-position.tsx"]` gains `"placement.js"`), `tests/browser/test_console_frame_page_browser.py` (the Position editor's tests and helpers; not the three Frame profile note tests, B5's), `tests/browser/test_frame_fit_browser.py`, `tests/browser/test_position_history_browser.py`, catalog baselines `patterns-quadeditor--*` (deleted), `patterns-rectangleeditor--*`, `domain-frameposition--*`, `domain-positionhistory--*`. Derived, not committed: `central/console/dist`, `storybook-static`.

**Frozen signatures:**

```js
// src/placement.js (stubs committed; bodies yours; JSDoc there is the contract)
export function hanging(size, turn)            // [W, H] as it hangs; Error("display_turn"|"screen_size")
export function place(rectangle, screen, aspect) // Placement | null (Python's "placement_outside"); Error("frame_rectangle"|"placement_screen"|"placement_aspect")
export function fillScreen(screen, aspect)     // {centre_x: 0.5, centre_y: 0.5, width: min(W, floor(H * aspect + 1e-6)) / W}

// src/useDraft.js
/** @typedef {{mode: "rectangle", centre_x: number, centre_y: number, width: number}} Fit */
export function fitOf(committed)               // the saved fit's three numbers (absent: 0.5, 0.5, 1), mode "rectangle"
export const draftKey                          // canonical([centre_x, centre_y, width, lighting])
export function useDraft(frameId, committed, committedLighting)
//   updateHandles(patch: Partial<{centre_x, centre_y, width}>) -> {valid, reason?}: refuses (no change) unless
//   0 <= centre <= 1 and 0 < width <= 1 (FrameRectangle's check), reason RECTANGLE_REASON. CONVEX_REASON,
//   QUARTER_TURNS and quarterTurn are deleted.
export const RECTANGLE_REASON = "A picture's centre is on the screen and its width is more than none and at most the screen's."
```

```ts
// src/patterns/rectangle-editor.tsx (stub committed): RectangleEditorProps {label, screen: {width, height},
//   picture: PixelRectangle | null, onMove(dx, dy), onResize(dWidth), onArrow(dx, dy), disabled?}
// src/domain/frame-position.tsx (changed props)
export interface FramePositionProps {
  adjustment: Adjustment;
  size: PixelSize | null;        // the Output's mode as built, as reported; null: none reported
  turn: Turn;                    // the Frame's turn (inventory `turn`)
  aspect: number | null;         // the Frame profile's width_px / height_px; null: the profile is not whole
  piHref: string | null;
  readiness?: Readiness;
  whatFits?: React.ReactNode;
  hardwareHref: string;          // the Frame page's Hardware tab, for the turn's line
}
// src/domain/position-history.tsx: positionDetails(entry, names?, profiles?), positionDays(entries, adjustment,
//   names?, profiles?), PositionHistoryProps and PositionHistoryListProps lose `size`.
```

**Rules:**
- The editor works on the screen as it hangs: `screen = hanging([size.width, size.height], turn)` (no reported size: `hanging([1920, 1080], turn)` with today's note). Every change is a candidate draft judged by `place(candidate, screen, aspect)`: null refuses it where it is made (the inline ProblemCard with `TRIAL_REFUSAL_WORDS.position_off_screen`, nothing sent); a placement sends it through `adjustment.change`. Moves are screen pixels: `centre_x += dx / W`, `centre_y += dy / H`; a resize keeps the centre, `width += dWidth / W`. The arrows and the NudgePad step 1, 10 or 50 px (today's Step control).
- Layout (Position tab, inside `LiveAdjustment`, top to bottom): the turn's line ("The screen hangs: <TURN_WORDS[turn]>." with a `LinkToOwner`-style link "Change it on the Hardware tab"), the unknown-size note, `RectangleEditor`, the inline problem, Nudge (Step, `NudgePad` "Move the picture"), Size (`Slider` and `NumberInput` of the width in pixels of the screen as it hangs, from 1 to the screen's width), buttons "Fill the screen" (`fillScreen`) and "Centre" (centre 0.5, 0.5 when it places), `whatFits`, and Exact values (centre across and down and width, in pixels of the `W x H` screen as it hangs). No Fit choice, no rotation, no angle. With `aspect` null: a note to set the Frame profile on the Hardware tab, no editor.
- `positionDetails`: Centre (`<x %> across, <y %> down`) and Width (`<w %> of the screen`) for a rectangle (`fit.mode === "rectangle"`: the generated type is still the union until B6, which removes the test), then the lighting rows and Made for, as today; no Fit row, no corner or rotation rows.
- `frame-page.tsx`: `Frame` gains `turn?: Turn`; FramePosition gets `turn={frame.turn ?? 0}`, `aspect` from `wholeProfile`, `hardwareHref` (`formatRoute` of the Frame's hardware tab); the Hardware tab's `FrameDraftFit` fallback is `{ mode: "rectangle" }`.
- `liveAdjustment.js` restore: `change(fitOf(result.data.calibration))` with the three fields.
- Stories: `Patterns/RectangleEditor` Default, Turned (a portrait screen), Disabled, NoPicture; `Domain/FramePosition` Landscape, TurnedScreen, UnknownSize, OffScreenRefused, NoProfile; `Domain/PositionHistory` as today with rectangle entries.

**Acceptance:**
- `tests/test_console_placement.py` (unit; Node required where CI runs, skipped elsewhere as `test_console_convex.py` did): `place` and `hanging` equal Python's `place`/`hanging` on vectors built in the test (every screen of the migration grid hung at every turn, widths 0.1-1.0, centres including both edges and half-pixel cases, aspects of the grid; Python's ValueError codes as JS errors, "placement_outside" as null); `fillScreen`'s answer places and the same with one pixel more width does not.
- Browser (fake Central of `test_console_frame_page_browser.py`): dragging the picture moves it and sends the new centre; dragging the handle changes the width at the Frame's shape; a move past the edge is refused where it is made, says so, and sends nothing; Fill the screen sends the largest centred rectangle; a Frame turned 90 (inventory `turn`) draws a portrait screen and its draft is in fractions of it; a refresh never overwrites the draft; a stale Done is refused (the session tests keep passing with the new helpers); with no reported size a step is a pixel of a 1920 x 1080 screen; Position history lists rectangle entries and Restore puts one back until Done; the Position tab fits a phone.
- `npm run typecheck`, `run lint`, `tests/test_console_routes_r4.py`, the catalog's own stories' baselines.

**Mutation probe:** drop `x + width <= W` from `placement.js` `place`: `tests/test_console_placement.py` and the browser's past-the-edge test fail.

## B3: geometry without Cover and Corners (about 60 minutes; G1)

**Owns:** `player/geometry.py`, `tests/test_print_look_reference.py`, `tests/test_room_light_physics.py`, `tests/test_player_target_reference.py`, `tests/test_player_test_patterns.py`, `tests/test_player_board_reference.py`, `tests/test_fit_pass_reference.py`, `tests/test_pattern_pass_reference.py`, `tests/node/display/test_display_picture_controls.py`, `tests/native_fit_pass_harness.py` (its docstring only).

**Changed signatures** (`player/geometry.py`; deleted: `_solve`, `homography`, `inverse`, `multiply`, `IDENTITY`, `_cover_matrix`, `output_to_picture`, `inside_opening`, `picture_source_uv`, `board_draw`, `_board`, `opening_size_mm`, `output_pixel`, `frame_lights`, `_opening_lights`, and the imports of `CornersFit`, `CoverFit`, `Corners`, `cover_rectangle`; kept: `transform`, `UNDO_QUARTER_TURN`, `buffer_to_picture`, `placement_uv`, `picture_board`, `picture_lights`, `final_pixel`, every physics and pattern rule):

```python
@dataclass(frozen=True)
class FrameLights:                 # `to_opening` removed: the lights are in the picture's own unit square
    size_mm: tuple[float, float]
    depth_mm: float
    paper: Paper
    lights: tuple[LitLight, ...]
    peak: float
def picture_lights(lighting: FrameLighting, document: RoomLightsDocument | None, profile: FrameProfile,
                   display: DisplayLight) -> FrameLights        # unchanged signature; `_opening_lights`'s body folded in
def picture_board(profile: FrameProfile, modules: QrModules | None) -> BoardDraw   # unchanged; `_board`'s body folded in
def irradiance(lights: FrameLights, x: float, y: float) -> Linear  # (x, y): the picture's point, 0..1 across and down as it hangs
def final_pixel(...)               # unchanged signature; `x, y` documented as the picture's point (what every caller passes since S3)
```

**Rules:** the physical size of the picture is the profile's own (`_profile_size_mm`, a turn turns the screen, not the wall); the module docstring states the rules it keeps and drops the Cover and Corners paragraphs and "until S5"; `pattern_signal`'s docstring names the picture's (u, v), not a homography. Reference tests that held a rule through a Cover or Corners fit hold it through `picture_lights`/`picture_board` on the same profile (a landscape and a portrait profile where the rule depends on it); tests of the deleted functions themselves (the cover matrix, the per-Frame mask, two Frames on one Output, `opening_size_mm`'s quarter turn) are deleted with them. `FrameLights` loses its first field: its one constructor is `picture_lights`, and only `tests/test_fit_pass_reference.py` (owned) reads `to_opening`.

**Acceptance:** the owned tests pass with no import of `CoverFit`, `CornersFit` or a deleted function (`rg -n "CoverFit|CornersFit|frame_lights|board_draw|output_pixel|opening_size_mm|homography|to_opening" player tests` finds nothing in B3's files); `tests/test_plane_renderer.py` and `tests/test_player_look.py` (not owned) still pass; the display harness's fit pass and pattern pass cases (`tests/native_fit_pass_harness.py`, CI node-pid1) judge the shaders against the kept references; ruff, lint-imports.

**Mutation probe:** make `picture_lights` take the profile's size as (H, W): the far light's band and the close light's place in `test_room_light_physics.py` and `test_print_look_reference.py` (a landscape profile) move and fail.

## B4: the tests' Positions are rectangles (about 75 minutes; G1)

**Owns:** `tests/support/position.py`, `tests/test_display_changed_readiness.py`, `tests/test_executor.py`, `tests/test_fit_analysis.py`, `tests/test_frame_output.py`, `tests/test_frame_output_wiring.py`, `tests/test_node_calibration.py`, `tests/test_node_display.py`, `tests/test_node_runtime_reconciliation.py`, `tests/test_photo_fit.py`, `tests/test_plane_screen.py`, `tests/test_player_trial_lighting.py`, `tests/test_position_history.py`, `tests/test_position_history_store.py`, `tests/test_registry.py`, `tests/test_source_preview_matches_plan.py`, `tests/test_tuning_snapshot.py`, `tests/test_wall_demo.py` (its `frame_output` line only), `tests/test_wall_plan_tracer.py`, `tests/test_print_look_contract.py`, `tests/native_display_service_probe.py`, `tests/browser/capture_photo_5a_evidence.py`, and any test file the default flip below shows that no other S4 or S5 bead owns.

**Rules:** every Position these files build or send (`CoverFit`, `CornersFit`, a `"mode": "cover"|"corners"` body, `FULL_SCREEN_CORNERS`) becomes a `RectangleFit` (or its JSON) that tests the same behaviour, and nothing in them imports a symbol B6 deletes (`CoverFit`, `CornersFit`, `FrameFit`, `FULL_SCREEN`, `Corners`, `cover_rectangle`, `picture_size`, `convex_clockwise`, `CONVEX_EPSILON`). `tests/support/position.py`: `WHOLE_WIDTH = Calibration(fit=RectangleFit())` replaces `FULL_SCREEN_CORNERS` as `save_position`'s default (a Save with no reported size is never checked; with one, a test whose Frame's shape is taller than its screen passes its own rectangle); its docstrings say so. Expected values that followed from a Cover or Corners Frame output are re-derived from `frame_placement`'s, never loosened; a test whose subject was the old form's own behaviour is B6's (listed there), not changed here. `native_display_service_probe.py` (node-pid1 harness): its Save sends `{"mode": "rectangle", "centre_x": 0.5, "centre_y": 0.5, "width": 0.5}` and asserts the saved width. No production file changes.
- **B6 changes a new Frame's default Position** (`create_frame`, `Calibration()`: Cover on the whole screen becomes `RectangleFit()`). A test in your files that reads a Frame output, a Position or a Save of a Frame it created without saving one must not depend on that default: save a rectangle first, or assert nothing about it. Find them by flipping `Calibration`'s default to `RectangleFit` and `create_frame`'s to `Calibration()` locally (never committed; undo by reversing the edit) and running the unit and DB tiers over every test file that creates a Frame (`rg -l "create_frame|FrameCreate" tests`); a failing file no other S4 or S5 bead owns joins your owned list (name it in your commit), one another bead owns goes to `.claude/errata.md` for B6.

**Acceptance:** every owned test passes (unit and DB tiers; the probe in CI's node-pid1 leg); `rg -n "CoverFit|CornersFit|FULL_SCREEN|\"cover\"|\"corners\"" <owned files>` finds nothing that is a Position; ruff.

**Mutation probe:** make `contracts.models.frame_output` answer the Output's size for a rectangle: `tests/test_frame_output.py` and `tests/test_wall_plan_tracer.py` (rectangle Frames whose Frame output is smaller than the Output) fail.

## B5: the turn control (about 60 minutes; G2, after B2)

**Owns:** `central/console/src/domain/frame-turn.tsx`, `central/console/src/domain/frame-turn.stories.tsx` (new), `central/console/src/framesApi.js` (`turnFrame`), `central/console/src/pages/frame-page.tsx` (the Hardware tab: `FrameTurn` after `FrameProfile`), `central/console/src/domain/frame-profile.tsx`, `tests/test_console_routes_r4.py` (`"frame-turn.tsx": {"framesApi.js", "useMutate.js"}`; `frame-profile.tsx` gains `"placement.js"`), `tests/browser/test_console_frame_page_browser.py` (the three Frame profile note tests only: `test_the_frame_profile_notes_a_reported_resolution_it_does_not_match`, `test_no_note_when_the_reported_resolution_matches_as_mounted`, `test_a_rebound_frame_does_not_trust_its_old_rotation`), `tests/browser/test_frame_turn_browser.py` (new), catalog baselines `domain-frameturn--*`.

**Frozen signatures** (stubs committed):

```ts
// src/domain/frame-turn.tsx
export interface FrameTurnProps { frameId: string; turn: Turn; generation: number; }
export function FrameTurn(props: FrameTurnProps): React.ReactElement
```
```js
// src/framesApi.js
export async function turnFrame(frameId, body)  // body: FrameTurnChange; {ok: true, changed, generation} | {ok: false, code, status}
```

**Rules:**
- A `Section` "How the screen hangs": a `SegmentedControl` labelled "Screen" over `TURNS` in `TURN_WORDS`, help "Where the screen's own top edge is, as you face it. Photo Wall turns the picture to match." Choosing sends `turnFrame(frameId, {turn, expected_generation: generation})` through `useMutate` (the inventory is read again); the control is disabled while it runs. Answers: changed → note "The picture turns now. Check its position on the Position tab, then press Done there." (a link to the Position tab); not changed → nothing; 409 `binding_generation_conflict` → inline ProblemCard "This Frame changed while you chose. Choose again."; 404 `unknown_frame` → "This Frame no longer exists."; no answer → "Photo Wall's server did not answer. Choose again." Shown for a bound and an unbound Frame.
- `frame-profile.tsx`: the reported-resolution note compares the profile with `hanging([observation.width_px, observation.height_px], frame.turn ?? 0)` (src/placement.js), whatever the readiness; its words name the turn ("The screen hangs <TURN_WORDS[turn]>.") instead of "The saved rotation …° was considered."; `Frame.calibration` loses `fit.rotation`.
- Stories: `Domain/FrameTurn` Upright, TopToTheRight, Sending, Refused.

**Acceptance:** browser, through the real Registry (`operator_server`): choosing "Top to the right" makes Central's Frame turn 90 (inventory), the Frame `position-needed`, and the Position tab draws a portrait screen; a choice against a stale generation (another write first) shows the conflict words and changes nothing; the profile note follows the turn (a 1080 x 1920 profile on a 1920 x 1080 Output turned 90: no note; turned 0: the note). typecheck, lint, `tests/test_console_routes_r4.py`, the new stories' baselines.

**Mutation probe:** send the Frame's current turn instead of the chosen one in `turnFrame`'s body: the browser test's turn stays 0 and fails.

## B6: Cover and Corners deleted (about 75 minutes; G2, after B1-B4)

**Owns:** `contracts/frame_fit.py`, `contracts/models.py`, `contracts/node_frame.py` (its line naming `player/native.py`), `central/registry.py` (`create_frame` and its import), `central/fleet/position_history.py`, `central/fleet/position_views.py`, `central/infra/position_history_store.py`, `player/plane_renderer.py` (`_place` and the module docstring's "The drawn Position"), `central/console/src/generated/contract.ts` (regenerated by `python -m scripts.console_types`, never by hand), `central/console/src/domain/position-history.tsx` (the `fit.mode === "rectangle"` test only), `tests/test_frame_fit.py`, `tests/support/frame_fit_vectors.json` (deleted), `tests/test_frame_fit_settings.py`, `tests/test_rectangle_fit.py`, `tests/test_contracts.py`, `tests/test_display_picture_settings.py` (the 075 cases' Position lines), `tests/test_plane_renderer.py`.

**Changed signatures:**

```python
# contracts/frame_fit.py keeps exactly: OutputSize, PictureSize, window_holds, FrameRectangle, Placement, place
# (and _finite, _half_up). Deleted: Point, Corners, FULL_SCREEN, CONVEX_EPSILON, convex_clockwise,
# CoverRectangle, cover_rectangle, picture_size, _pixels_up. __all__ follows; the docstring states the two rules kept.

# contracts/models.py. Deleted: FitPoint, FitCorners, CornersFit, CoverFit, FrameFit.
class RectangleFit(Model): ...                        # unchanged (mode: Literal["rectangle"] = "rectangle")
class Calibration(Model):
    revision: int = Field(default=1, ge=1)
    fit: RectangleFit = Field(default_factory=RectangleFit)
def frame_output(profile: FrameProfile, calibration: Calibration, output: OutputSize | None,
                 turn: DisplayTurn) -> PictureSize | None   # frame_placement's (width, height); None for no size or off screen
def off_screen(profile: FrameProfile, calibration: Calibration, output: OutputSize | None,
               turn: DisplayTurn) -> bool                   # True exactly when the size is known and frame_placement refuses "placement_outside"
# frame_placement unchanged.

# central/fleet/position_history.py
@dataclass(frozen=True)
class PositionValues:
    fit: RectangleFit
    lighting: FrameLighting
# central/fleet/position_views.py: PositionEntryView.fit: RectangleFit
# central/infra/position_history_store.py: entries read by RectangleFit.model_validate (no TypeAdapter of a union)
# central/registry.py: create_frame stores Calibration() (centred, full width); its docstring says so.
# player/plane_renderer.py: _place has no form check ("failed" only when `place` refuses or the profile's aspect is bad).
```

**Rules:** a body or stored record with `"mode": "cover"` or `"corners"`, or with `corners` or `rotation`, is refused by pydantic (extra fields forbidden): no reader, no default, no compatibility path. Every docstring naming Cover, Corners, corners or the rotation in these files is rewritten to the rectangle (or deleted). `tests/test_frame_fit.py` keeps `window_holds`'s and `place`'s tests only; `test_frame_fit_settings.py` holds: a new Frame starts at `RectangleFit()`; a Cover or Corners Save body is refused 422 before any write; S2's rectangle cases unchanged. `test_rectangle_fit.py` drops the Cover-and-Corners-ignore-the-turn case and gains "a Cover or Corners `Calibration` is a validation error". `test_contracts.py`: the folded-aperture cases go; the default is the rectangle; `corners`, `rotation` and the old modes are refused. `test_display_picture_settings.py`: the 075 cases assert 075's stored JSON as JSON (today's models cannot read it) and the configuration case applies `102_rectangle_positions.sql` after 075 before reading the configuration through `Coordinator`. `test_plane_renderer.py`: the "older form fails" case goes and `framed` takes a `RectangleFit | None`.

**Acceptance:** `rg -n "CoverFit|CornersFit|FrameFit\b|cover_rectangle|picture_size|convex_clockwise|FULL_SCREEN|CONVEX_EPSILON" --glob '!docs/**' --glob '!*.md' .` finds only the console's `FrameFit` of `photo-fit.ts` (another thing: a Frame's fit analysis) and `central/wall_plan/fit_analysis.py`'s; `python -m scripts.console_types --check` exits 0 and `contract.ts` has no `CornersFit` or `CoverFit`; the owned tests pass (unit and DB); the full unit tier's Position tests (B3's, B4's) still pass; console typecheck and lint (no build); ruff; lint-imports.

**Mutation probe:** keep `CoverFit` in `Calibration.fit` (`CoverFit | RectangleFit`): the "a Cover Save body is refused 422" and "a Cover `Calibration` is a validation error" tests fail.

## Parallel groups

1. **G1: B1 ∥ B2 ∥ B3 ∥ B4.** Disjoint files and derived outputs: B1 is SQL and DB tests, B2 the console (the only one building `dist` and `storybook-static` and writing baselines), B3 `player/geometry.py` and its reference tests, B4 other tests. All four land while Cover and Corners still exist, each green alone.
2. **G2: B5 ∥ B6**, after all of G1. B5 needs B2 (shared console files and builds); B6 needs B1 (the migration test no longer reads today's old types), B2 (the console sends rectangles), B3 and B4 (nothing else imports the deleted symbols). B5 builds the console and runs browser tests that start Central from source while B6 edits `contracts/models.py`: run them in separate worktrees (fan-out lanes), or B6 first in one checkout.

## Test architecture

- **Pure, unit:** `place`/`hanging` (S1's tests), the JS mirror's conformance (`tests/test_console_placement.py`, Node over shared vectors), `RectangleFit` and `Calibration` refusing old forms, `frame_output`/`off_screen`, geometry's physics and patterns on `picture_lights`/`picture_board`.
- **DB:** migration 102 against recorded cover answers on a grid (a property test: places, keeps shape, maps back by the turn) and worked Frames (readiness, revisions, trials, configurations, offers); Save refusing old bodies; the turn command (S2's).
- **Browser:** the rectangle editor through the fake Central's trial rules; the turn control through the real Registry; Position history.
- **Catalog:** each new or changed story rendered, axe-checked and diffed in dark and light.
- **Conformance boundaries:** `place` across Python, the Player (S1), the console mirror and the migration's whole pixels; the console contract (`console_types --check`).
- **Not proven in S5:** the live wall's Frames after the migration (the deploy; decision 3), the Pi turning the screen from the console's control (S3's harness covers Display Host; the bench covers the rest).

## Maintenance risks

- **Two copies of `place`** (Python, JS) and one SQL shrink: the conformance test holds the JS; the migration runs once. A change to `place`'s rounding must change `placement.js` in the same commit (the test fails otherwise).
- **`RectangleFit.mode`** is a one-member discriminator; if a second form ever comes, it is the union's seed (keep it), but never grow a field-by-field compatibility reader.
- **Several Frames per screen (Q4 later):** the editor draws one rectangle; the pattern takes one `picture`. The next step is a list of rectangles on one screen with the controller budget (S1's) checked in the console, and `turn_frame` turning every Frame of the screen together (S2's note).
- **The live wall after deploy** is one Done per Frame (decision 3) unless the owner picks the profile fallback.
- **`final_pixel`'s `x, y` and `opening_uv`** are both the picture's point since S3 (`to_opening` gone); merging the two arguments is a later cleanup (residual), not S5's.

## Integration points with existing code

- `contracts/frame_fit.py` `place` and `contracts/display_turn.py` `hanging` (S1): the one pixel rule; `contracts/models.py` `frame_placement`, `frame_output`, `off_screen` (S2) lose their old-form branches.
- `central/registry.py` `turn_frame` and its route (S2): the turn control's command, unchanged. `commit_calibration_trial_in` (S2's off-screen check): unchanged.
- `central/infra/display_store.py` `frame_outputs_in` (the Output size the migration reads the same way), `output_documents` (a migrated turn is re-sent at the next read: its digest covers the turn).
- `player/plane_renderer.py` (S3): places by `RectangleFit` already; only its form check goes.
- The console's live adjustment (`liveAdjustment.js`, `useDraft.js`): the draft, its base revision and the session are unchanged; only the fit's shape changes. A turn raises the Frame's generation, so an open session is invalidated by Central (`_same`) and begun again, and the Frame is `position-needed` until Done (S2).

## For S6 (docs), named here so the docs bead can find them

`docs/design-language.md` §4 (QuadEditor → RectangleEditor; the turn control), §8 rows for Position; `docs/operator-console-design.md` §5 (the Position tab, the Hardware tab's "How the screen hangs"); `docs/architecture.md` line ~120 ("The Frame fit (1c)": four corners, fit and rotation); `docs/execution-contract.md` and `docs/module-planner.md` wherever the Frame output is "Cover's rectangle"; `docs/operator-console-delivery-plan.md` (convex.js) as history; AGENTS.md's `contracts/` row (`frame_fit.py`: "the Frame fit's corners and cover rectangle"); the decision record (the turn rule `T = 360 - q`, the conversion, decision 3's cost).
