# Render planes, S3 (the Node on the new path only): frozen page

Branch `feat/render-planes` at 2ea7c172 (S2 landed). Plan: `/Volumes/Dock/tmp/node-render-arch/plan.md` v3 §4 S3 and §6. S1's page (`s1-page.md`: the composition, budget, screens, fit pass, GPU, decode, renderer) and S2's (`s2-page.md`: the turn in the Output document, `RectangleFit`) are frozen; this page changes only what it names. Process: `~/.claude/skills/implementation-workflow/SKILL.md`.

The interface commit that carries this page holds every NEW S3 symbol as code with no bodies (Python stubs raise `NotImplementedError("render-planes S3 bead Bn")`, the C stub refuses): `appliance/display_host/screen_turn.py`, `player/pattern_pass.py`, `player/renderer_root.py`, `PlaneScreens.mode` and `.trial`, `overlay_corners`, `OverlayCorners`, `pw_screen_overlay`, geometry's `placement_uv`, `commissioning_grid`, `picture_board`, `look.moments`, `FrameLoop` and `FRAME`, `PlaneRenderer.set_player`, `.capacity`, `.frame`, `.finish`, `.due`, `Gpu.renderer`, `output_discovery.measured`. It also made two mechanical moves so no two parallel beads share a file: `FrameGrant`, `composition_tag` and `CLIENT_LIBRARY` now live in `player/plane_screen.py` (every importer repointed; `player/wayland_frames.py` imports them until B8 deletes it), and `AppliedTrial` lost `primitives` (the overlay's corners are the renderer's now; `player/native.py` computes its own until B8 deletes it). **Changes to existing signatures are stated here and made by the bead that owns them, with every caller and test, in one commit.** A bead that finds a signature wrong appends to `.claude/errata.md` and STOPS.

## What S3 delivers

