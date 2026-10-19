# The Frame's box: frozen page

Branch `feat/frame-box-position`, cut from `origin/main` at 53702f41 (v0.41.0, render planes, PR #115). Process: `~/.claude/skills/implementation-workflow/SKILL.md`. Open PR #118 (trial tag fix) touches `player/plane_screen.py` and `player/plane_renderer.py` only; expect a rebase in fb-6 and fb-9.

The interface commit that carries this page holds every NEW symbol of fb-1 and fb-4 (and fb-2's `screen_px`) as code with no bodies (Python `...`; JS and TS stubs throw `Error("frame box bead fb-N")`): `contracts/frame_fit.py` (`ScreenMm`, `BOX_SLACK`, `FrameBox`, `Placement.aspect`, `place_box`, `box_size_mm`), `contracts/models.py` (`BoxSide`, `PositionBox`, `frame_size_mm`), `central/displays/model.py` `screen_px`, `central/console/src/placement.js` (`placeBox`, `boxSizeMm`), `central/console/src/domain/position-box.ts` and `central/console/src/patterns/box-editor.tsx`. Nothing imports them yet. **Changes to existing signatures are stated here and made by the bead that owns them, with every caller and test, in one commit.** A bead that finds a signature or a rule here wrong appends to `.claude/errata.md` and STOPS.

## The owner's words (the only requirements this page answers)

- 2026-10-19, after trying v0.41.0: "I can only set the width because it assumes a 16x9 aspect ratio. That's wrong. Aspect ratio will be unique per frame, and I need to be able to set width and height independently, along with the position of that box on the display".
- 2026-10-19, shown that a Frame stores its shape three times (wall placement `x_mm/y_mm/width_mm/height_mm`; Frame profile `width_px/height_px/diagonal_inches/video`; the Position's centre and width at the profile's aspect): "diagonal inches is a weird concept when we already have width and height" and "I agree with the breakdown".
- The breakdown he agreed: (1) the Position (left, top, width, height of the Frame's box on the Display's screen as it hangs, plus the screen's turn) is the one source of the Frame's shape and pixel size, and photos are judged and prepared from it; (2) wall placement keeps only where the Frame hangs on the wall, its physical size coming from the Display's real size scaled by the box, not typed again; (3) the profile shrinks to what is not geometry (whether video is allowed) or folds into the Frame's own settings, and `diagonal_inches` goes, its users taking the physical size from the Display's real size.

## What this delivers

- **The Position is a box** (`PositionBox`: `left`, `top`, `width`, `height`, fractions of the screen as it hangs). Width and height are set independently; the Frame's shape is whatever the box is. A box always lies on its screen, so "the picture would run off the screen" can no longer happen.
- **The Frame's pixels, shape and physical size are worked out, never typed**: pixels = `place_box(box, screen as it hangs)` on the Output's mode; shape = those pixels' ratio; physical size = the box's share of the Display's picture area from its EDID.
- **Photos are judged and sized by the Frame output alone.** A Frame whose screen size is not known has no shape, so it gets no photos (it cannot show any either); the photo-fit analysis lists it as not judged.
- **"Shows videos" is the Frame's own setting** (`frames.video`), a switch on the Hardware tab.
- **The Frame profile, `diagonal_inches` and the wall placement's width and height are gone**, from the database, the wire, the Player, the console and the docs.
- **Display Host reports each display's picture area** from its EDID; Central keeps it on the Display, shows it on the Hardware tab and sends it on each binding so the Player draws the room lights and the mat at the Frame's real size.

## Decisions and departures, with their cost

1. **Fractions, not pixels.** A box in fractions survives a mode change (1080p to 4K keeps the same box; the pixels follow) and gives the physical size without the mode (box share x Display mm); pixels would need a rewrite on every mode change and a second rule for millimetres. Edges round half up (`place_box`), so two boxes that share an edge share its pixel column. Cost: on a mode of another aspect (a 1280 x 1024 mode on a 16:9 panel) the box's pixel shape changes with it; the Frame output follows, so photos are judged by what is drawn.
2. **The box is always on its screen.** `FrameBox` refuses `left + width > 1` and `top + height > 1` at construction, and `place_box` is total (every box on every screen places at least 1 x 1). Departures: `off_screen`, `ValueError("placement_outside")`, `TrialRefusal.POSITION_OFF_SCREEN` and the Player's `"failed"` placement go (fb-6). Cost: none; a guarantee moves from a runtime check to construction.
3. **The screen's size has one rule, `screen_px`**: the Pi's report of the Output's mode (`outputs.observation`) when at least 1 x 1, else the preferred mode of the Display seen there (Weston runs the preferred mode when given none). Migration 102 already used this rule in SQL; fb-2 makes it app code. Departure: today a Pi that enrolled an Output at 0 x 0 left the Frame's output unknown and the Planner judged it by the profile's shape; now its Display's preferred mode answers. Cost: a Pi whose Weston runs another mode than both is still judged at the wrong size (a gap that exists today).
4. **No known screen, no shape, no photos.** The profile was the fallback shape; there is no fallback now. `fit_target` answers None, the Planner gives the Frame's slots no photo (the empty-pool rule: keep the last still, else the fallback), the rendition choice asks nothing for it, the fit analysis lists it under `unsized`, authoring a photo onto it is refused (`authored_frame_size_unknown`) and the source preview for it answers `frame_size_unknown`. Cost: such a Frame shows nothing, but it could not show anything anyway (its Pi reports no mode and its Display none).
5. **The Display's size is its EDID base block's** (bytes 21 and 22, whole centimetres, times 10 mm); detailed timing descriptors' millimetre sizes are ignored. Evidence (2026-10-19, read on the production Pi): the Samsung QN43Q8F states 95 x 54 cm in its base block (43 in across: right) and 1872 x 1053 mm in its preferred timing (85 in: wrong). Either byte 0 (a projector, or an aspect-ratio encoding) reads as no size.
6. **No size known: washes only, large for video, a dashed outline on the wall plan.** When the Display states no size, (a) the Player draws only wash lights on that Frame (they need no size); far and close lights and the mat shadow need millimetres and add none; (b) the large-Frame video rule treats the Frame as large, so the requirement "A 720p video must never be assigned to a 40-inch Frame" holds; (c) the wall plan draws the Frame as a dashed outline of `UNSIZED_FRAME_MM` (500 x 300 mm). Departure not taken: typing a Display's size on its Hardware tab (the owner said the size is "not typed again"; it is the first thing to add if a display he owns states none).
7. **The large-Frame setting stays in inches** (`large_frame_inches`, "From this diagonal up"), compared with the Frame's diagonal worked out from its physical size (`hypot(w, h) / 25.4`). It is a threshold the owner reads on the settings page, not a stored copy of the Frame's shape.
8. **"Shows videos" is a plain Frame setting**: `PUT /v1/operator/frames/{frame_id}/video`, last write wins, no generation, no configuration revision, no Position check (it changes planning, not the picture). It replaces the profile's `video`.
9. **Wall placement keeps `surface_id`, `x_mm`, `y_mm`** (the Frame's top-left on the wall). Its drawn size is `FrameInventory.size_mm`. Departure: the orientation-coherence rule (`_orientation_coherent`, `oriented_profile` 422) goes, having nothing left to compare.
10. **Position history**: an entry's box is its own shape, so Restore no longer refuses an entry "made for another profile" (`MADE_FOR_OTHER_PROFILE` goes, with `MadeFor.profile_px` and `position_entries.profile_*_px`). Old entries of kind `kept-profile-changed` stay readable; nothing writes the kind again. Restore still refuses an entry made for another turn or Display.
11. **The calibration board's aspect is the placement's pixels** (`picture_board(placement, ...)`), not the physical size. Cost: on a mode with non-square pixels the board's squares are drawn non-square; the owner's displays run native modes.
12. **The binding carries the Display's size** (`OutputBinding.display_mm`, as built), not the Frame's: a Position trial changes the box on the Pi, and the Player works the size out from the drawn box and the screen's turn with the same `frame_size_mm`. `profile` leaves the binding.
13. **Every stored copy migrates (as 075, 101, 102)**: 103 adds the Display's size; 104 converts every stored Position (Frames, Position history, both trial copies, Players' last configurations, plan offers); 105 adds `frames.video`; 106 moves stored bindings to the new binding form; 107 drops the Position history's profile columns; 108 drops `frames.profile`, `width_mm`, `height_mm`. Forward-only, no CHECK, trigger or function; take a `pg_dump` before deploying. The Pi keeps no configuration on disk (checked: `player/` persists none).
14. **Conversion is exact where the screen is known.** A rectangle becomes the box of its own pixels (`x / W, y / H, w / W, h / H` on the screen as it hangs), which `place_box` places on exactly the same pixels, so every Frame whose screen is known keeps its Frame output, its photos and its readiness. A stored rectangle not wholly on its screen is clipped onto it (not exact); with no known screen the box is the whole screen (not exact); a Frame not converted exactly is to check (`position_generation` NULL). Frames' calibration revision and configuration revision rise by one (as 102), so every Player is sent its box and a draft made before the deploy is refused (stale base). The alternative for no known screen (keep the width share, height from the profile's aspect) is not taken: it guesses a shape the owner said is wrong.
15. **Wire and node schema**: `OutputBinding` changes (fb-9) and the display line's schema major goes 9 to 10 (fb-2, `OutputReport.size_mm`): Central and the Pi move together on the release, as v0.41.0 did. Pi tested before merge.
16. **Transitional scaffolding inside the PR, gone at merge** (cut by green-alone, not a compatibility path): fb-5 writes `frames.video` and the profile's `video` together until fb-14 drops the profile; fb-6 keeps the existing Position editor working on boxes through an adapter in `useDraft.js` until fb-7 swaps in the box editor. fb-7's and fb-14's acceptance grep that each is gone, and the end review re-checks.
17. **Fifteen beads, the tracer sixth.** Package depth first: the shared primitives (fb-1), the Display's size (fb-2), the conversion migration (fb-3) and the console's box pieces (fb-4) are additive and land before the tracer so that it fits the 90-minute ceiling; the profile then leaves consumer by consumer (planner, binding and Player, analysis, Hardware tab, history, Frame API). About ten hours on the critical path: fb-2, fb-5, fb-6, fb-8, fb-9, fb-13, fb-14.

## New terms

| Term | Definition | Why needed | Existing term it could be |
|---|---|---|---|
| Box (the Position's form) | Left, top, width and height of the Frame's picture as fractions of its screen as it hangs | The owner's "box on the display" | Position (the console's word stays "Position"; "box" names its four values) |
| Display size | The Display's picture area in millimetres, from its EDID | Replaces `diagonal_inches` | none (the owner's "the Display's real size") |
| Shows videos | Whether the Planner may put a video on this Frame | The profile's `video`, moved to the Frame | Frame profile `video` |
| Screen size (`screen_px`) | The mode as built of the Output a Frame is bound to | The Frame output's input | "the size its Output last reported" (`display_px`) |

## Rules for every bead

- Read only this page's sections for your bead, its files and the code they name. Siblings may share one checkout: keep to your owned files; a derived output goes outside the repo (`$TMPDIR/frame-box-<bead>/`, pytest `--basetemp` there; `TMPDIR=/Volumes/Dock/tmp/agent-tmp`, `UV_CACHE_DIR=/Volumes/Dock/tmp/uv-cache`). A console bead rebuilds `dist/` or `storybook-static/` only when no other console bead runs; otherwise typecheck and lint with no emit.
- Catalog baselines: a bead that adds, changes or deletes a story owns its PNGs under `tests/browser/catalog-baselines/` and rewrites only them: `python3 scripts/catalog_baselines.py -k <story id>` (Docker; CI's `console-catalog` leg judges).
- Pinned runtimes: `.venv/bin/python` after `uv sync --frozen` (revert `uv.lock` if anything rewrites it); Node 22 (`/Users/matt/.nvm/versions/node/v22.23.2/bin`).
- Checks before landing: your changed packages' tests (`-n 4`), `.venv/bin/python -m ruff check .`, `.venv/bin/lint-imports`, and for console files `npm --prefix central/console run typecheck` and `run lint`. DB tests: `docker compose -f tests/integration/compose.test-database.yml up -d --wait`, then `.venv/bin/python scripts/test_local.py -q -m db -n 4 --dist loadgroup <your files>`. CI is the full gate; no local full verify.
- Land: `git add -A -- <owned> && git commit -m '<message>' -- <owned> && git push origin HEAD:feat/frame-box-position`, Conventional Commits, trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. A commit that meets a sibling's `index.lock` waits and retries. A rejected push: `git fetch origin && git rebase origin/feat/frame-box-position` (your own unpushed commit only), re-run your checks, push.
- Run your bead's mutation probe before landing: break the named line, see the named test go RED, restore, see it green.
- Migrations are forward-only; never edit 001-102. Migration numbers are fixed here: 103 fb-2, 104 fb-3, 105 fb-5, 106 fb-9, 107 fb-13, 108 fb-14.

## Frozen signatures by bead

### fb-1 Primitives: the box and its pixels (contracts)

Owned: `contracts/frame_fit.py`, `contracts/models.py`, `tests/test_frame_box.py` (new).

```python
# contracts/frame_fit.py (stdlib only; appliance never imports it)
ScreenMm = tuple[int, int]            # a Display's picture area in whole mm as built, each >= 1
BOX_SLACK: Final = 1e-9               # how far a box's right or bottom edge may pass 1 in floating point

@dataclass(frozen=True)
class FrameBox:
    left: float
    top: float
    width: float
    height: float
    def __post_init__(self) -> None:
        """ValueError("frame_box") unless each field is a finite int or float (never a bool),
        0 <= left, 0 <= top, 0 < width, 0 < height, left + width <= 1 + BOX_SLACK and
        top + height <= 1 + BOX_SLACK."""

@dataclass(frozen=True)
class Placement:                      # unchanged fields x, y, width, height
    @property
    def aspect(self) -> float: """width / height."""

def place_box(box: FrameBox, screen: OutputSize) -> Placement:
    """x = min(W - 1, floor(left * W + 0.5)); right = max(x + 1, min(W, floor((left + width) * W + 0.5)));
    width = right - x; y and height likewise with H. Total for every FrameBox on every valid screen.
    ValueError("placement_screen") unless both screen sides are ints (never bools) >= 1. Pure."""

def box_size_mm(box: FrameBox, screen_mm: ScreenMm) -> tuple[float, float]:
    """(box.width * screen_mm[0], box.height * screen_mm[1]); `screen_mm` already as it hangs.
    ValueError("screen_mm") unless both sides are ints (never bools) >= 1. Pure."""

# contracts/models.py
BoxSide = Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)]

class PositionBox(Model):
    mode: Literal["box"] = "box"
    left: Fraction = 0.0
    top: Fraction = 0.0
    width: BoxSide = 1.0
    height: BoxSide = 1.0
    @model_validator(mode="after")
    def boxed(self) -> Self: """self.box(); FrameBox's ValueError("frame_box") becomes the model's validation error."""
    def box(self) -> FrameBox: """The FrameBox of these four fields. Pure."""

def frame_size_mm(fit: PositionBox, display_mm: ScreenMm | None, turn: DisplayTurn) -> tuple[float, float] | None:
    """box_size_mm(fit.box(), hanging(display_mm, turn)); None when display_mm is None.
    ValueError as `hanging` for a known size. Pure."""
```

`FrameRectangle`, `place`, `RectangleFit`, `frame_output`, `frame_placement` and `off_screen` stay untouched here (fb-6 changes them). The stub commit leaves `PositionBox`'s validator out (a stub after-validator would return None); this bead adds it.

Acceptance (behaviour tests in `tests/test_frame_box.py`):
- Shared edges: on screens 1919, 1920 and 3840 wide, boxes `left=0, width=0.5` and `left=0.5, width=0.5` place with the first's `x + width` equal to the second's `x`; three thirds tile a 1920 screen with no gap or overlap.
- Totality: over a grid of boxes (left and width in steps of 0.05 and the extremes 0, 1e-6, 1) on screens (1, 1), (7, 3), (1920, 1080), (1080, 1920), (3840, 2160): `0 <= x`, `x + width <= W`, `width >= 1`, and the same down.
- Exact round trip (the migration's premise): for every whole-pixel rectangle `P` on a screen `S` of that grid, `place_box(FrameBox(P.x / W, P.y / H, P.width / W, P.height / H), S) == P`.
- `FrameBox` refuses a bool, NaN, a negative left, a zero width and `left + width = 1 + 1e-6`, and accepts `0.7 + 0.3`.
- `PositionBox()` is the whole screen; `PositionBox(left=0.6, width=0.5)` is a validation error; `frame_size_mm(PositionBox(width=0.5, height=0.25), (950, 540), 90)` is `(270.0, 237.5)`; with `None` it is `None`.
- `Placement(0, 0, 480, 540).aspect == 480 / 540`.

Probe: compute `width` as `max(1, floor(box.width * W + 0.5))` instead of from the two edges: the shared-edge test goes RED.

### fb-2 The Display's size and the screen-size rule (node, Central)

Owned: `contracts/node_output.py`, `appliance/display_host/edid.py`, `appliance/display_host/runner.py`, `appliance/display_host/output_power.py`, `central/displays/model.py` (`screen_px`), `central/displays/views.py`, `central/infra/display_store.py`, `central/installation_models.py`, `central/registry.py` (`inventory_in` and its SELECT only), `central/migrations/103_display_size.sql`, `tests/node/display/test_display_output_report.py`, every test that constructs an `OutputReport` (`git grep -l "OutputReport("`), `tests/test_display_size.py` (new, DB), `tests/test_migration_103_display_size.py` (new).

```python
# contracts/node_output.py
SCHEMA_MAJOR: Final = 10               # the display line's schema major; 10 from the frame box (the Display's size)
DISPLAY_MM_MAX: Final = 2550           # an EDID base block states at most 255 cm a side

@dataclass(frozen=True)
class OutputReport:                    # existing fields unchanged, in order, then:
    size_mm: tuple[int, int] | None    # the display's picture area as built, from its EDID base block; None: not stated
    # __post_init__ adds: size_mm is None or a tuple of two ints (never bools), each 1..DISPLAY_MM_MAX

# appliance/display_host/edid.py
@dataclass(frozen=True)
class EdidReading:
    identity: DisplayIdentity | None
    modes: tuple[DisplayMode, ...]
    size_mm: tuple[int, int] | None

def parse_edid(raw: bytes) -> EdidReading:
    """As today for identity and modes; size_mm from the base block's bytes 21 and 22 (cm) times 10
    when both are >= 1, else None; detailed timing descriptors' mm sizes are never read. A base
    block that fails its checks: EdidReading(None, (), None)."""

# central/displays/model.py (stub committed)
def screen_px(reported: OutputSize | None, modes: Sequence[DisplayMode]) -> OutputSize | None: ...

# central/displays/views.py
class DisplayView(Model):              # existing fields, then:
    size_mm: tuple[int, int] | None    # the Display's picture area as built (its EDID's); None: not stated

# central/installation_models.py
class FrameInventory(Model):           # existing fields, then:
    screen_px: tuple[Pixels, Pixels] | None    # screen_px of the bound Output (None: unbound or unknown)
    display_mm: tuple[int, int] | None         # the seen Display's size (None: none seen, or none stated)
```

Central: `display_store._recognise` and `_display_for(conn, key, identity, modes, size_mm, now)` keep the Display's `width_mm` and `height_mm` following the latest sighting that states a size (a report with `size_mm` None keeps the last known, as `modes` does). `frame_outputs_in` takes `display_px = screen_px(observation, modes of the seen Display)`. The display view reads the size.

Migration `103_display_size.sql`: `ALTER TABLE displays ADD COLUMN width_mm INTEGER NULL, ADD COLUMN height_mm INTEGER NULL;` and `UPDATE output_displays SET report = report || '{"size_mm": null}'::jsonb;` (every stored report reads as schema 10). Sizes fill as each Pi on the new release reports.

Acceptance:
- The QN43Q8F's EDID (base block bytes 21 and 22 = 95, 54; preferred detailed timing bytes 12-14 = 80, 29, 116, that is 1872 x 1053 mm) reads `size_mm == (950, 540)`; a base block with byte 21 or 22 zero reads `None`; the spike EDID keeps its identity and modes.
- A Pi's report on the display bus carries `size_mm`; Central's Display row holds 950 x 540 after it; a later report with `None` keeps it; `GET /v1/operator/frames/{id}/display` shows it.
- `screen_px((0, 0), [1080p preferred])` is (1920, 1080); `screen_px((3840, 2160), ...)` is (3840, 2160); no report and no preferred mode is None. A bound Frame whose Output enrolled at 0 x 0 gets its Frame output from its Display's preferred mode.
- The inventory carries `screen_px` and `display_mm` for a bound Frame, None for an unbound one.

Probe: make the size read the preferred detailed timing's millimetres: the QN43Q8F test reads (1872, 1053), RED.

### fb-3 Every stored Position becomes a box (migration 104)

Owned: `central/migrations/104_frame_box_positions.sql`, `tests/test_migration_104_frame_box_positions.py` (new), `tests/test_migration_102_rectangle_positions.py` (apply up to 102 only: `migrate_before(db, "103")` where it now calls `db.migrate()`), and any other `tests/test_migration_*.py` that calls `db.migrate()` and then reads a stored fit's JSON shape (find them with `-m db -k migration`).

JSON forms. Old (`RectangleFit`): `{"mode": "rectangle", "centre_x": cx, "centre_y": cy, "width": w}`, a missing key read as 0.5, 0.5, 1.0. New (`PositionBox`): `{"mode": "box", "left": l, "top": t, "width": bw, "height": bh}`. A copy already `"mode": "box"` is left as it is.

The conversion of one copy, given the screen's mode as built `(Wb, Hb)` (the `screen_px` rule, which 102 states in SQL: `outputs.observation` of the Frame's bound Output when at least 1 x 1, else the first preferred mode of `output_displays.report`'s modes there; unknown when unbound or neither), the turn it was made for (`position_entries.turn` for a history entry, `frames.turn` for every other copy) and the aspect `a` it was made at (a history entry's own `profile_width_px / profile_height_px`, every other copy its Frame's `profile` width_px / height_px):
- `(W, H)` = `(Wb, Hb)` hung by the turn (swapped at 90 and 270).
- Pixels as v0.41.0's `place`, half up `floor(v + 0.5)`: `pw = max(1, floor(w * W + 0.5))`, `ph = max(1, floor(pw / a + 0.5))`, `px = floor(cx * W - pw / 2 + 0.5)`, `py = floor(cy * H - ph / 2 + 0.5)`.
- On its screen (`0 <= px`, `px + pw <= W`, `0 <= py`, `py + ph <= H`): the box `(px / W, py / H, pw / W, ph / H)`, exact.
- Not on it: `pw' = least(pw, W)`, `ph' = least(ph, H)`, `px' = least(greatest(px, 0), W - pw')`, `py'` likewise; the box of those; not exact.
- Unknown screen: the whole screen `(0, 0, 1, 1)`; not exact.

Where each lands (one statement per table, as 102): trials in flight end (`state = 'ended'` where `'active'`), then both trial copies convert; history entries convert (revisions kept); Frames take the new box under `revision + 1`, `configuration_revision + 1`, `position_generation` kept when exact else NULL; each binding in `player_configurations.configuration->'bindings'` and `plan_offers.manifest->'bindings'` whose fit is not a box takes its Frame's converted calibration, or, its Frame gone, the whole-screen box under its own revision. No Position history entry records the conversion.

Acceptance (raw SQL on the schema `migrate_before(db, "104")` builds, then `migrate_before(db, "105")`):
- Exactness: for a grid of rectangles (the four turns, modes 1920 x 1080 and 3840 x 2160, aspects 16:9, 4:3, 9:16, 1:1, widths 0.25 to 1, centres inside) the converted box placed by `contracts.frame_fit.place_box` on the hung screen equals the pixels the test's own reference of v0.41.0's `place` gives (the test keeps its own copy of the old rule: fb-6 deletes `place`); those Frames keep `position_generation`.
- A rectangle not on its screen is clipped onto it and its Frame is to check; a Frame with no known screen gets the whole screen and is to check; a Frame whose Pi enrolled 0 x 0 converts by its Display's preferred mode.
- A history entry converts at its own aspect and turn; both trial copies convert and an active trial ends; a stored configuration's and an offer's bindings carry the Frame's new calibration; a binding of a deleted Frame gets the whole screen under its own revision.
- A box copy is untouched; running the conversion's statements on an already converted database changes nothing.

Probe: drop the `+ 0.5` from `pw`'s half-up: the exactness grid goes RED.

### fb-4 The console's box: its rule, its words and its editor pattern (additive)

Owned: `central/console/src/placement.js` (add `placeBox`, `boxSizeMm`; the rectangle functions stay until fb-7), `central/console/src/domain/position-box.ts`, `central/console/src/patterns/box-editor.tsx`, `central/console/src/patterns/box-editor.stories.tsx` (new), `tests/test_console_placement.py` (add box vectors), `tests/test_console_position_box.py` (new; runs `position-box.ts`'s pure functions under Node as `tests/test_console_flow.py` does), the new story's catalog baselines.

Before this bead builds, a UX designer agent proposes the editor's layout from `docs/design-language.md` and `.claude/skills/console-ux/SKILL.md` (the owner's standing rule: designers propose layouts before UI builds).

Signatures are the committed stubs: `placement.js` `placeBox(box, screen) -> Placement` (throws `Error("frame_box")` and `Error("placement_screen")` exactly where Python raises), `boxSizeMm(box, displayMm, turn) -> [number, number] | null`; `domain/position-box.ts` `Box`, `WHOLE_SCREEN`, `screenOf`, `boxPixels`, `boxFromPixels`, `movedBy`, `resizedBy`, `shapeWords`, `sizeWords`; `patterns/box-editor.tsx` `PixelRectangle`, `ScreenEdge`, `BoxHandle`, `BoxEditorProps`, `BoxEditor` (four edge handles and four corner handles, each with a 44-unit hit area; the box draggable; arrows report steps; the display's top edge thicker; Tab reaches the drawing and each handle). Nothing imports them yet; `PixelRectangle` and `ScreenEdge` are declared here and in `rectangle-editor.tsx` until fb-7 deletes the latter.

Acceptance:
- Parity: `placeBox` equals `place_box` and `boxSizeMm` equals `frame_size_mm` on fb-1's grids and on 2,000 seeded random boxes, refusals included.
- `resizedBy(box, "right", +10, 0, s)` keeps left, top and height and grows the width by 10 pixels; a corner changes both sides; no handle moves an edge past the screen or below 1 pixel; `movedBy` stops at the edges; `boxFromPixels` round-trips `boxPixels` on a grid.
- `shapeWords({x: 0, y: 0, width: 1920, height: 1080})` is "1,920 × 1,080 pixels, 16:9"; `sizeWords([950, 540])` names centimetres and the diagonal in inches; `sizeWords(null)` is null.
- The story renders in both schemes with axe clean.

Probe: in `resizedBy` move the left edge for a "right" drag: the resize test goes RED.

### fb-5 Shows videos is the Frame's own setting

Owned: `central/migrations/105_frame_video.sql`, `central/registry.py`, `central/app.py` (the route), `central/installation_models.py` (`FrameInventory.video`), `central/console_contract.py` (`FrameVideoChange` in `CONSOLE_BODIES`), `central/console/src/generated/contract.ts` (regenerated), `central/console/src/framesApi.js` (`setFrameVideo`), `central/console/src/domain/frame-profile.tsx` (the video checkbox leaves the profile editor; a "Shows videos" switch saves at once), `tests/test_frame_video.py` (new, DB), `tests/browser/test_operator_frame_profile_browser.py` (its video steps).

```python
# central/registry.py
class FrameVideoChange(Model):
    """PUT /v1/operator/frames/{frame_id}/video: whether the Frame shows videos (its own setting)."""
    video: StrictBool

class Registry:
    def set_frame_video(self, frame_id: str, video: bool) -> dict:
        """{"frame_id", "video", "changed"}. RegistryError("unknown_frame", 404). Last write wins: no
        generation, configuration revision or Position check; audits "frame_video_changed" {"video"}
        when it changes; the Planner plans with it from its next plan (wake it as a photo-fit
        settings save does). Until fb-14 also writes the profile JSON's "video" (transitional)."""
# create_frame and replace_frame_profile also write frames.video = profile.video (transitional, fb-14 deletes)

# central/app.py
@app.put("/v1/operator/frames/{frame_id}/video", dependencies=[Depends(admin)])
def set_frame_video(frame_id: Identifier, request: FrameVideoChange) -> dict: ...

# central/installation_models.py
class FrameInventory(Model):  # + video: bool
```

```js
// central/console/src/framesApi.js
/** @param {string} frameId @param {boolean} video */
export async function setFrameVideo(frameId, video)   // PUT .../video {video}; apiWrite's result
```

Migration `105_frame_video.sql`: `ALTER TABLE frames ADD COLUMN video BOOLEAN NOT NULL DEFAULT TRUE;` then `UPDATE frames SET video = COALESCE((profile->>'video')::boolean, TRUE);`

Acceptance: switching "Shows videos" off on a bound, ready Frame saves at once and keeps it ready (no generation change); the Planner's next plan puts no video on it while images still play; on, videos come back; an unknown Frame answers 404; a migrated Frame whose profile had `video` false has `video` false.

Probe: make `set_frame_video` skip the write when the Frame is bound: the bound-Frame test goes RED.

### fb-6 TRACER: the Position is a box, end to end

Owned: `contracts/models.py`, `contracts/frame_fit.py`, `central/registry.py` (`commit_calibration_trial_in`), `central/fleet/models.py`, `central/fleet/position_history.py` (the fit type only), `central/fleet/position_views.py` (the fit type only), `central/infra/position_history_store.py` (the fit type only), `central/infra/display_store.py` (`frame_outputs_in`'s call), `central/wall_plan/fit_analysis.py` (the draft's `frame_output` call), `central/installation_models.py` (docstrings), `player/plane_renderer.py`, `scripts/demo_wall.py` (its Position), `scripts/render_bench/timeline.py` (its Position), `central/console/src/useDraft.js`, `central/console/src/liveAdjustment.js`, `central/console/src/trialBodies.ts`, `central/console/src/domain/trial-refusals.ts`, `central/console/src/domain/position-history.tsx`, `central/console/src/generated/contract.ts`, and every test that builds or reads a `RectangleFit`, `FrameRectangle`, `place`, `off_screen` or `position_off_screen` (`tests/test_rectangle_fit.py` is deleted; `tests/test_plane_renderer.py`, `tests/test_plane_screen.py`, `tests/test_node_calibration.py`, `tests/test_position_history.py`, `tests/test_wall_plan_tracer.py`, `tests/test_frame_fit_settings.py`, `tests/test_source_preview_matches_plan.py`, `tests/test_print_look_contract.py`, `tests/native_picture_e2e_harness.py`, and the browser tests that assert a fit's JSON).

```python
# contracts/models.py
class Calibration(Model):
    revision: int = Field(default=1, ge=1)
    fit: PositionBox = Field(default_factory=PositionBox)

def frame_placement(fit: PositionBox, output: OutputSize, turn: DisplayTurn) -> Placement:
    """place_box(fit.box(), hanging(output, turn)). ValueError as hanging ("display_turn",
    "screen_size"). The one home, for Central, of a Frame's box on its screen. Pure."""

def frame_output(calibration: Calibration, output: OutputSize | None, turn: DisplayTurn) -> PictureSize | None:
    """frame_placement's (width, height); None exactly when output is None or a side is below 1. Pure."""

# deleted: RectangleWidth, RectangleFit, off_screen (contracts/models.py); FrameRectangle, place (contracts/frame_fit.py)
# FrameProfile and OutputBinding.profile stay (later beads remove them); after this bead only the
# Planner's FitTarget reads the profile's width and height (fb-8 ends that)

# central/fleet/models.py: TrialRefusal.POSITION_OFF_SCREEN and its status row deleted
# central/registry.py commit_calibration_trial_in: same signature; no output-size read, no off-screen check
# central/fleet/position_history.py: PositionValues.fit: PositionBox
# central/fleet/position_views.py: PositionEntryView.fit: PositionBox

# player/plane_renderer.py
def _place(self, binding: OutputBinding, drawn: _Drawn) -> tuple[Screen, Placement] | Literal["gone"]:
    """The Frame's screen and place_box(drawn.calibration.fit.box(), screen.size); "gone" without the
    screen. Never "failed": a box always places. Wherever the renderer used the profile's aspect it
    uses placement.aspect."""
```

Console (no new UI): the wire fit is `{mode: "box", left, top, width, height}` (`useDraft.js` `Fit`, `fitOf`, the canonical draft key; `liveAdjustment.js` reads and sends boxes; `trialBodies.ts` types follow the regenerated contract). The existing rectangle editor keeps working through one adapter in `useDraft.js`: it reads a rectangle view of the box (`centre_x = left + width / 2`, `centre_y = top + height / 2`, `width`) at the box's own pixel aspect and turns each rectangle patch back into a box of that aspect (fb-7 deletes the adapter). `trial-refusals.ts` loses `position_off_screen`. `position-history.tsx` says an entry's box with `position-box.ts` (left and top, width and height, in pixels of the screen when known, else as shares).

Acceptance:
- A Position trial sends a box 0.25 wide and 0.5 high on a 1920 x 1080 screen; Save keeps it; the Frame's Frame output is (480, 540), not 16:9; the Player's plane for it is placed at exactly `place_box`'s pixels (renderer test with a fake screen) and its pictures are finished at that aspect.
- A box on a screen hung at 90 with a 3840 x 2160 mode places on the 2160 x 3840 screen as it hangs.
- Save accepts any valid box (nothing is off its screen); `position_off_screen` and `RectangleFit` exist nowhere in the code (`git grep` in the bead's test of the refusal vocabulary).
- The console's Position tab still moves and resizes a picture and saves it (browser: the existing Position test, adapted).

Probes: (1) give `frame_output` the profile's aspect for the height: the 480 x 540 test goes RED. (2) In `_place` take the height from the width and the profile's aspect: the renderer placement test goes RED.

### fb-7 The console's Position editor on the box

Owned: `central/console/src/domain/frame-position.tsx` and its stories, `central/console/src/pages/frame-page.tsx` (the Position tab's screen and box: `FrameInventory.screen_px`, `display_mm` and `turn`; no profile for the Position), `central/console/src/useDraft.js` (the adapter goes: patches are box fields), `central/console/src/liveAdjustment.js` (rectangle words, if any), `central/console/src/placement.js` (delete `place`, `pixels`, `fillScreen`; keep `hanging`, `placeBox`, `boxSizeMm`), `central/console/src/domain/position-on-screen.ts` (deleted; callers use `position-box.ts`), `central/console/src/patterns/rectangle-editor.tsx` and its stories (deleted), `tests/test_console_placement.py` (the rectangle vectors go), `tests/browser/test_frame_fit_browser.py`, `tests/browser/test_console_frame_page_browser.py` (Position parts), `tests/browser/test_frame_turn_browser.py` (Position parts), the changed and deleted stories' baselines.

No new signatures beyond fb-4's. The Position tab: the screen as it hangs with the box (`BoxEditor`); typed Left, Top, Width and Height in whole pixels of the screen; nudges move by 1, 10 or 50 pixels; "Fill the screen" sets `WHOLE_SCREEN`; live facts under the drawing: the Frame's pixels and shape (`shapeWords`) and, when the Display states a size, its physical size (`sizeWords` of `boxSizeMm`); with no known screen size, the drawing is a 16:9 stand-in and a note says the values are shares of the screen, not pixels. Nothing refuses a box for running off the screen (it cannot).

Acceptance (browser): dragging the right edge widens only the width; dragging the bottom-left corner changes left and height and keeps right and top; typing Width 800 and Height 800 on a 1920 x 1080 screen saves a square Frame whose shape reads "800 × 800 pixels, 1:1"; Fill the screen saves the whole screen; `git grep -n "centre_x\|RectangleEditor\|position-on-screen\|fillScreen" central/console/src` finds nothing.

Probe: wire `onResize`'s vertical part to nothing: the corner test goes RED.

### fb-8 The Planner judges by the Frame output

Owned: `central/wall_plan/fit.py`, `central/wall_plan/assignment.py`, `central/wall_plan/fit_analysis.py` (building `FitTarget` only), `central/photo_library/rendition.py`, `central/photo_library/views.py` (`FrameCopy`), `central/infra/photo_library_store.py` (`_frame_copy`), `central/planner.py`, `central/coordination.py`, `central/app.py` (the source-candidates route), `central/installation_models.py` (`FrameOutput`), `central/infra/display_store.py` (`frame_outputs_in`), `central/console/src/domain/photo-fit.ts` (the `output_known` lines), and the tests that build a `FitTarget`, `FrameOutput`, `Slot` or `FrameCopy` (`tests/test_photo_fit.py`, `tests/test_planner*.py`, `tests/test_planner_wall_assignment.py`, `tests/test_rendition_choice.py`, `tests/test_wall_copy_sizing.py`, `tests/test_authored_compatibility.py`, `tests/test_authored_scene.py`, `tests/test_wall_plan_tracer.py`, `tests/test_fit_analysis.py` and `_budget.py`, `tests/test_frame_output*.py`, `tests/test_source_preview_matches_plan.py`, `tests/test_readable_size.py`, `tests/test_photo_library_routes.py`, and the browser tests whose seeds expected photos on a Frame with no known screen).

```python
# central/installation_models.py
PositiveMm = Annotated[float, Field(gt=0, allow_inf_nan=False)]
class FrameOutput(Model):
    frame_id: Identifier
    turn: ExactTurn
    video: bool                                       # frames.video
    display_px: tuple[Pixels, Pixels] | None          # screen_px
    output_px: tuple[Pixels, Pixels] | None           # frame_output(calibration, display_px, turn)
    size_mm: tuple[PositiveMm, PositiveMm] | None     # frame_size_mm(fit, the seen Display's size, turn)
    # profile: deleted

# central/wall_plan/fit.py
class FitTarget(Model):                               # hashable: the rule's memo key
    output_px: tuple[Pixels, Pixels]
    video: bool
    size_mm: tuple[PositiveMm, PositiveMm] | None
    @property
    def aspect(self) -> float: """output_px[0] / output_px[1]: the Frame's shape (Placement.aspect)."""

def fit_target(frame: FrameOutput) -> FitTarget | None:
    """The FitTarget of `frame`; None when its output_px is None (no known screen: no shape). The
    one place a Frame becomes what the fit rule judges: the Planner, authoring, the source preview,
    the analysis and the rendition choice ask here. Pure."""

def large_frame(target: FitTarget, settings: FitSettings) -> bool:
    """True when size_mm is None (an unknown size counts as large, so the requirement "A 720p video
    must never be assigned to a 40-inch Frame" holds) or hypot(*size_mm) / 25.4 >=
    settings.large_frame_inches. The one home of "a large Frame". Pure."""

def crop_share(width: int, height: int, aspect: float) -> float
def shape_fit(kind: MediaKind, width: int, height: int, target: FitTarget, crop_limit: float,
              settings: FitSettings, *, size_known: bool) -> DoesNotFit | None
    """As today; `size_known` False (an image whose readable size is not measured yet) skips an
    image's two size tests, which an unknown Frame output used to skip."""

# central/wall_plan/assignment.py
class Slot(Model):          # target: FitTarget | None (None: its Frame has no Frame output; the slot takes no candidate)
def variant_usable(candidate: Candidate, target: FitTarget, settings: FitSettings) -> bool  # the video floor asks large_frame

# central/photo_library/rendition.py
def frame_need(frame: FrameOutput) -> FrameNeed | None                         # None without a Frame output
def rendition_needs(frames: Sequence[FrameOutput]) -> tuple[FrameNeed, ...]    # skips the None ones

# central/photo_library/views.py
class FrameCopy(Model):     # frame_id, need_px, preview_holds; output_known deleted (a Frame without output has no FrameCopy)

# central/planner.py: frame_outputs: Mapping[str, FrameOutput]; a slot's target is fit_target of its Frame's output, else None
# central/coordination.py: authored validation targets via frame_outputs_in and fit_target; a Frame with authored refs and no
#   Frame output: RegistryError("authored_frame_size_unknown", 409); it no longer calls installation.frame_profiles_in
# central/app.py source candidates with frame_id: target = fit_target(output); no Frame output: RegistryError("frame_size_unknown", 409)
# central/infra/display_store.py frame_outputs_in: video = frames.video; size_mm from the seen Display's width_mm, height_mm
```

Acceptance:
- A box 0.25 x 0.5 on a 1920 x 1080 Frame refuses a 16:9 photo as `beyond_crop_limit` at a 30 % crop limit and accepts a 9:10 one: the shape is the box's, not the profile's.
- A 720p video is refused on a Frame whose Display states 950 x 540 mm and whose box is the whole screen (43 in, with `large_frame_inches` 40) and accepted at box width 0.5 (a smaller diagonal); on a Frame whose Display states no size it is refused.
- A Frame with "Shows videos" off gets no video.
- A bound Frame whose screen is not known gets no photo and its slot keeps the last still (else the fallback); no rendition need is made for it; authoring a photo onto it is refused `authored_frame_size_unknown`.
- A copy for a Frame is sized to its Frame output; the photo sheet lists no Frame without one.

Probes: (1) make `fit_target` take a 16:9 shape when `output_px` is None instead of answering None: the no-screen test goes RED. (2) In `large_frame` answer False for an unknown size: the unknown-size video test goes RED.

### fb-9 The binding carries the Display's size; the Player draws by it

Owned: `contracts/models.py` (`OutputBinding`, `DisplayMm`), `central/installation_repository.py`, `central/registry.py` (`configuration_in`, `configuration_for` only), `central/readiness_diagnostics.py`, `central/infra/display_store.py` (a seen-Display size column constant), `player/geometry.py`, `player/plane_renderer.py`, `player/executor.py`, `player/look_shader.py`, `player/fit_pass.py` and `player/pattern_pass.py` (only where they pass the board or the size), `central/migrations/106_binding_display_size.sql`, `scripts/render_bench/timeline.py`, `tests/native_fit_pass_harness.py`, `tests/native_picture_e2e_harness.py`, `tests/native_screen_harness.py`, and every test that builds an `OutputBinding` or calls `picture_lights` or `picture_board` (`git grep -l "OutputBinding(\|picture_lights\|picture_board" tests`: `test_room_light_physics.py`, `test_player_board_reference.py`, `test_player_target_reference.py`, `test_print_look_reference.py`, `test_fit_pass_reference.py`, `test_pattern_pass_reference.py`, `test_player_trial_lighting.py`, `test_executor.py`, `test_contracts.py`, `test_coordination_stored_configuration.py`, `test_central_session.py`, `node/display/test_display_picture_controls.py`, and the rest the grep lists).

```python
# contracts/models.py
DisplayMm = tuple[Annotated[int, Field(ge=1, le=DISPLAY_MM_MAX, strict=True)],
                  Annotated[int, Field(ge=1, le=DISPLAY_MM_MAX, strict=True)]]   # DISPLAY_MM_MAX from contracts/node_output.py
class OutputBinding(Model):
    output_id: Identifier
    frame_id: Identifier
    generation: int = Field(ge=1)
    configuration_revision: int = Field(default=1, ge=1)
    calibration: Calibration = Field(default_factory=Calibration)
    lighting: FrameLighting
    display_mm: DisplayMm | None      # required: the Display seen on this Output, its picture area as built; None: unknown
    # profile: deleted

# central/installation_repository.py
def output_binding(row: Mapping[str, Any]) -> OutputBinding:
    """The one mapping of a frames row joined with its binding and its seen Display's size to an
    OutputBinding; InstallationRepository.configuration_in uses it and Registry.configuration_in
    delegates to InstallationRepository.configuration_in (one copy of the query and the mapping)."""
# central/readiness_diagnostics.py: _snapshot_bindings builds from FrameInventory with display_mm=frame.display_mm

# player/geometry.py
@dataclass(frozen=True)
class FrameLights:
    size_mm: tuple[float, float] | None   # None: the Display states no size; then every light in `lights` is a wash
    depth_mm: float
    paper: Paper
    lights: tuple[LitLight, ...]
    peak: float
    # __post_init__: ValueError("frame_lights") when size_mm is None and a light is not a wash

def picture_lights(lighting: FrameLighting, document: RoomLightsDocument | None,
                   size_mm: tuple[float, float] | None, display: DisplayLight) -> FrameLights:
    """As today at the Frame's physical size `size_mm` (contracts/models.py `frame_size_mm` of the
    drawn box, the binding's display_mm and the screen's turn); with size_mm None only wash lights
    add light (far and close lights and the mat shadow need millimetres). Pure."""

def picture_board(placement: Placement, modules: QrModules | None) -> BoardDraw:
    """aspect max(w, h) / min(w, h) of the placement's pixels, portrait when w < h. Pure."""
# deleted: _profile_size_mm
# player/plane_renderer.py: size_mm = frame_size_mm(drawn.calibration.fit, binding.display_mm, screen.turn)
# player/look_shader.py: the mat_size uniform is (1.0, 1.0) when lights is None or lights.size_mm is None
# player/executor.py: the retained-binding comparison compares display_mm where it compared profile
```

Migration `106_binding_display_size.sql`: each binding object in `player_configurations.configuration->'bindings'` and `plan_offers.manifest->'bindings'` becomes `(binding - 'profile') || '{"display_mm": null}'` (Central's next configuration fills the size; its revision rises then because the content differs).

Acceptance:
- A Frame on a Display stating 950 x 540 mm with the whole-screen box draws a far light's mat shadow at the width a 950 mm picture gives (scalar reference against `irradiance`); at box width 0.5 the same mat is twice the share of the picture.
- With `display_mm` None, wash lights still add light and far and close lights add none; building `FrameLights(size_mm=None, lights=(a close light,))` raises.
- The board's aspect on a 480 x 540 placement is 1.125, portrait.
- A Player is sent `display_mm` once its Display reports a size (configuration revision rises); `git grep -n "\.profile\b" player central/installation_repository.py central/readiness_diagnostics.py` finds no Frame profile.

Probe: make `picture_lights` drop the wash lights too when the size is None: the unknown-size wash test goes RED.

### fb-10 The fit analysis without the profile

Owned: `central/wall_plan/fit_analysis.py`, `central/infra/fit_analysis_store.py` (if it builds drafts or Frame fits), `central/console/src/domain/photo-fit.ts`, `central/console/src/domain/photo-fit-board.tsx`, `central/console/src/pages/frame-page.tsx` (the draft it sends; `wholeProfile`, `aspectOf` and the profile type go), `central/console/src/pages/photo-fit-settings-page.tsx` (the large-Frame help words: "From this diagonal up, worked out from the display's size and the Position"), `tests/test_fit_analysis.py`, `tests/test_fit_analysis_budget.py`, `tests/test_fit_analysis_store.py`, `tests/browser/test_frame_photo_fit_browser.py`, the photo-fit stories' baselines.

```python
class FrameDraft(Model):
    frame_id: Identifier
    calibration: Calibration          # profile: deleted

class FrameFit(Model):                # as today, except
    output_px: tuple[int, int]        # required: an unsized Frame has no FrameFit

class FitAnalysis(Model):             # as today, then:
    unsized: tuple[Identifier, ...] = Field(default=(), max_length=64)   # judged Frames with no Frame output: not judged
```

A draft is judged at `fit_target` of its saved Frame output with `output_px = frame_output(draft.calibration, display_px, turn)` and `size_mm = frame_size_mm(draft.calibration.fit, the Display's size, turn)`. The board shows each unsized Frame as one line: "Its screen size is not known yet, so no photo is judged for it."

Acceptance: a draft box 0.25 x 0.5 is judged at 480 x 540; a Frame with no known screen appears in `unsized` and on the board's line, and nowhere in `frames`; the console sends drafts with no profile.

Probe: judge a draft at the saved Position's output instead of the draft's: the draft test goes RED.

### fb-11 The Hardware tab without the Frame profile (console)

Owned: `central/console/src/domain/frame-profile.tsx` (file and export name `FrameProfile` kept until fb-14; its content changes), `central/console/src/domain/frame-display.tsx`, `central/console/src/framesApi.js` (`updateFrameProfile` deleted), `tests/browser/test_operator_frame_profile_browser.py`, `tests/browser/test_frame_display_changed_browser.py`, their stories' baselines.

The section becomes "Frame": the "Shows videos" switch (fb-5), the Frame's size in pixels and its shape (`placeBox` of its Position on `screen_px` at its turn, `shapeWords`), its physical size (`sizeWords`) or "This display does not state its size: only even room light is drawn on this Frame", and "Change its size and place on the Position tab" with the link. No pixel or diagonal field. The Display section adds the Display's size ("95 × 54 cm, 43 in across") or "Does not state its size". The display-changed card loses step 1 (Confirm Frame profile): it says to check the Position on the new display.

Acceptance (browser): the Hardware tab of a Frame on a 950 x 540 mm Display with the whole-screen box reads "1,920 × 1,080 pixels, 16:9" and "95 × 54 cm"; no input for pixels or diagonal exists; the display-changed card offers only the Position check; `git grep -n "updateFrameProfile\|/profile\`" central/console/src` finds nothing.

Probe: show the box's pixels without the turn: the turned-Frame facts test goes RED.

### fb-12 One seed for a Frame in tests

Owned: `tests/support/frames.py` (new) and the test files that build a `FrameCreate` or post a Frame body to `/v1/operator/frames`, EXCEPT the files fb-13 owns and the tests whose subject is the profile, the wall size or the orientation rule (they change in fb-14: `tests/test_operator_frames.py`'s profile and orientation cases, `tests/test_registry.py`'s `oriented_profile` cases, `tests/browser/test_operator_frame_profile_browser.py`, `tests/browser/test_frame_display_changed_browser.py`, `tests/browser/test_operator_wall_browser.py`).

```python
# tests/support/frames.py
def frame_create(frame_id: str, *, name: str | None = None, surface_id: str = "wall",
                 x_mm: float = 0, y_mm: float = 0, video: bool = True) -> FrameCreate:
    """The one place a test builds a FrameCreate. Until fb-14 it fills the profile (1920 x 1080,
    24 in, `video`) and a 400 x 225 mm wall size, which nothing reads for a shape any more."""

def frame_body(frame_id: str, *, name: str | None = None, surface_id: str = "wall",
               x_mm: float = 0, y_mm: float = 0, video: bool = True) -> dict[str, object]:
    """frame_create(...).model_dump(mode="json"): the body a test posts."""
```

A test that needs a portrait or square Frame gets it from its Position box (or a turn) after binding, never from the seed. Behaviour-preserving: no assertion changes.

Acceptance: `git grep -n "FrameProfile(\|width_mm=\|diagonal_inches" tests` lists only fb-13's files and the exceptions above; the changed files' tests pass.

Probe: make `frame_create` ignore `video`: a no-video planning test goes RED.

### fb-13 Position history without the profile; the profile route goes

Owned: `central/fleet/position_history.py`, `central/fleet/position_views.py`, `central/infra/position_history_store.py`, `central/registry.py` (`replace_frame_profile` and `FrameProfileReplacement` deleted), `central/app.py` (the `/profile` route deleted), `central/migrations/107_position_history_without_profile.sql`, `central/console/src/domain/position-history.tsx`, `central/console/src/domain/trial-refusals.ts`, `central/console/src/generated/contract.ts`, `tests/test_position_history.py`, `tests/test_position_history_store.py`, `tests/test_tuning_history*.py`, `tests/test_tuning_snapshot.py`, `tests/test_operator_frames.py` (the profile-route cases only), and the browser test of the Position history.

```python
@dataclass(frozen=True)
class MadeFor:
    turn: int
    display_id: UUID | None           # profile_px: deleted

class PositionRestoreRefusal(StrEnum):
    OTHER_FRAME = "other_frame"
    MADE_FOR_OTHER_TURN = "made_for_other_turn"
    MADE_FOR_OTHER_DISPLAY = "made_for_other_display"   # MADE_FOR_OTHER_PROFILE: deleted

def restore_refusal(entry: PositionEntry, *, frame_id: str, now: MadeFor) -> PositionRestoreRefusal | None
    """OTHER_FRAME, then MADE_FOR_OTHER_TURN, then MADE_FOR_OTHER_DISPLAY (both known and different)."""
# deleted: profile_changed_record; write_position_in's `profile` parameter; ProfilePxView; MadeForView.profile_px
# PositionEntryKind.KEPT_PROFILE_CHANGED stays (stored entries keep it); nothing writes it
```

Migration `107_position_history_without_profile.sql`: `ALTER TABLE position_entries DROP COLUMN profile_width_px, DROP COLUMN profile_height_px;`

Acceptance: an entry kept before a turn is refused `made_for_other_turn`; an old entry with a different box restores (its box is its shape); `/v1/operator/frames/{id}/profile` answers 404 or 405 (no route); a stored `kept-profile-changed` entry still lists with its words.

Probe: keep the deleted profile comparison as an always-true refusal: the old-box restore test goes RED.

### fb-14 A Frame without profile or wall size

Owned: `contracts/models.py` (`FrameProfile` deleted, and `Positive` if nothing else uses it), `central/registry.py`, `central/app.py`, `central/installation_models.py` (`FrameInventory`), `central/installation_ports.py` and `central/installation_repository.py` (`frame_profiles_in` deleted), `central/infra/display_store.py` (any profile read left), `central/console_contract.py` (if it names the profile), `central/migrations/108_frame_without_profile.sql`, `scripts/demo_wall.py`, `tests/support/frames.py` (its internals), fb-12's exception tests, `central/console/src/Plan.jsx`, `central/console/src/projection.js`, `central/console/src/framesApi.js` (`createFrame`, `placeFrame`), `central/console/src/domain/frame-profile.tsx` renamed `central/console/src/domain/frame-facts.tsx` (export `FrameFacts`), `central/console/src/pages/frame-page.tsx` (its import and the profile type), `tests/browser/test_operator_wall_browser.py` and the wall-plan browser tests, their baselines.

```python
# central/registry.py
class FrameCreate(Model):
    id: TargetIdentifier
    name: Name | None = None
    surface_id: Identifier = "wall"
    x_mm: float = 0                   # the Frame's top-left on the wall
    y_mm: float = 0
    video: StrictBool = True

class FramePlacement(Model):
    surface_id: Identifier | None = None
    x_mm: float | None = None
    y_mm: float | None = None

# Registry.create_frame(frame: FrameCreate) -> dict   (writes frames.video; no profile, no wall size)
# Registry.place_frame(frame_id: str, placement: FramePlacement) -> dict   {"id", "surface_id", "x_mm", "y_mm"}
# deleted: _orientation_coherent, the oriented_profile refusals, Registry.frame_profile, frame_profiles_in
#          (Registry, InstallationPort, InstallationRepository), fb-5's dual writes to the profile JSON

# central/installation_models.py
class FrameInventory(Model):
    id: Identifier
    name: str
    surface_id: Identifier
    x_mm: float
    y_mm: float
    video: bool
    generation: int = Field(ge=0)
    calibration: Calibration
    lighting: FrameLighting
    readiness: Readiness
    turn: ExactTurn
    configuration_revision: int = Field(ge=1)
    player_id: Identifier | None = None
    output_id: Identifier | None = None
    screen_px: tuple[Pixels, Pixels] | None
    display_mm: tuple[int, int] | None
    size_mm: tuple[PositiveMm, PositiveMm] | None   # frame_size_mm(calibration.fit, display_mm, turn): the wall plan draws it
```

```js
// central/console/src/framesApi.js
export async function createFrame(id, placement, video)   // placement {surface_id, x_mm, y_mm}
export async function placeFrame(frameId, placement)      // {surface_id, x_mm, y_mm}
// central/console/src/projection.js
export const UNSIZED_FRAME_MM = [500, 300];               // the dashed outline of a Frame whose size is not known
// orientationCoherent: deleted
```

Migration `108_frame_without_profile.sql`: `ALTER TABLE frames DROP COLUMN profile, DROP COLUMN width_mm, DROP COLUMN height_mm;`

Console: placing a Frame on the wall plan asks its id or name, where it hangs (a click or a drag places its top-left) and "Shows videos"; moving it moves only its top-left; each Frame is drawn at `size_mm`, or as a dashed `UNSIZED_FRAME_MM` outline labelled "size not known yet".

Acceptance: creating a Frame with only an id and a place works and starts with the whole-screen box and "Shows videos" on; a body with `profile`, `width_mm` or `diagonal_inches` is refused 422 (extra field); the wall plan draws a bound Frame on a 950 x 540 mm Display at 950 x 540 and an unbound one as the dashed outline; `git grep -n -E "FrameProfile|diagonal_inches|_orientation_coherent|orientationCoherent" -- '*.py' central/console/src scripts` finds nothing; `git grep -n "profile->>'video'" central` finds nothing.

Probe: make `inventory_in` answer `size_mm` without the turn: the turned Frame's wall-plan size test goes RED.

### fb-15 Docs

Owned: `docs/decisions/0030-frame-box.md` (new: the decisions above, with the owner's words), `docs/decisions/0029-render-planes.md` (Design rule 2 and the Costs line point to 0030), `docs/operator-console-design.md` (§5 Position, the Hardware tab's Frame facts and Shows videos, row 108), `docs/module-planner.md` (the photo fit by the Frame output; no output, no photos; a large Frame by the Display's size), `docs/runbook.md` (every Frame profile mention; the Display's size; deploying 103-108: `pg_dump` first, the Pi release), `docs/player-architecture.md` (the lights' size from the Display), `docs/architecture.md`, `docs/operator-console-ddd.md`, `docs/design-language.md` (profile mentions), `docs/decisions/0023-print-look.md` and `docs/decisions/0025-configuration-snapshots.md` (a line each pointing to 0030 where they name the profile), `docs/roadmap.md` (Developed column), `README.md`, `AGENTS.md` (the code map's `contracts/frame_fit.py`, `central/displays/` and `central/wall_plan/` rows), `.claude/skills/display-control/SKILL.md` (reading a display's EDID size: the base block, never the detailed timing; the QN43Q8F's two answers). `python3 scripts/check_docs.py` passes. A code bead is never failed on a docs finding.

## Shared primitives

| Primitive | Responsibility | Home | Consumers now | Scheduled |
|---|---|---|---|---|
| `FrameBox`, `place_box` | A box's validity and its pixels on a screen | `contracts/frame_fit.py` | none (fb-1) | Central `frame_placement` and the Player `_place` (fb-6), migration 104's test (fb-3), the console mirror (fb-4) |
| `Placement.aspect` | A Frame's shape as a number | `contracts/frame_fit.py` | none | the Player (fb-6); `FitTarget.aspect` reads the same sides (fb-8) |
| `box_size_mm`, `frame_size_mm` | A Frame's physical size from the Display's | `contracts/frame_fit.py`, `contracts/models.py` | none | `frame_outputs_in` and the inventory (fb-8, fb-14), the Player's lights (fb-9), the console mirror `boxSizeMm` (fb-4) |
| `PositionBox` | The Position's wire and stored form | `contracts/models.py` | none | `Calibration.fit` (fb-6) |
| `screen_px` | A Frame's screen mode as built | `central/displays/model.py` | `frame_outputs_in`, the inventory (fb-2) | migration 104 states it in SQL (fb-3) |
| `fit_target`, `large_frame` | A Frame as the fit rule judges it | `central/wall_plan/fit.py` | the Planner, authoring, preview, rendition (fb-8) | the analysis (fb-10) |
| `output_binding` | One row-to-binding mapping | `central/installation_repository.py` | both `configuration_in`s (fb-9) | none |
| `position-box.ts`, `placeBox` | The console's box numbers and words | console | none (fb-4) | the Position tab and History (fb-6, fb-7), the Hardware facts (fb-11) |
| `frame_create`, `frame_body` | One test seed for a Frame | `tests/support/frames.py` | fb-12 | fb-14 changes only its internals |

## Test architecture

- **Pure contracts** (`tests/test_frame_box.py`): grids and properties on `FrameBox`, `place_box`, `box_size_mm`, `frame_size_mm`; no fixtures.
- **Conformance boundaries**: (1) the console mirror against Python (`tests/test_console_placement.py` under Node: `placeBox` = `place_box`, `boxSizeMm` = `frame_size_mm`); (2) migration 104's SQL against `place_box` (the exactness grid); (3) `screen_px` against migration 104's screen choice (the 0 x 0 enrollment case in both tests).
- **Node**: EDID bytes in, `EdidReading` out, with the production display's real bytes; Display Host's report through `BusReportSink` as today.
- **Central**: pure fit-rule tests build `FitTarget` and `FrameOutput` directly (no database); stores and routes through the DB tier; migrations on raw SQL by `migrate_before`.
- **Player**: the renderer with a fake screen for placement; the scalar reference (`player/geometry.py`) for lights, mat and board, which the shaders are held to (the native harnesses, Linux CI).
- **Console**: pure modules under Node; the catalog for `BoxEditor`; browser tests for the Position tab, the Hardware tab and the wall plan.
- **Bench (before merge; the agent drives the test Pi over SSH)**: Display Host reports 950 x 540 mm for the Samsung; a non-16:9 box (for example 0.6 x 0.9) shows on the Pi exactly where the console draws it; the room lights' mat shadow looks right at the real size; the Frame stays ready through the deploy (its conversion is exact).

## Maintenance risks

- **The console's copy of `place_box`** is a convention held by a parity test, as `place` was: kept because a drag asks no round trip. Central's Save no longer needs to check anything a box can break.
- **Migrations restate rules in SQL** (102 and 104: the placement and the screen choice). Tested against the Python; a later change to `place_box` does not touch them (forward-only), which is right: they describe the data at their time.
- **Transitional scaffolding** (decision 16) must be gone at merge; fb-7's and fb-14's acceptance grep for it, and the end review re-checks.
- **The screen size Central uses can differ from the mode Weston runs** (a stale enrollment report): Central then sizes photos for one size and the Player places another. Pre-existing; the box makes it no worse; the fix is the Player reporting its running mode (not in scope).
- **EDID sizes lie**: the base block is read, the timing descriptors never; a display stating nothing gets washes only. If the owner meets one, a typed Display size is the follow-up (one field on the Display, never on the Frame).
- **Test churn**: about 80 test files build a profile or a wall size; fb-12's helper makes the last change (fb-14) one file. Migration tests that call `db.migrate()` and then read a shape must stop before the migrations they do not test (fb-3).
- **Three binding builders** (`Registry.configuration_in`, `InstallationRepository.configuration_in`, `readiness_diagnostics._snapshot_bindings`) are today two copies of one query and a third mapping; fb-9 leaves one query and one row mapping.
- **The critical path is long** (about ten hours of serial beads); the parallel groups below are the most the file ownership allows.

## Parallel groups (each waits for the one before)

1. fb-1, fb-2
2. fb-3, fb-4, fb-5 (fb-3 and fb-4 need fb-1; fb-5 needs fb-2)
3. fb-6 (the tracer)
4. fb-7, fb-8
5. fb-9, fb-10, fb-11
6. fb-12, fb-13
7. fb-14, fb-15

Then once: CI watch, verify (each bead's probe), review (a security lens: migrations and a public API change; a UX designer reviews before/after screenshots, dark and light, posted as a PR comment), one fix agent, the Pi bench.

## For the owner (plain words)

- The Position is now four numbers: where the Frame's left and top edges sit and how wide and how tall it is, each as a share of the screen. You drag any edge or corner, or type them in pixels; the Frame's shape is whatever box you draw. A box can never hang off the screen.
- The Frame's size in pixels and its shape are worked out from the box and the screen's resolution; its size in centimetres from the box and the display's own size, which the display reports. Your Samsung reports 95 × 54 cm (right) in one place and 187 × 105 cm (wrong) in another; Photo Wall reads the right one.
- If a display reports no size: the room's even light is still drawn, but lamps and window light (and the mat's shadow) are not, and the Frame counts as large for videos, so a low-resolution video is not put on it. The other choice was to let you type a display's size; say if you want it.
- If Photo Wall does not know a Frame's screen resolution at all, that Frame gets no photos (it could not show them). The resolution now also comes from the display's preferred mode when the Pi did not report one.
- "Shows videos" becomes a switch on the Frame's Hardware tab. The pixel, diagonal and wall width and height fields go; the wall plan draws each Frame at its real size from its display, or as a dashed outline when that is not known.
- Your Frame keeps its picture through the upgrade: its current Position converts to the same pixels and it stays ready. Take a database dump before deploying; the Pi gets a new release at the same time.