- **Display Host turns the screen** from the Output document's `turn` (S2), through the shell's `turn` op (S1).
- **The Player runs on the new path only**: `PlaneRenderer` is the Player's `Renderer`, placing each Frame by its `RectangleFit` Position (or a Position trial's), drawing test patterns, calibration targets and Position's commissioning grid, driven every refresh by a frame loop on the screens' connection; GPU work never sits between a due commit and the screen (tracer 3's photo-to-video fade, 49-59 a second).
- **Deleted**: the GTK GLArea renderer (`player/native.py`), its fitted pictures and masks (`player/fitted.py`), the GDK bridge (`player/wayland_frames.py`), the GDK frame clock pieces of `player/mainloop.py`, their harnesses and tests, and GTK (and `gstreamer1.0-libav`) from the Player's Depends.
- The Player enrolls each Output with its mode as built (S2's erratum: a Pi enrolled 0 x 0, so Central sized no photo by the rectangle and Save's off-screen check never fired there).

## Decisions and departures, with their cost

1. **The identify banner and the enrollment notice are deleted, not made sub-windows** (plan v3 §4 says "banner, notice ... included"; the work proves it cannot be done). Both show only on an Output with no Frame bound (`service._apply_identify_output` refuses a bound one; Central's `coordination.py` sends identify only for an unbound Output), and Display Host grants an Output only for a bound Frame; the shell covers every Output not handed off with its opaque diagnostic curtain (`shell.c` `invalidate`: `curtain(o, 1.0f)`). On the node path neither was ever visible, and on the new path the Player cannot present at all without a grant. The Player stops offering the `identify_output` capability (`ControlHello.capabilities` empty), so Central's existing refusal answers the console's Identify (`identify_unsupported`, `tests/test_player_control_protocol.py`) instead of a silent nothing. Cost: Identify does nothing visible on a Pi, as before S3, now said honestly. **Owner question** (errata): move Identify to Display Host's diagnostic page (a Display Host feature over the display line), or delete the console's button. Nothing in Central or the console changes in S3.
2. **A pattern is the screen's ground, not a plane.** A test pattern, a calibration target or the commissioning grid is drawn once into a whole-screen picture, black outside the Frame, and committed as the screen's ground with no planes above it. `Plane`, `Composition` and `compose` (S1) stay as they are; the look's "outside every opening stays black" stays one rule (`final_pixel`'s `inside`). Cost: one full-screen draw per pattern change (rare: a tuning action); with several Frames per screen (Q4, later) a ground would hold every Frame's pattern (`PatternRequest` then takes a placement per Frame).
3. **`present` refuses over the budget instead of cutting.** S1's `present` cut with `within`; the executor's acknowledgment then refused the drawn composition (it requires every requested layer), so a cut could never be acknowledged. The executor's shed loop (`renderer.capacity`, `visibility.shed`) is the one home of who gives up (plan v3 §2: "today's capacity"); `capacity` judges each composition at its busiest moment (`look.moments`) so a fade that starts between ticks is counted. `within` stays as the budget module's pure cut, used by the bench, which stands in for the executor.
4. **GPU work only in `finish`.** `present`, `frame` and `prepare` never draw, upload, import or fence; `finish` runs after the turn's commits. A still therefore finishes only while the frame loop runs (it does while anything is finishing, `due`), and a prepared layer is reported prepared one turn later than S1's in-line step. This is tracer 3's fix: a video's frame was drawn inside `present` (and in its decoder callback) before the due commit.
5. **Display Host hears of Outputs from the Output reports** (`runner.PowerObservations`, as the power and picture controllers do), not from a new shell hook: documents arrive before the shell announces any Output, so the first ask is refused and the Output's first report triggers it again. A turn's own remode re-reads the EDID (the Output's key moved) and puts no report (it is unchanged), so a turn never triggers itself.
6. **The frame client keeps its surface-level functions** (`pw_frame_client_create`, `tag_next_commit`, `presentation`, `overlay`): the display smoke's probe app (`tests/native_display_smoke.py`, `tests/native_display_probe.c`) uses them, and they take a `wl_display`, not GDK. Only the Python GDK bridge goes. `pw_screen_overlay` is `pw_frame_client_overlay` on the screen's surface.
7. **`player/geometry.py`'s Cover and Corners references stay** (`output_to_picture`, `picture_source_uv`, `frame_lights`, `board_draw`, `opening_size_mm`, `compose_pixel`, `output_pixel`, `homography`): their parameter types are S5's to delete, and the calibration target reader (`scripts/calibration_target/`, `tests/calibration_target_scenes.py`) and five reference tests use them. S5's page must take them and their consumers (errata). S3 deletes only the shader copies (`native.py`) and `fitted.py`.
8. **Eight beads, not four** (plan v3: 4 x 90 minutes, W4 2 h). Each is sized under the 90-minute ceiling on compile-unit seams; four run at once, then two, then one, then one: about 5.5 h of wall clock. The chain is forced by green-alone: the picture end-to-end harness cannot move to the new path before `finish` exists (B5), and the old path cannot be deleted while that harness imports it (B7 before B8).
9. **The Pi proof of the fade cadence is not a bead.** After B5 the bench drives `frame`/`finish`; a Pi run (W-step after S3, as W2) re-measures the photo-to-video fade (target: every refresh, 59-61 a second, as unloaded fades). CI cannot see plane timing.
10. **Old Position forms fail on the new path.** A Cover or Corners binding (or trial) answers `failed`/`decode` until S5's migration 102 converts them; new Frames still start on Cover (S2). Nothing deploys between S3 and S5 (one PR).

## Rules for every bead

- Siblings share one checkout. Keep to your owned files; derived outputs go outside the repo (`$TMPDIR/render-planes-s3-<bead>/`: pytest `--basetemp` with a short path, a meson build dir, a `debian-packaging/build-repo.sh --output` dir).
- Land: `git add -A -- <owned> && git commit -m '<message>' -- <owned> && git push origin HEAD:feat/render-planes`, Conventional Commits, trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. A commit that meets a sibling's `index.lock` waits and retries. If the push is rejected: `git fetch origin && git rebase origin/feat/render-planes` (your own unpushed commit only), re-run your checks, push.
- Run your changed tests (`.venv/bin/python -m pytest -q -n 4 --dist worksteal <tests>`), `.venv/bin/ruff check .`, `.venv/bin/lint-imports`, `python3 scripts/check_docs.py`. C and the display harness build only in Linux arm64 Docker (`debian-packaging/build-repo.sh`, then `scripts/run_display_harness.py --debs`); Docker hangs on this Mac: when `docker info` does not answer in a minute, push and let CI's `node-components` `debs` and `node-pid1` `display-harness` jobs judge; record NOT RUN. CI is the full gate; no local full verify.
- New Python modules import no native library at module level (load GStreamer, GL, EGL, GBM and the frame client in constructors). No GLib source outside `player/mainloop.py` (`tests/test_player_mainloop.py`).
- Docs are S6's bead; keep every module docstring you touch true (each states the rules it owns). `.venv/bin/python`; revert `uv.lock` if anything rewrites it.

## Shared primitives (frozen now)

| Primitive | Home | Responsibility | Consumers in S3 | Scheduled |
|---|---|---|---|---|
| `ScreenTurns`, `turn_due` | `appliance/display_host/screen_turn.py` | the screen hangs as its Output document says | `runner.py` | several Frames per screen (one turn per Output) |
| `PatternRequest`, `PatternPass`, `PATTERN_CODES` | `player/pattern_pass.py` | a Frame's pattern drawn into its screen's ground | renderer, harnesses | S5 (Position's editor uses the grid) |
| `placement_uv`, `commissioning_grid`, `picture_board` | `player/geometry.py` | the pattern pass's scalar reference | pattern pass, its tests | S5 deletes `board_draw`, keeps `picture_board` |
| `OverlayCorners`, `overlay_corners` | `player/plane_screen.py` | a trial overlay's corners from the drawn placement | renderer | S5 rectangle editor |
| `FrameGrant`, `composition_tag`, `CLIENT_LIBRARY` | `player/plane_screen.py` (moved) | the grant, the witness tag, the library path | renderer, screens, harnesses | |
| `moments` | `player/look.py` | the frame times covering every seen set of a composition | `PlaneRenderer.capacity` | |
| `FrameLoop`, `FRAME` | `player/mainloop.py` | the screens' frame turns on GLib | `renderer_root` | |
| `start_renderer`, `RendererRoot` | `player/renderer_root.py` | the new path composed | `service.main` | |
| `measured` | `player/output_discovery.py` | Outputs enrolled with their modes as built | `renderer_root` | |

## B1: Display Host turns the screen (about 75 minutes)

**Owns:** `appliance/display_host/screen_turn.py`, `appliance/display_host/runner.py`, `tests/node/display/test_screen_turn.py` (new), `tests/node/display/test_node_display_runner.py`.

**Frozen signatures** (stubs committed; bodies are yours):

```python
# appliance/display_host/screen_turn.py
TURN_SECONDS: Final = 2.0
class ScreenShell(Protocol):
    def turn(self, output_id: str, turn: DisplayTurn, *, timeout: float) -> bool
def turn_due(wanted: DisplayTurn | None, confirmed: DisplayTurn | None, *, appeared: bool) -> bool
class ScreenTurns:
    def __init__(self, shell: ScreenShell) -> None
    def start(self) -> None
    def stop(self, timeout: float = 5.0) -> None
    def document(self, output_id: str, document: OutputDocument) -> None     # never blocks
    def output(self, output_id: str, *, connected: bool) -> None             # never blocks
    def confirmed(self, output_id: str) -> DisplayTurn | None

# appliance/display_host/runner.py (changed)
class PowerObservations:
    def __init__(self, power: OutputPowerController, picture: PictureController, turns: ScreenTurns) -> None
    # put_report also calls turns.output(report.output_id, connected=report.connected)
def document_feed(power: OutputPowerController, picture: PictureController,
                  turns: ScreenTurns) -> Callable[[str, Document], None]
    # an Output document goes to power.document and to turns.document
```

**Rules:** the module docstring's (wanted, when to act, newest wins). `main` builds `ScreenTurns(backend)` (the `WestonBackend`, whose `turn` is S1's), starts it with power and picture, passes it to `document_feed` and `PowerObservations`, and stops it on the way out (`_stopping` gains it). One worker thread; one `shell.turn` per trigger; `confirmed` set only from a True answer. No change to `weston.py` or the shell.

**Acceptance (unit tier, fake shell):** a document with turn 90 asks the shell once for 90 and `confirmed` is 90; the same document again (a `change` rise with the same turn) asks nothing; a document with turn 0 asks for 0; a refused ask is not repeated until the next trigger; a document before any Output report is refused, then `output(..., connected=True)` asks again and confirms; `output(..., connected=False)` forgets `confirmed`; a document arriving while the worker is blocked in `turn` replaces the waiting one (only the newest is asked for next); `turn_due` truth table. `document_feed` routes an Output document to both power and turns, a picture document to picture only; `PowerObservations.put_report` reaches turns with the report's connection. Linux (as `tests/node/display/test_display_turn.py`): `ScreenTurns` over a real `WestonBackend` on a socketpair sends `{"op": "turn", ..., "transform": 1}` for turn 90 and confirms on the `turn` event.

**Mutation probe:** drop the `appeared` clause from `turn_due` (ask only when wanted differs from confirmed): the document-before-the-Output case never turns and its test fails.

## B2: the pattern pass (about 90 minutes)

**Owns:** `player/pattern_pass.py`, `player/geometry.py` (the three new functions; their reference section), `tests/native_fit_pass_harness.py` (new cases), `tests/test_player_test_patterns.py`, `tests/test_pattern_pass_reference.py` (new).

**Frozen signatures** (stubs committed):

```python
# player/pattern_pass.py
PATTERN_CODES: Final[Mapping[TestPattern | None, int]]     # None 0 (the grid), WHITE 1, GREY_RAMP 2, BLACK_LEVEL 3, TARGET_PRE_SOFTWARE 4, TARGET_POST_SOFTWARE 5
@dataclass(frozen=True)
class PatternRequest:
    size: tuple[int, int]; placement: Placement; turn: DisplayTurn; operations: PictureOperations
    lights: FrameLights; lut: bytes | None; board: BoardDraw | None
class PatternPass:
    def __init__(self, gl: Any) -> None                                  # FitFailed("gpu_error")
    def draw(self, request: PatternRequest, target: DrawTarget) -> None  # ValueError("pattern_target_size" | "board"), FitFailed
    def close(self) -> None

# player/geometry.py
def placement_uv(x: float, y: float, placement: Placement, screen: tuple[int, int]) -> tuple[float, float] | None
def commissioning_grid(u: float, v: float) -> Linear
def picture_board(profile: FrameProfile, modules: QrModules | None) -> BoardDraw
```

**Rules:** `pattern_pass.py`'s module docstring (which pattern; what a draw computes; the codes). The shader is written from the reference (it is in the Frame's picture space, not `native.py`'s opening space): `LOOK_GLSL`'s `srgb_decoded`, `look` and the Display's steps for the look patterns and the grid, the board's `board_point`/`board_signal` with its raster on its own unit and the QR's modules on another, the pattern codes and geometry's constants written in from their homes (as `native.py`'s `_FINAL_CONSTANTS`). The LUT step is a compile-time `lut_on` (as `fit_pass.py`). `picture_board` shares `board_draw`'s body (as `picture_lights` shares `frame_lights`'). `tests/test_player_test_patterns.py` reads the codes from `player/pattern_pass.py` (no longer `native.py`'s `_FINAL`) and its docstring names the fit pass harness as the shader's holder.

**Acceptance:** unit tier: `placement_uv` maps a placement's top-left to (0, 0), is None on its right and bottom edges and outside; `commissioning_grid` is the line colour at u = 0.1 and the slate at (0.05, 0.05); `picture_board(profile, None)` equals `board_draw(CoverFit(), profile, None)` for a landscape and a portrait profile; `PATTERN_CODES` holds every `TestPattern` and None, all codes distinct; for each pattern, `final_pixel` at a placement point equals the reference composed through `placement_uv` (the docstring's chain). Display harness (`tests/native_fit_pass_harness.py`, software GL, `PatternPass` into plain framebuffers): on a 64 x 48 screen with a 32 x 18 placement at (16, 15), each of WHITE (non-neutral operations), GREY_RAMP, BLACK_LEVEL, TARGET_PRE_SOFTWARE and TARGET_POST_SOFTWARE (with modules for a fixed `TargetCode` and with None) and the grid (with a LUT and a far light) equals the reference within one level at sampled pixels away from edges, and is exactly (0, 0, 0) outside the placement; the same at turn 90 with the target 48 x 64; a calibration target without `board` raises ValueError("board"); a wrong target size ValueError("pattern_target_size"). `tests/native_renderer_pixel_harness.py` stays unchanged and green.

**Mutation probe:** in the shader, map target pixels to the screen without the turn (turn 0 always): the turn-90 cases fail.

## B3: the screens carry the trial (about 75 minutes)

**Owns:** `appliance/display_host/native/frame-client.c`, `appliance/display_host/native/frame-client.h` (comments and the new prototype only), `player/plane_screen.py`, `tests/test_plane_screen.py`, `tests/native_screen_harness.py`.

**Frozen signatures:**

```c
/* frame-client.h (committed; the .c stub refuses) */
int pw_screen_overlay(struct pw_screen *screen, const char *grant_id, const char *frame_tag, const char *primitives);
```

```python
# player/plane_screen.py (stubs committed)
OverlayCorners = tuple[tuple[float, float], tuple[float, float], tuple[float, float], tuple[float, float]]
def overlay_corners(placement: Placement, screen: tuple[int, int]) -> OverlayCorners   # ValueError("overlay_screen" | "overlay_outside")
class PlaneScreens:
    def mode(self, output_id: str) -> tuple[int, int] | None
    def trial(self, composition: OutputComposition, grant: FrameGrant) -> AppliedTrial | None
    # changed (yours, with its callers' fakes in tests/test_plane_screen.py):
    def present(self, composition: Composition, pictures: Mapping[str, ScreenPicture], grant: FrameGrant,
                tag: str, picture_change: int | None, ground: ScreenPicture | None = None,
                overlay: OverlayCorners | None = None) -> int | PresentRefused
```

**Rules:** `pw_screen_overlay` is `pw_frame_client_overlay` on the screen's own surface (no second copy of its checks), -1 for a screen with no configure yet. `present` with `overlay` sends it first, as JSON `[[x, y], [x, y], [x, y], [x, y]]` (`json.dumps(..., separators=(",", ":"), allow_nan=False)`), under the same grant and tag the present carries; a refused overlay raises RuntimeError("display_trial_overlay_failed") before anything is committed. `trial` is `WaylandFrames.trial` moved onto this connection (one `TrialState` per Output, CLOCK_BOOTTIME milliseconds). `mode` is `pw_frame_client_output`'s width and height unturned. `overlay_corners` per its docstring. Update the module docstring (the trial and the overlay are this module's; the GDK sentence goes).

**Acceptance:** unit tier (fake library, as the existing tests): `overlay_corners(Placement(0, 1313, 2160, 1215), (2160, 3840))` is ((0, 0.341927...), (1, 0.341927...), (1, 0.658333...), (0, 0.658333...)); refusals; a present with an overlay calls `pw_screen_overlay` with that JSON and the present's tag before `pw_screen_present`, and one without calls it not at all; an overlay refused raises and commits nothing; `trial` hands the library's envelope to `TrialState` and returns its `AppliedTrial` (a `RectangleFit` trial included), None for result 0, RuntimeError for -1; `mode` with width 3840, height 2160, transform 1 is (3840, 2160) while `screen` is 2160 x 3840. Display harness (`tests/native_screen_harness.py`): with a trial set through the controller (`WestonBackend.trial`, as `native_display_smoke.py`'s overlay cases do) and the screen presenting under that trial's tag with an overlay, the shell sends `overlay_presented` naming that tag; a present without the overlay sends none.

**Mutation probe:** make `pw_screen_overlay` return 0 without sending the request: the harness waits for `overlay_presented` and fails.

## B4: the renderer is the Player's (about 90 minutes)

**Owns:** `player/plane_renderer.py`, `player/look.py` (`moments`), `tests/test_plane_renderer.py`, `tests/test_player_look.py` (new), `scripts/render_bench/__main__.py`, `scripts/render_bench/timeline.py`, `tests/test_render_bench.py`.

**Frozen signatures** (`set_player`, `capacity`, `moments` committed as stubs; the rest changed by you):

```python
# player/look.py
def moments(composition: OutputComposition, at: float) -> tuple[float, ...]

# player/plane_renderer.py
class Screens(Protocol):          # gains trial; present gains overlay (B3's signature)
    def trial(self, composition: OutputComposition, grant: FrameGrant) -> AppliedTrial | None: ...
    def present(self, composition: Composition, pictures: Mapping[str, ScreenPicture], grant: FrameGrant,
                tag: str, picture_change: int | None, ground: ScreenPicture | None = None,
                overlay: OverlayCorners | None = None) -> int | PresentRefused: ...
class Patterns(Protocol):         # player/pattern_pass.py PatternPass, as this module uses it
    def draw(self, request: PatternRequest, target: DrawTarget) -> None: ...
# FrameOnScreen deleted.
class PlaneRenderer:              # conforms to player/rendering.py Renderer
    gl_renderer: str | None
    def __init__(self, screens: Screens, device: Device, fitter: Fitter, patterns: Patterns, luts: LutSource, *,
                 stills: StillFactory, videos: VideoFactory, budget: ControllerBudget = PI5,
                 clock: Callable[[], float] = time.monotonic, gl_renderer: str | None = None) -> None
    def set_room_lights(self, document: RoomLightsDocument) -> None
    def set_player(self, player_id: str | None) -> None
    def prepare(self, local: LocalLayer, binding: OutputBinding) -> PrepareResult
    def capacity(self, compositions: tuple[OutputComposition, ...]) -> CapacityResult
    def present(self, composition: OutputComposition) -> PresentationResult
    def release(self, assignment_id: str) -> None
    def close(self) -> None
    # frame, finish, due: stubs, B5's.
```

`on_change` is gone: a video's new frame wakes the frame loop through the wiring (B6), not the renderer.

**Rules (the renderer composes; each rule it applies has its home):**
- **The drawn Position**: the Output's trial (`Screens.trial` under the composition's grant) when one is drawn, else the binding's calibration; its fit must be a `RectangleFit` (else `prepare` "failed"/"decode", `present` "failed"/"decode"); the placement is `place(fit.rectangle(), screen.size, profile aspect)`. The drawn lighting is the trial's when one is drawn, else the binding's. `prepare` reads the trial through the grant of `OutputComposition(binding)`; no grant, no trial.
- **What a present reports**: `applied_calibration` always: `AppliedCalibration(calibration=<drawn>)` with the trial's id, generation, sequence and hash when one is drawn (as `native.py` `_apply_trial`). With a trial drawn the commit carries `overlay=overlay_corners(placement, screen.size)`; the overlay is part of what a commit shows (Nothing changed).
- **Patterns** (decision 2): when the Output's operations carry a `test_pattern`, or a trial is drawn over a fallback composition with no layers (the commissioning grid), the commit has no planes and its ground is the pattern ground: `PatternRequest(screen.size, placement, screen.turn, operations, lights, lut, board)`, `board = picture_board(profile, modules)` for a calibration target, `modules = target_modules(TargetCode(player_id, operations.output_id, operations.picture_change))` once `set_player` named an id, else None. A pattern ground is drawn again exactly when its request changes (the LUTs' generation counted, as a still's key); it is one target per Output, fenced, "pending" (the screen keeps its last commit) until finished. The composition reported and tagged is the looked composition, as today: a pattern is a tuning overlay, not a change of what plays.
- **Capacity** (decision 3): `capacity` as its docstring; `present` over the budget answers "failed"/"capacity" and commits nothing; `within` is no longer called here.
- `gl_renderer` is the constructor's.
- Everything else S1 and its fix rounds made (stills in steps, fences, the ground, Nothing changed, one commit in flight, video targets) stays as it is; B5 moves the drawing out of `present`.
- **The bench** (`scripts/render_bench`): the tracer's binding carries `RectangleFit(centre_x=.5, centre_y=.5, width=1)`; `Tracer.rectangle` goes (`strengths_at` and `ground_seen` read `binding.calibration.fit.rectangle()`); `_present` sheds its own overload as the executor does (while `capacity` is unavailable, drop `visibility.shed`'s victim) before presenting; the bench builds `PatternPass(gpu.gl)` for the constructor.

**Acceptance (unit tier, fakes for every port):** a `RectangleFit(0.5, 0.5, 0.5)` binding places a half-width plane; a `CoverFit` binding prepares and presents "failed"/"decode"; a trial with another rectangle moves the plane, the commit carries `overlay_corners` of that placement and `applied_calibration` names the trial; the trial's lighting finishes the still again; with no trial `applied_calibration.calibration` is the binding's and no overlay is sent; operations with `TestPattern.WHITE` commit no planes and a ground drawn by `Patterns.draw` once (a second present draws nothing), a new picture change draws it again, and the reported composition still holds the layers; a trial over a fallback with no layers draws the grid ground (`operations.test_pattern` None); a calibration target's request carries `picture_board` with modules after `set_player("p")` and None before, and `set_player` with the same id draws nothing; `capacity` of a composition whose dissolve starts in 2 s counts both planes (three 4K full-width pictures fit `PI5`, four do not); a present over the budget answers "failed"/"capacity" with no commit; `moments` gives (at, mid of each fade under way or ahead) and (at,) for a still composition. Bench, pure: the tracer's binding is a rectangle and the overload step's shed leaves three pictures.

**Mutation probe:** read the binding's calibration even while a trial is drawn: the trial test (moved plane, overlay) fails.

## B5: the frame loop, finishing off the present path (about 90 minutes, after B4)

**Owns:** `player/plane_renderer.py`, `player/mainloop.py` (`FrameLoop`, `FRAME`), `tests/test_plane_renderer.py`, `tests/test_player_mainloop.py`, `tests/test_frame_loop.py` (new), `scripts/render_bench/__main__.py`.

**Frozen signatures** (stubs committed):

```python
# player/plane_renderer.py
    def frame(self) -> None
    def finish(self) -> None
    def due(self) -> float | None

# player/mainloop.py
FRAME = -50                                   # CONTROL < FRAME < TICK
class FrameLoop:
    def __init__(self, glib, fd: int, turn: Callable[[], float | None], *, clock: Callable[[], float] = time.monotonic) -> None
    def wake(self) -> None
    def stop(self) -> None
```

**Rules:**
- **GPU work only in `finish`** (decision 4): `present`, `frame` and `prepare` never call `Fitter.draw`, `Patterns.draw`, `Device.target`, `.texture`, `.upload`, `.import_frame` or `.fence`; they commit and report only finished pictures. `finish`, called after the turn's commits: (1) each seen started video's newest waiting frame drawn into a released target and fenced, one draw per video per call; (2) every pattern ground and black picture owed, drawn and fenced; (3) at most one still step over every Output when STEP_GAP has passed since the last (seen layers first, then by start), the fade gate (S1 fix round 2) unchanged. A video's decoder callback only records the waiting frame. Rewrite the module docstring's "Finishing never holds a due commit" to say so.
- **`frame`**: `present` again each Output's held composition (the newest one `present` was given; dropped on release of all its layers or on close) at this frame time; its answer is the renderer's own (the executor hears at its next tick).
- **`due`**: its docstring. `finishing` stays (the bench reads it).
- **`FrameLoop`**: its docstring: `glib.unix_fd_add_full(FRAME, fd, IN, ...)` kept armed (level: the turn reads the connection, so it is not ready again until the compositor speaks), `wake` an idle at FRAME (one pending at most), a timeout at FRAME armed for the answered time (replaced on each turn; none for None; 0 ms when already due), every source removed by `stop`. A raising `turn` is logged (`logging.getLogger(__name__).exception`) and treated as None.
- **The bench**: after each `present`, `renderer.finish()`; its loop wakes at `renderer.due()` or the screens' fd, as the FrameLoop would.

**Acceptance (unit tier):** with recording fakes, a present that commits makes no draw, upload or fence call, and a waiting video frame is drawn by the next `finish`, after the commit in call order; `prepare` of a new still takes no step and `finish` takes at most one per STEP_GAP; a dissolve held by one `present` is committed by `frame` at each of 60 frame times with the strengths `plane_strength` gives and no further `present` from the test; `due` is now while a fence is pending or a video frame waits, the fade's next change for a held composition ahead of its fade, None for a still held composition; a released layer's composition is no longer held. `FrameLoop` over a fake glib (records sources and priorities): an fd read runs one turn; two wakes before the idle runs one turn; the answered time arms one timeout at FRAME, replaced by the next answer; a raising turn is logged and arms nothing; `stop` removes every source. `tests/test_player_mainloop.py`: FRAME sits strictly between CONTROL and TICK.

**Mutation probe:** in `present`, draw a waiting video frame before the commit (S1's order): the call-order test fails.

## B6: the Player wired to the new path (about 90 minutes, after B4; beside B5)

**Owns:** `player/service.py`, `player/renderer_root.py`, `player/rendering.py`, `player/output_discovery.py`, `player/gpu.py` (`renderer`), `tests/test_player_service.py`, `tests/test_player_link_guards.py`, `tests/test_renderer_root.py` (new), `tests/test_output_discovery.py` (new), `tests/test_player_gpu.py`.

**Frozen signatures** (stubs committed: `start_renderer`, `RendererRoot`, `MODE_WAIT`, `measured`, `Gpu.renderer`; the rest changed by you):

```python
# player/renderer_root.py
MODE_WAIT: Final = 2.0
class RendererRoot:
    renderer: Renderer; outputs: tuple[OutputReport, ...]; fault: str | None
    def close(self) -> None
def start_renderer(glib, dispatcher: MainLoopDispatcher, luts: LutSource, outputs: tuple[OutputReport, ...], *,
                   socket: str | None = None, mode_wait: float = MODE_WAIT) -> RendererRoot

# player/output_discovery.py
def measured(outputs: tuple[OutputReport, ...], modes: Mapping[str, tuple[int, int]]) -> tuple[OutputReport, ...]

# player/gpu.py
class Gpu:
    @property
    def renderer(self) -> str | None

# player/rendering.py (changed)
class Renderer(Protocol):
    gl_renderer: str | None
    def set_player(self, player_id: str | None) -> None: ...
    def set_room_lights(self, document: RoomLightsDocument) -> None: ...
    def prepare(self, layer: LocalLayer, binding: OutputBinding) -> PrepareResult: ...
    def capacity(self, compositions: tuple[OutputComposition, ...]) -> CapacityResult: ...
    def present(self, composition: OutputComposition) -> PresentationResult: ...
    def release(self, assignment_id: str) -> None: ...
class UnavailableRenderer: ...    # moved here from player/service.py, the same null renderer
# RecordingRenderer: set_unbound_outputs, set_identify_output and their state removed.

# player/service.py (changed)
class PlayerConfig(Model): ...    # decoder_limit and texture_budget removed (NativeRenderer's only)
# the hello offers capabilities=() (decision 1); identify, the unbound diagnostic and their state removed
```

**Rules:** `start_renderer` per its module docstring: the turn it gives `FrameLoop` is `screens.dispatch(); renderer.frame(); renderer.finish(); return renderer.due()`; each video's `on_frame` (a streaming thread) posts through a `dispatcher.lane(...)` the renderer's callback and then `loop.wake()` (a full lane drops the post: the next frame comes); `gl_renderer=gpu.renderer`; modes waited for by dispatching the screens until every connected Output's `mode` is known or `mode_wait` passes. `main` builds the root after discovery and before `PlayerService`, enrolls with `root.outputs`, adds `root.fault` to the faults, closes the root at exit, and imports `gi.repository.GLib` only (no `Gtk`). Remove what served only the notice and the banner (`_render_unbound_diagnostic`, `_unbound_outputs`, `_apply_identify_output`, the `_identify_*` state, `_sync_identify_output`, the `display_serial` label's use); keep `central_link_state` only where something else reads it. `rendering.py`'s module docstring loses GTK.

**Acceptance (unit tier):** `measured` sets a connected Output's size from `modes`, keeps 0 x 0 for one absent or disconnected, keeps order; `start_renderer` with fakes for the screens, Gpu, passes and decoders (injected through module attributes or parameters your tests own) returns a root whose outputs carry the modes, whose turn calls dispatch, frame, finish, due in that order, whose video callback goes through the dispatcher lane then wakes the loop, and which closes in the docstring's order; a Gpu that raises gives `UnavailableRenderer`, fault "native_initialization" and the Outputs as discovered; no connected Output gives `UnavailableRenderer` and no fault. Service: the hello's capabilities are empty and an identify cue in a state changes nothing; the enrollment carries the measured sizes (`test_player_link_guards.py`'s `main` stubs the root); `PlayerConfig` refuses `decoder_limit`. `Gpu.renderer` strips and decodes `glGetString(GL_RENDERER)` (fake gl). The identify and unbound-diagnostic tests are deleted with their feature.

**Mutation probe:** make `measured` return `outputs` unchanged: the enrollment-size test fails.

## B7: the picture end-to-end harness on the new path (about 75 minutes, after B5 and B6)

**Owns:** `tests/native_picture_e2e_harness.py`, `tests/harness_device.py` (new), `tests/native_fit_pass_harness.py` (its GL context comes from `harness_device` only).

**Signatures (test code, yours):** `tests/harness_device.py` holds the harness's one EGL surfaceless GLES 3 context on software GL (moved from `native_fit_pass_harness.py`) and `MemfdDevice`, a `player/plane_renderer.py` `Device` whose targets are GL textures behind framebuffers with a memfd-backed LINEAR XR24/AR24 buffer each (as `native_screen_harness.py`'s pictures), whose `fence` reads every target's framebuffer back into its buffer (`glReadPixels`, RGBA swizzled to XR24/AR24 byte order) and is signalled at once, and whose `import_frame` raises RuntimeError (no video in the harness).

**Rules:** the Player child runs the real `PlaneRenderer` with the real `PlaneScreens`, `FitPass`, `PatternPass`, `StillDecode` and `PlayerLuts`, on `MemfdDevice`, presenting and finishing every 50 ms on real GLib (no `PHOTO_WALL_DISPLAY_HOST`, no GDK); the Frame's Position is a `RectangleFit` (centre (0.5, 0.5), width 0.6) on the 640 x 480 headless Output; everything else of the harness (Weston, shell, `WestonBackend`, `PictureController`, the health client) is as today.

**Acceptance (CI display harness):** cases 1-4 as today with the opening replaced by the rectangle's placement (inside equals `final_pixel` of the controller's operations, outside exactly black; neutral; the matched lux and far light; the LUT joining once released); new case 5: a picture document asking `TestPattern.WHITE` shows `display_steps` of white inside the placement and black outside, and its report says presented for that change. `native_fit_pass_harness.py` passes with its context from `harness_device`.

**Mutation probe:** make the renderer draw the pattern ground over the whole screen (the placement ignored, `PatternRequest.placement = Placement(0, 0, *size)`): case 5's outside samples are not black and the harness fails.

## B8: the old path deleted, packaging (about 90 minutes, last)

**Owns:** deletes `player/native.py`, `player/fitted.py`, `player/wayland_frames.py`, `tests/test_native_math.py`, `tests/test_player_fitted.py`, `tests/test_player_target_handoff.py`, `tests/native_player_mainloop_harness.py`, `tests/native_player_resource_harness.py`, `tests/native_renderer_pixel_harness.py`, `tests/native_display_gtk_probe.py`, `tests/test_player_frame_clock.py` (its tests of `look`, `next_change`, `fade_end` and the executor's timebase, the ones that build no `NativeRenderer`, `FrameFollower` or `DisplayEvents`, move to `tests/test_player_look.py` first); edits `player/mainloop.py` (deletes `FrameFollower`, `frame_time`, `DisplayEvents`, `WAYLAND_EVENTS`, `GDK_PRIORITY_REDRAW`, `REARM`; `MainLoopDispatcher` requires priority below FRAME, `Tick` CONTROL < priority, FRAME < priority), `tests/test_player_mainloop.py`, `tests/test_player_look.py`, `tests/test_player_luts.py` (its `native` cases go), `tests/native_display_service_probe.py` and `tests/test_node_display_native.py` (their GTK probe branch goes), `scripts/run_display_harness.py` (chain: smoke, screen, fit pass, picture e2e; docstring), `scripts/release_plan.py` (node-pid1 paths: the deleted harnesses out, `tests/harness_device.py` in), `debian/control` (`gir1.2-gtk-3.0` and `gstreamer1.0-libav` out of the Player's Depends and the Build-Depends; the comment naming `wayland_frames.py` names `plane_screen.py`), `appliance/display_host/native/frame-client.h` (its first line: no GTK thread), and the docstrings naming a deleted module or GTK in `player/geometry.py`, `player/look_shader.py`, `player/fit_pass.py`, `player/look.py`, `player/pacing.py`, `player/room_lights.py`, `player/plane_screen.py`, `player/plane_renderer.py`, `player/output_discovery.py`, `appliance/bootstrap.py`, `tests/test_print_look_reference.py`, `tests/test_player_board_reference.py`, `tests/test_player_target_reference.py`, `tests/test_room_light_physics.py`, `tests/test_player_test_patterns.py`.

**Rules:** nothing in `player/`, `tests/` or `scripts/` imports a deleted module or `gi`'s Gtk or Gdk; `rg -n "native.py|fitted.py|wayland_frames|GLArea|Gtk|Gdk" player tests scripts appliance debian` finds only `scripts/verify_netboot_initrd.py`'s forbidden-library list (GTK must stay out of the initramfs) and history in `.claude/`. The frame client's C functions stay (decision 6). Docs under `docs/` are S6's.

**Acceptance:** unit tier green with the deletions; `tests/test_player_mainloop.py` holds CONTROL < FRAME < TICK and refuses a Tick at FRAME; CI's `node-components` `debs` job (the import check judges the Player's Depends against its imports), `tests/debs/test_roots.py` and the `node-pid1` display harness (four harnesses) green.

**Mutation probe:** let `Tick` accept priority FRAME (the new ordering check removed): `tests/test_player_mainloop.py` fails.

## Parallel groups

1. **B1, B2, B3, B4** at once: disjoint files; B4's tests fake B2's and B3's ports (their signatures are on this page).
2. **B5, B6** after group 1: B5 owns `plane_renderer.py` after B4; B6 constructs B4's renderer and calls B5's methods only through `FrameLoop`'s turn (stubs until B5; its tests fake the renderer).
3. **B7** after group 2: its Player child needs `finish` (B5) and no GDK.
4. **B8** last: the old harnesses and `native.py` go only once B7 no longer imports them.

Then a Pi bench run (not a bead): the bench at B8's head, photo-to-video fade at every refresh (decision 9), the turned screen from a real Output document (Display Host's `ScreenTurns`, a document put on the Node's bus by hand or by a branch Central), plane log and verdict on the PR.

## Test architecture

- **Pure, unit tier (macOS and CI):** `turn_due` and `ScreenTurns` over a fake shell; the pattern reference; `overlay_corners`; `moments`; `measured`; the renderer through fakes of its six ports (call order recorded, so "finishing is off the present path" is a unit fact); `FrameLoop` over a fake glib; the root's wiring over fakes.
- **Display harness (CI `node-pid1` `display-harness`, Linux arm64, built debs):** the shell (`native_display_smoke.py`, unchanged), the screens with the trial overlay (`native_screen_harness.py`), the fit and pattern passes on software GL against the reference (`native_fit_pass_harness.py`), and the Player's new path end to end from a picture document to Weston's pixels on a memfd device (`native_picture_e2e_harness.py`). These are the conformance boundaries: the shell-client contract, the shader-reference contract, and the document-to-pixels chain.
- **Packages (CI `debs`):** the import check over the Player's new Depends; the app root loads the GPU libraries.
- **Pi only:** GBM targets on planes, the hardware decoder, plane timing (the fade cadence), a real turn on a real screen.

## Maintenance risks

- **The harness device is not the Pi's.** `MemfdDevice` reads back on the CPU and signals at once; fence timing, GBM scan-out and SAND128 import are proven only on the Pi. A Mesa or vc4 change can break the Pi with CI green.
- **The pattern GLSL is written anew in picture space** while `native.py` keeps the opening-space copy until B8; between B2 and B8 two shaders draw patterns (the old one only on the old path, which nothing in production uses after B6).
- **`geometry.py` carries Cover and Corners references until S5** (decision 7); S5 must delete them with the types.
- **Identify is refused until the owner decides** (decision 1); the console still offers the button.
- **The trial overlay maps the diagnostic layer over the Output**, so during a Position trial Weston composites the Player's pictures on the GPU (as before S1). Trials are short and rare.
- **`ScreenTurns` asks once per trigger**: a shell that refuses a turn for a connected Output leaves the screen unturned until the next document or reconnection (no retry loop, as power).

## Integration points with existing code

- `appliance/display_host/runner.py` `PowerObservations`, `document_feed`, `_stopping`: extended for turns. `weston.py` `WestonBackend.turn` (S1) is the port, unchanged.
- `player/executor.py`: unchanged; its capacity loop and acknowledgment are what decision 3 serves.
- `player/calibration_trial.py` `TrialState`: reused by `PlaneScreens.trial`; `AppliedTrial` lost `primitives` in the interface commit.
- `player/decode.py`, `player/gpu.py`, `player/fit_pass.py`, `player/planes.py`, `player/controller_budget.py`, `player/pacing.py`, `player/visibility.py`: reused as S1 left them (`Gpu.renderer` added).
- `contracts/player_control.py` `CAPABILITIES`: unchanged; the Player offers none of it.
- Central: unchanged by S3; it starts receiving real Output sizes at enrollment (S2's `frame_output` and Save's check then act on a Pi).
