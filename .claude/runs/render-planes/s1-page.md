# Render planes, S1 (the tracer): frozen page

Branch `feat/render-planes`, from `origin/main` at eba95ce7. Design: `/Volumes/Dock/tmp/node-render-arch/design.md` v2 (owner answers Q1-Q3, 2026-10-17). Plan: `/Volumes/Dock/tmp/node-render-arch/plan.md` v3 (owner answers Q4-Q6 and "go", 2026-10-18). Bench evidence: `/Volumes/Dock/tmp/pi-render-bench/` (T2 controller limits, T3 fade mix, T4 HEVC, T5 turn and targets). Process: `~/.claude/skills/implementation-workflow/SKILL.md`.

The interface commit that carries this page holds every S1 signature as code with no bodies (Python stubs raise `NotImplementedError("render-planes S1 bead Bn")`, C stubs return refusals), the protocol code generation in `appliance/display_host/meson.build`, the two new harnesses registered in `scripts/run_display_harness.py` and `scripts/release_plan.py` as placeholders, and the bench registered as never shipped. **The code is the frozen signature; this page states the rules, owners and proofs.** A bead that finds a signature wrong appends to `.claude/errata.md` and STOPS; it never edits another bead's file or a frozen signature.

## What S1 delivers

One Frame on the test Pi's 4K screen: photos dissolving, a dip to black, a photo dissolving into a 1080p30 HEVC video with the look, then the screen turned 90 degrees; every picture finished once by the GPU at its Frame's size and shown as its own display plane, its fade mixed by the display controller. The Player's new path stands beside the old one (S3 switches over and deletes the old); a bench script stands in for Central and Display Host's controller.

**Departures from plan v2 and the design, with their cost** (all in plan v3):

1. **No frame protocol v5.** Sub-windows (`wl_subsurface`), window alpha (`wp_alpha_modifier_v1`, wayland-protocols staging), the turn (`wl_output.transform`) and buffers (`zwp_linux_dmabuf_v1`) are standard Wayland; the grant stays per Output (Q4: one Frame per screen) and the tag stays on the screen's commit. The private XML is untouched. Cost: none; the trial overlay's rectangle (S5) still fits v4's four-corner primitives.
2. **Dip or dissolve is read from the stack, not the plan.** `player/planes.py` decides it from what the Player shows beneath the fading picture (`visible_now`, the one rule of what can be seen). Plan v2 had Central's plan say which: that would copy the visibility rule into Central and be wrong for a Run playing beneath another. Cost: the owner's rule "the Player only draws the alphas the plan carries" (2026-10-10) is kept in letter (the alpha drawn is the plan's); the strength is the controller's encoding of it. Flagged for the owner in plan v3 §2.
3. **The controller budget lives in the Player** (`player/controller_budget.py`), not contracts: with one Frame per screen Central has no use for it until several Frames come (Q4 "later"). Moving it to contracts then is one import change.
4. **The rectangle is a contracts value, not yet a Position form.** `FrameRectangle` and `place` are frozen now; S2 adds the wire form (`RectangleFit` in `FrameFit`) with the generated console contract. Until then the Player's new path takes the rectangle beside the binding (`FrameOnScreen`). Growing `FrameFit` in S1 would have pulled the console into the tracer.
5. **Six beads, not five.** Each is sized under the 90-minute ceiling; the GPU device with hardware decode, and the fit pass with the look's move, are each a bead.

## Rules for every bead

- Siblings share one checkout (group 1 runs five beads at once). Keep to your owned files; derived outputs go to your own directory outside the repo (`$TMPDIR/render-planes-<bead>/`: pytest `--basetemp` with a short path, a meson build dir, a `debian-packaging/build-repo.sh --output` dir).
- Land: `git add -A -- <owned> && git commit -m '<message>' -- <owned> && git push origin HEAD:feat/render-planes`. A commit that meets a sibling's `index.lock` waits and retries.
- Run your changed packages' tests (`.venv/bin/python -m pytest -q -n 4 --dist worksteal <your tests>`), `ruff check`, `lint-imports`, `python3 scripts/check_docs.py`. C and the display harness build only in Linux arm64 Docker (`debian-packaging/build-repo.sh`, then `scripts/run_display_harness.py --debs`); when Docker is unavailable, push and let CI's `node-components` `debs` and `node-pid1` `display-harness` jobs judge. CI is the full gate; no local full verify.
- New Python modules import no native library at module level (GStreamer, GL, EGL, GBM, the frame client): load them in constructors, as `player/native.py` does, so the pure parts import anywhere.
- No GLib source (idle, timeout, fd watch, and no identifier named `Source`, `Idle` or `Timeout`) in a `player/` module other than `player/mainloop.py` (`tests/test_player_mainloop.py`): schedule through its dispatcher.
- Replace your placeholder harness (B3, B4) with the real one: the end verifier fails a run that still prints `PLACEHOLDER`.
- Docs are S6's bead; keep module docstrings true (each states the rules it owns).

## Shared primitives (frozen now)

| Primitive | Home | Responsibility | Consumers now | Scheduled |
|---|---|---|---|---|
| `DisplayTurn`, `TURNS`, `wayland_transform`, `turn_of`, `hanging` | `contracts/display_turn.py` (stdlib) | the turn's vocabulary and its one mapping to `wl_output.transform` | planes, fit pass, geometry, screens, bench, `WestonBackend.turn` | S2 Output document, S3 Display Host |
| `FrameRectangle`, `Placement`, `place` | `contracts/frame_fit.py` | how a rectangle becomes whole pixels on a screen as it hangs | planes, renderer, bench | S2 `RectangleFit` and Central's Frame output, S5 migration |
| `Screen`, `FrameLayers`, `Plane`, `Composition`, `plane_strength`, `compose` | `player/planes.py` (pure) | one screen's planes and their strengths | budget, screens, renderer, bench | S3 executor wiring |
| `ControllerBudget`, `PI5`, `load`, `fits`, `within` | `player/controller_budget.py` (pure) | the controller's limits and the cut | renderer, bench | several Frames per screen (later) |
| `Target`, `DmabufFrame`, `Targets` | `player/gpu.py` | a scan-out-able picture buffer; a decoder frame | fit pass, screens, renderer | S3 |
| `FitSource`, `FitRequest`, `DrawTarget`, `target_size` | `player/fit_pass.py` | what one finishing draw takes | renderer | S3 test patterns, targets |

`contracts/display_turn.py` stays stdlib-only and never imports `contracts/frame_fit.py` (import-linter: Display Host never reaches the Frame fit's geometry).

## B1: the composition and the budget (pure)

**Owns:** `contracts/display_turn.py`, `contracts/frame_fit.py` (the three additions and the module docstring's rule list), `player/planes.py`, `player/controller_budget.py`, `tests/test_display_turn.py`, `tests/test_frame_placement.py`, `tests/test_planes.py`, `tests/test_controller_budget.py` (new). About 75 minutes.

**Frozen signatures** (bodies are yours; the docstrings in the stubs are the contract):

```python
# contracts/display_turn.py
DisplayTurn = Literal[0, 90, 180, 270]
TURNS: Final[tuple[DisplayTurn, ...]] = (0, 90, 180, 270)
def wayland_transform(turn: DisplayTurn) -> int            # 0->0, 90->1, 180->2, 270->3; ValueError("display_turn")
def turn_of(transform: int) -> DisplayTurn                 # inverse; 4..7, other values, bools: ValueError("display_turn")
def hanging(size: tuple[int, int], turn: DisplayTurn) -> tuple[int, int]   # swap for 90/270; ValueError("screen_size")

# contracts/frame_fit.py
@dataclass(frozen=True)
class FrameRectangle:      # centre_x, centre_y in 0..1, width in (0, 1], finite, never bool; ValueError("frame_rectangle")
    centre_x: float; centre_y: float; width: float
@dataclass(frozen=True)
class Placement:           # whole pixels as the screen hangs
    x: int; y: int; width: int; height: int
def place(rectangle: FrameRectangle, screen: OutputSize, aspect: float) -> Placement
    # half-up rounding; ValueError("placement_screen" | "placement_aspect" | "placement_outside")

# player/planes.py
MIN_STRENGTH: Final = 1 / 255
@dataclass(frozen=True) class Screen: output_id: str; size: tuple[int, int]; turn: DisplayTurn
@dataclass(frozen=True) class FrameLayers: frame_id: str; placement: Placement; layers: tuple[LocalLayer, ...]
@dataclass(frozen=True) class Plane: frame_id: str; layer: LocalLayer; at: Placement; strength: float
@dataclass(frozen=True) class Composition: screen: Screen; planes: tuple[Plane, ...]
def plane_strength(alpha: float, over_picture: bool) -> float        # ValueError("plane_alpha")
def compose(screen: Screen, frames: Sequence[FrameLayers]) -> Composition
    # ValueError("compose_frames" | "compose_outside" | "compose_output")

# player/controller_budget.py
@dataclass(frozen=True) class ControllerBudget: planes: int; line_pixels: int
PI5: Final = ControllerBudget(planes=16, line_pixels=15_360)
def load(compositions: Sequence[Composition]) -> tuple[int, int]     # ValueError("budget_screens")
def fits(budget: ControllerBudget, compositions: Sequence[Composition]) -> bool
def within(budget: ControllerBudget, compositions: Sequence[Composition]) -> tuple[tuple[Composition, ...], tuple[str, ...]]
```

**Rules:** `plane_strength` is `alpha` over a picture and `encode_srgb(alpha)` (`player/geometry.py`) over black. In `compose`, a layer is over a picture exactly when the nearest seen layer beneath it on the same Frame is a media layer; a plane under `MIN_STRENGTH` is left out (removed, never alpha 0); planes keep `visible_now`'s order, Frames in the order given. `load`: planes = per screen 1 + its planes; line pixels = per screen, its physical width (hanging width at 0/180, hanging height at 90/270) plus the most picture pixels crossing any one physical scanline (for 0/180 the sum of widths of planes covering a hanging row, for 90/270 the sum of heights of planes covering a hanging column), summed over screens. `within`: `player/visibility.py` `shed`'s order applied to planes; never a Frame's only plane.

**Acceptance:** `wayland_transform`/`turn_of` round-trip every turn and refuse 4..7 and bools; `hanging` swaps only for 90/270. `place` on a 3840x2160 screen: (0.5, 0.5, 1.0) at 16:9 is (0, 0, 3840, 2160); on a 2160x3840 (turned) screen the same rectangle is (0, 1313, 2160, 1215) (y = floor(1920 - 607.5 + 0.5)); outside and degenerate inputs refuse with their codes. `compose`: a dissolve (outgoing at 1, incoming at 0.3) gives strengths (1, 0.3); a dip (one photo at 0.3 alone, or over a black layer) gives `encode_srgb(0.3)`; a layer covered by an opaque one above has no plane; an incoming dissolve layer at alpha 0.001 has no plane (below `MIN_STRENGTH`), while a dip layer at 0.001 has one (`encode_srgb(0.001)` is about 0.0129). `load` of one 4K screen with three full-width planes is (4, 15,360) and fits `PI5`; four is over; at 90 the same placements count their heights; `within` of five full-width planes on one Frame drops the bottom two and reports their assignment ids bottom first. Every test is pure and runs in the unit tier.

**Mutation probe:** make `compose` call `plane_strength(alpha, False)` for every plane: the dissolve test (incoming at 0.3 must stay 0.3) fails.

## B2: Display Host: window alpha, the turn, amber on a live health client

**Owns:** `appliance/display_host/native/shell.c`, `appliance/display_host/overlay/client.py`, `appliance/display_host/weston.py` (`turn`), `tests/native_display_smoke.py`, `tests/native_display_probe.c`, `tests/display_harness_health_client.py` (only if the smoke needs it), `tests/node/health/test_display_overlay_health.py`, `tests/node/display/test_display_turn.py` (new). About 90 minutes. `meson.build` is frozen: the alpha-modifier server header and code are already generated for the shell.

**Frozen contract.**

- The shell serves `wp_alpha_modifier_v1` version 1 as wayland-protocols' `staging/alpha-modifier/alpha-modifier-v1.xml` states it: `get_surface` (error `already_constructed`), `set_multiplier(factor)` double-buffered, applied when the surface's state is applied (Weston's surface commit signal: for a synchronized sub-surface, its parent's commit) as `weston_view_set_alpha(view, factor / UINT32_MAX)` on every view of the surface; destroying the object resets the factor to 1 at the next commit; a request after its surface is gone raises `no_surface`. Any client may bind it.
- Control op `turn`: `{"op": "turn", "output_id": <id>, "output": <output key>, "transform": 0..3}` sets `weston_output_set_transform` and sends `{"event": "turn", "output_id": <id>, "transform": <n>}` ahead of the op's response; any other transform is refused (`false`). The wanted transform is kept across a hot-plug of the Output and applied to its new `weston_output`, as `black_on` is.
- `WestonBackend.turn(self, output_id: str, turn: DisplayTurn, *, timeout: float) -> bool`: the op with `wayland_transform(turn)`; true when the `turn` event confirms that transform; false for an unknown Output, a refusal or a timeout (like `black`).
- Amber (`fallback_sync`): wanted exactly while the Output is handed off (released) and the private client is not alive (not connected, or connected without having bound `pw_diagnostic_manager_v1` at version 3 or later). No longer keyed on a mapped health surface.
- The health client (`HealthLayer`): no transparent base map. A tint-off page commits a NULL buffer (the layer unmapped, so the Player's pictures can stay on planes) and is reported presented to the judge when the compositor has processed that commit (a `wl_display.sync` after it), at that callback's time. A tint-on page is unchanged. A tint-on page that cannot be drawn shows an amber single-pixel buffer (the shell's fallback colour, pre-multiplied) through the viewport over the whole Output, never nothing: the client's own fallback, now that the shell's amber keys on its liveness.

**Acceptance (CI: display harness, `tests/native_display_smoke.py` with `tests/native_display_probe.c`):** a probe sub-surface of opaque white at multiplier 0x7fffffff over the probe's black captures 127-128 inside it and black outside; after destroying its modifier and committing, 255. The `turn` op with transform 1 brings the `turn` event, the probe's `wl_output` reports transform 1 and the captured Output is 2160 wide by 3840 high (its mode turned); transform 4 is refused. With the private client alive and nothing to show, no health view is mapped and the capture equals the probe's pixels exactly; with the private client killed, the amber tint shows over the released Output and goes when the restarted client binds. Unit tier: the health layer's tint-off unmaps and reports presented after the sync; a failed tint-on draw commits the amber single-pixel buffer; `WestonBackend.turn` sends the op and answers the event (Linux, like `test_display_black_picture.py`).

**Mutation probe:** remove the `weston_view_set_alpha` call: the 0x7fffffff sub-surface captures 255 and the smoke fails.

## B3: the frame client's screens

**Owns:** `appliance/display_host/native/frame-client.c`, `appliance/display_host/native/frame-client.h` (bodies' comments only: its prototypes are frozen), `player/plane_screen.py`, `tests/native_screen_harness.py` (replace the placeholder), `tests/test_plane_screen.py` (new). About 90 minutes. `meson.build` is frozen: the client headers and code of xdg-shell, linux-dmabuf-v1, viewporter, single-pixel-buffer-v1 and alpha-modifier-v1 are already generated for the library.

**Frozen signatures:** `frame-client.h`'s new block (`PW_SCREEN_PLANES 15`, `struct pw_plane_state`, `pw_frame_client_connect`, `pw_frame_client_fd`, `pw_frame_client_output`, `pw_screen_create`, `pw_screen_size`, `pw_screen_destroy`, `pw_picture_import`, `pw_picture_busy`, `pw_picture_destroy`, `pw_screen_present`, `pw_screen_presentation`) as committed, with the return codes its comments give; every existing function works on a connected client too, and `pw_frame_client_destroy` disconnects a client it connected. Python:

```python
PresentRefused = Literal["picture_refused", "output_gone"]
SCREEN_PLANES: Final = 15
def multiplier(strength: float) -> int                     # round(strength * 0xffffffff); ValueError("plane_strength") unless 0 < s <= 1
@dataclass(frozen=True) class ScreenPicture: handle: int; size: tuple[int, int]
class PlaneScreens:
    def __init__(self, library: str = CLIENT_LIBRARY, socket: str | None = None) -> None
    fd: int  (property)
    def dispatch(self) -> None
    def screen(self, output_id: str) -> Screen | None
    def grant(self, composition: OutputComposition) -> FrameGrant | None
    def picture(self, output_id: str) -> PictureOperations | None
    def import_picture(self, target: Target) -> ScreenPicture | None
    def busy(self, picture: ScreenPicture) -> bool
    def forget(self, picture: ScreenPicture) -> None
    def present(self, composition: Composition, pictures: Mapping[str, ScreenPicture], grant: FrameGrant,
                tag: str, picture_change: int | None) -> int | PresentRefused
    def presentation(self, output_id: str, now: float) -> Word | None
    def close(self) -> None
```

**Rules:** the screen is an xdg toplevel with app id `photo-wall-<output_id>` (the shell maps it full screen; `player/output_discovery.py` `output_app_id`), its buffer a `wp_single_pixel_buffer_v1` opaque black stretched by `wp_viewporter` to each configure's size; sub-surfaces synchronized, created as needed up to 15, each with its alpha-modifier object; `pw_screen_present` orders them with `place_above`, attaches, sets the buffer transform, damages, sets the multiplier and commits each, attaches NULL to the rest, then `tag_picture` and presentation feedback on the screen's surface and its commit (the screen's buffer is attached once; its later commits carry no new buffer). Pings are answered while dispatching. `pw_picture_import` uses `zwp_linux_buffer_params_v1.create` and one roundtrip. `PlaneScreens.screen` builds `Screen(output_id, hanging(mode, turn_of(transform)), turn_of(transform))`; `present` maps each plane's picture by `plane.layer.layer.assignment_id`, the transform by `wayland_transform(screen.turn)`, the strength by `multiplier`, and the tag `picture_change` by `contracts.node_frame.picture_change_text`.

**Acceptance (CI: display harness, `tests/native_screen_harness.py`, headless Weston with the installed shell and library, the harness as controller like `native_display_smoke.py` and as app through `PlaneScreens`; pictures are memfd-backed LINEAR XR24 buffers passed through linux-dmabuf):** after the candidate commit and the handoff the Output shows black; two planes (red under green, green at strength 0.5) capture the stored-value mix within 2 levels; a following present with one plane leaves the other sub-surface unmapped; a picture is busy after the commit that shows it and released once a commit without it is presented; `present` returns rising sequences and the shell's `presented` event names the tag; after the `turn` op (transform 1) `screen()` reports turn 90 and the hanging size 2160 by 3840 (or the harness Output's own size turned), and a present of a target laid out turned with buffer transform 1 captures upright; an import with a stride below width x 4 returns None. If Weston 14 headless imports such buffers with neither the pixman nor the GL (llvmpipe) renderer, append the finding to `.claude/errata.md` and STOP: no shared-memory substitute. Unit tier (`tests/test_plane_screen.py`): `multiplier` at 1, 0.5 and 1/255 and its refusals; `present` refuses past 15 planes before touching the library.

**Mutation probe:** in `pw_screen_present`, skip unmapping the sub-surfaces past `count`: the one-plane present still captures the stale second plane and the harness fails.

## B4: the fit pass and the look's one shader

**Owns:** `player/fit_pass.py`, `player/look_shader.py`, `player/native.py` (only the move of the look's GLSL and its uniforms to `look_shader.py`, behaviour unchanged), `player/geometry.py` (`buffer_to_picture`, `fit_source_uv`, `picture_lights`, and `frame_lights` sharing its body with `picture_lights`), `tests/native_fit_pass_harness.py` (replace the placeholder), `tests/test_fit_pass_reference.py` (new). About 90 minutes.

**Frozen signatures:**

```python
# player/fit_pass.py
SourceEncoding = Literal["srgb", "bt709"]
class FitFailed(RuntimeError)                              # args[0] "gpu_error"
@dataclass(frozen=True) class FitSource: texture: int; external: bool; size: tuple[int, int]; encoding: SourceEncoding; alpha: bool
@dataclass(frozen=True) class FitRequest:
    source: FitSource; crop: PhotoCrop | None; aspect: float; placement: Placement; turn: DisplayTurn
    operations: PictureOperations; lights: FrameLights; lut: bytes | None
class DrawTarget(Protocol): framebuffer: int; size: tuple[int, int]   (read-only properties)
def target_size(placement: Placement, turn: DisplayTurn) -> tuple[int, int]
class FitPass:
    def __init__(self, gl: Any) -> None
    def draw(self, request: FitRequest, target: DrawTarget) -> None    # ValueError("fit_target_size"), FitFailed
    def black(self, target: DrawTarget) -> None
    def close(self) -> None

# player/look_shader.py
LUT_UNIT: Final = 1
LOOK_GLSL: Final[str]                 # GLSL ES 3.00: vec3 look(vec3 linear, vec2 opening_uv, ivec2 dither_at) and its uniforms
def look_uniforms(gl: Any, program: int, operations: PictureOperations, lights: FrameLights | None, *,
                  dither_height: int, lut_bound: bool) -> None

# player/geometry.py (the fit pass's scalar reference)
def buffer_to_picture(x: float, y: float, size: tuple[int, int], turn: DisplayTurn) -> tuple[float, float]
def fit_source_uv(u: float, v: float, aspect: float, source: tuple[int, int], crop: PhotoCrop | None) -> tuple[float, float]
def picture_lights(lighting: FrameLighting, document: RoomLightsDocument | None, profile: FrameProfile,
                   display: DisplayLight) -> FrameLights
```

**Rules:** a draw computes, per target pixel, `buffer_to_picture` then `fit_source_uv`, samples the source, decodes it (`decode_channel`, `video` for BT.709), and applies `final_pixel` with `inside=True`, `lights` from `picture_lights`, the pixel's (u, v) as the opening's, `lut`, and `dither_at` the target pixel; it writes stored 8-bit values; a source with `alpha` is written pre-multiplied (AR24) and an opaque one with alpha 1. The external sampler (`samplerExternalOES`) is used exactly when `source.external`. `LOOK_GLSL` is the only copy of the look's GLSL: `native.py`'s frame pass builds its program from it after the move. Test patterns and calibration boards stay in `native.py` until S3.

**Acceptance (CI: display harness, `tests/native_fit_pass_harness.py`: an EGL surfaceless GLES 3 context on software GL made by the harness, `FitPass` drawing into plain texture framebuffers, read back with `glReadPixels`):** a four-quadrant RGBA photo at turn 0 with non-neutral operations, a far light and a LUT equals the reference at sampled pixels away from the quadrant edges within one level of 256; at 90 and 270 the target is height by width and the quadrants land where the reference puts them; crop offset 0 and 1 show the photo's left and right edges; a PNG with alpha 0.5 writes pre-multiplied values; `black` writes (0, 0, 0, 255); a 2D source marked `bt709` equals the reference decoded as video; both linked programs (the fit pass's and the frame pass's) contain `LOOK_GLSL`. The existing `tests/native_renderer_pixel_harness.py` passes unchanged. Unit tier (`tests/test_fit_pass_reference.py`): `target_size` swaps for 90/270; `buffer_to_picture` maps the target's corners to the picture's corners for each turn; `fit_source_uv` agrees with `crop_window`; `picture_lights` of a Frame equals `frame_lights` of a full-screen unturned Cover fit of the same Frame mapped to picture space.

**Mutation probe:** drop the turn from the shader's target-to-picture step: the 90-degree case's quadrants land wrong and the harness fails.

## B5: the GPU device and hardware decode

**Owns:** `player/gpu.py`, `player/decode.py`, `debian/control` (the `photo-wall-player` Depends: add `libgbm1` and `libgles2`), `tests/test_player_gpu.py`, `tests/test_player_decode.py` (new). About 90 minutes.

**Frozen signatures:**

```python
# player/gpu.py
RENDER_NODE = "/dev/dri/renderD128"; XR24 = 0x34325258; AR24 = 0x34325241; NV12 = 0x3231564E; LINEAR = 0
class GpuUnavailable(RuntimeError)                         # args[0] "render_node" | "gbm" | "egl" | "context"
@dataclass(frozen=True) class Target: size; framebuffer: int; texture: int; fd: int; stride: int; fourcc: int; modifier: int
@dataclass(frozen=True) class DmabufFrame: fd: int; size; fourcc: int; modifier: int; offsets: tuple[int, int]; strides: tuple[int, int]; serial: int; position: float
class Targets(Protocol): target(size, *, opaque) -> Target; free(target) -> None
def sand_modifier(modifier: int, luma_stride: int) -> int  # ValueError("sand_modifier")
class Gpu:
    def __init__(self, render_node: str = RENDER_NODE) -> None
    gl  (property: PyOpenGL's OpenGL.GLES3)
    def target(self, size: tuple[int, int], *, opaque: bool) -> Target     # RuntimeError("gpu_target")
    def free(self, target: Target) -> None
    def upload(self, size: tuple[int, int], stride: int, pixels: bytes) -> int   # RuntimeError("gpu_upload")
    def delete_texture(self, texture: int) -> None
    def import_frame(self, frame: DmabufFrame) -> int       # RuntimeError("gpu_import")
    def forget_frames(self, fds: tuple[int, ...]) -> None
    def flush(self) -> None
    def close(self) -> None

# player/decode.py
DECODE_QUEUE = 4; STILL_TIMEOUT = 10.0; VIDEO_DECODER = "v4l2slh265dec"; DMABUF_CAPS = "video/x-raw(memory:DMABuf),format=DMA_DRM"
class VideoRefused(RuntimeError)                           # args[0] "decode"
@dataclass(frozen=True) class StillPicture: size; stride: int; pixels: bytes; alpha: bool
class StillDecode: __init__(path: Path); result() -> StillPicture | Literal["pending", "failed"]; close()
class VideoFrames: __init__(path: Path, on_frame: Callable[[], None]); seek(position: float) -> bool;
                   latest() -> DmabufFrame | None; done(frame: DmabufFrame) -> None; buffers() -> tuple[int, ...]; close()
```

**Rules:** EGL on GBM (`EGL_PLATFORM_GBM_KHR`), a surfaceless GLES 3 context; targets are `gbm_bo_create_with_modifiers2` LINEAR, XR24 or AR24, scan-out usage, imported as an EGLImage bound to a texture behind a framebuffer, exported once with `gbm_bo_get_fd`; decoder frames import as `EGL_LINUX_DMA_BUF_EXT` NV12 two-plane images with `sand_modifier`, `EGL_ITU_REC709_EXT` and `EGL_YUV_NARROW_RANGE_EXT`, bound to `GL_TEXTURE_EXTERNAL_OES`, one EGLImage per decoder buffer (keyed by the fd's device and inode, offsets, strides and modifier). The video pipeline is built element by element (never `parse_launch` on a path), with a caps filter of `DMABUF_CAPS`, an appsink holding at most `DECODE_QUEUE` buffers, and a ctypes pad probe (`gst_pad_add_probe` on the appsink's sink pad, `GST_PAD_PROBE_TYPE_QUERY_DOWNSTREAM`) that adds `GstVideoMeta` to the allocation query with `gst_query_add_allocation_meta`, PyGObject never touching the query; a frame's planes come from its `GstVideoMeta`, its fd from `gst_dmabuf_memory_get_fd`. EOS loops to 0. Stills: `filesrc ! decodebin ! videoconvert ! capsfilter(video/x-raw,format=RGBA) ! appsink`, prerolled.

**Acceptance:** unit tier: `sand_modifier` of the bench's decoder modifier 0x0700000000000004 with luma stride 1632 sets the SAND128 column height to 1632 (`fourcc_mod_broadcom_code(4, 1632)`), leaves LINEAR alone, refuses negatives; `Gpu("/nonexistent")` raises `GpuUnavailable("render_node")`; `StillDecode` refuses a relative path; importing both modules loads no native library. The hardware path (targets the controller scans out, SAND128 import, the decoder's dmabufs, a 1080p30 HEVC clip playing at real time) is proven only on the Pi by the tracer (W2): there is no V3D or V4L2 decoder in CI.

**Mutation probe:** make `sand_modifier` return `modifier` unchanged: the column-height unit test fails (and on the Pi the video's EGLImage import fails).

## B6: the new renderer and the bench (after B1)

**Owns:** `player/plane_renderer.py`, `scripts/render_bench/` (`__init__.py`, `__main__.py`, `timeline.py`, `controller.py`, `kms_state.py`, `registers.py`, `evidence.py`), `tests/test_plane_renderer.py`, `tests/test_render_bench.py` (new). About 90 minutes. Needs B1's bodies (its tests compose real planes); the other beads' bodies are reached only through the ports below, faked in its tests.

**Frozen signatures:** `player/plane_renderer.py` as committed: `VIDEO_TARGETS = 3`; `FrameOnScreen(binding: OutputBinding, rectangle: FrameRectangle)`; the ports `Screens`, `Device`, `Fitter`, `Still`, `Video` (Protocols, the subsets of `PlaneScreens`, `Gpu`, `FitPass`, `StillDecode`, `VideoFrames` used); `StillFactory = Callable[[Path], Still]`; `VideoFactory = Callable[[Path, Callable[[], None]], Video]`;

```python
class PlaneRenderer:
    def __init__(self, screens: Screens, device: Device, fitter: Fitter, luts: LutSource, *,
                 stills: StillFactory, videos: VideoFactory, budget: ControllerBudget = PI5,
                 on_change: Callable[[str], None] = lambda output_id: None,
                 clock: Callable[[], float] = time.monotonic) -> None
    def set_room_lights(self, document: RoomLightsDocument) -> None
    def prepare(self, local: LocalLayer, frame: FrameOnScreen) -> PrepareResult
    def present(self, composition: OutputComposition, frame: FrameOnScreen) -> PresentationResult
    def release(self, assignment_id: str) -> None
    def close(self) -> None
```

and the bench modules as committed (`timeline.tracer`, `timeline.strengths_at`, `BenchController`, `kms_state.kms_planes`, `kms_state.read_properties`, `registers.glitched`, `RegisterWatch`, `evidence.verdict`, `__main__.main`). The debugfs state dump prints no plane alpha: `read_properties` reads each plane's `alpha` and `pixel blend mode` properties through libdrm as a read-only second client.

**Rules (the renderer composes; every rule it applies has its home elsewhere):** `present` looks the composition at `clock()` (`player/look.py`), places the Frame (`place(frame.rectangle, screen.size, profile aspect)`), composes, cuts with `within` over every Output's newest composition, and commits once (`Screens.present`) with the witness tag `player/wayland_frames.py` `composition_tag` of the drawn composition's seen layers and the picture change of the operations drawn. A still's picture is finished again exactly when its key changes: (decode, placement size, turn, crop, profile aspect, the Output's operations, the Frame's lights, the LUT generation); before any operations arrive it draws `neutral_operations(output_id, 1, UPGRADE_DISPLAY_LIGHT)` and tags no change (as `native.py` `_stand_in`). A playing video's newest frame is finished into the first of its `VIDEO_TARGETS` targets the screen has released, then `on_change(output_id)`. A black layer's picture is one black target per (Output, size), shared. A fade never draws. At most one commit in flight per screen (`player/pacing.py` `FramePacer` over `Screens.presentation`): a present meanwhile commits nothing and answers as the last commit did. Outcomes: no grant, "pending" and nothing committed; a candidate grant, a commit with no planes tagged `candidate-<grant id>`; Output gone or a picture refused, "failed"/"decode"; over budget after the cut, "failed"/"capacity"; a seen layer not finished yet, "pending", the screen keeps its last commit; else "presented" with the drawn composition (cut layers absent) and `presented_at` the commit's clock time. Resources of a released layer are freed once no commit shows them.

**The bench:** run on the Pi as root with Display Host's controller and the Player stopped (Weston, the shell and the health client running): it builds the tracer (`timeline.tracer`), grants itself the Output (`BenchController`), drives `PlaneRenderer` with the real `PlaneScreens`, `Gpu`, `FitPass` (on `Gpu.gl`), `StillDecode` and `VideoFrames`, presents on every refresh and on `on_change`, turns the screen at the turn step, and records a `Moment` ten times a second (the expected strengths from `strengths_at`, `kms_planes` of the debugfs state with `read_properties`, a `RegisterWatch` sample, whether a picture was being finished, the v3d busy share), then writes `moments.jsonl` and `verdict.json`.

**Acceptance (unit tier):** with fake ports, a dissolve presented at 60 frame times draws each still once and commits strengths that follow `plane_strength`; a dip's planes use the sRGB strength; a Position change (a new rectangle size), a new operations text, a turn and a new room-lights document each finish the still again; a covered layer keeps its picture; a video frame is drawn into a released target and calls `on_change`; a busy target is never drawn into; five full-width planes are cut to three and the cut layers are absent from the presented composition; no grant gives "pending" with no commit; a candidate grant commits no planes with the candidate tag; while a commit is in flight a present commits nothing. Bench, pure: `tracer` has the steps in order with the outgoing photo at full through each dissolve; `strengths_at` gives (1, s) mid-dissolve and `encode_srgb(s)` mid-dip; `kms_planes` parses a recorded state dump (copy `/Volumes/Dock/tmp/pi-render-bench/test5/raw/a/p-normal-t0/kms-state.txt` into a test fixture) merged with given properties; `glitched` sees a newly set 0xA00 bit only; `verdict` fails a moment with an alpha off by 2 levels, a missing plane, a glitch, or GPU use while nothing draws.

**Mutation probe:** drop the still's picture key check (finish on every present): the 60-frame dissolve test sees 60 draws instead of one and fails.

## Parallel groups

1. **B1, B2, B3, B4, B5** at once in one checkout: their owned files are disjoint, and no one's tests need another's bodies (B3 and B4 reach `gpu.Target` and `planes.Screen`, plain dataclasses already committed).
2. **B6** after group 1 lands (its tests compose with B1's real rules; its bench needs every body).

Then W2 runs the tracer on the Pi.

## The tracer (W2, on the test Pi)

**Preflight:** the Pi on PR #114's Weston 14.0.2-1+photowall1 (`/Volumes/Dock/tmp/weston-pi-test/debs/`) and this branch's `photo-wall-node-display`, `photo-wall-frame-client` and `photo-wall-player` (CI's `photo-wall-node-debs-<sha>` artifact); four 4K photos and a 1080p30 HEVC clip (`/Volumes/Dock/tmp/pi-render-bench/test4/clips/h1080p30.mp4`); `photo-wall-display-controller.service` and the Player stopped; reboot after, as every bench test.

**Passes when** `python -m scripts.render_bench ... --soak 600` exits 0: every expected picture on its own plane above the primary at every moment (including the turned screen: Weston did not composite); every plane's kernel alpha (its `alpha` property) within one level of its strength (the dip's sRGB strengths included) and its blend mode Pre-multiplied; no sticky glitch bit in the run; the GPU idle whenever nothing is being finished; the overload step cut to fit without a glitch. The PR gets the plane log, the verdict and the register record.

**Mutation probes (each run once, then restored):** (1) `plane_strength` returns 1: the verdict's alpha check fails in the dip. (2) `--budget unlimited`: the overload step's five full-width pictures reach the controller and the glitch bits set. (3) `pw_screen_present` sends buffer transform 0 on the turned screen: Weston composites the picture (no own plane) and the plane check fails.

## Test architecture

- **Pure, unit tier (macOS and CI):** the turn, placement, composition, strength and budget rules; the fit pass's scalar reference; `multiplier`; `sand_modifier`; the renderer through fakes of its five ports; the bench's timeline, parser and verdict. Each module is tested through its own interface; no test reaches past a port.
- **Display harness (CI `node-pid1` `display-harness`, Linux arm64 Docker, built debs):** the shell's window alpha, turn and amber (B2, the existing smoke); the frame client's screens through `PlaneScreens` under headless Weston (B3); the fit pass on software GL against the reference (B4). These are the conformance boundaries: the private shell-client contract and the shader-reference contract.
- **Pi only (W2):** planes, plane alpha in the kernel, the controller's glitch bits, GBM targets, the SAND128 import, the hardware decoder, the turn without a copy. Nothing of these runs in CI; the tracer's verdict is their proof.

## Maintenance risks

- **Two paths until S3.** `native.py` and the new path coexist for S1-S2; `look_shader.py` keeps one copy of the look meanwhile, and the ctypes declarations of the library's older functions exist in both `wayland_frames.py` and `plane_screen.py` until S3 deletes the GDK path. Do not grow either old module.
- **The shell's alpha-modifier implementation is ours** until Weston 16 (which implements the protocol upstream). Its state follows Weston's commit signal, which matches the protocol for how the Player uses it (multiplier set before the commit it goes with), not for every client; on a Weston upgrade, delete it rather than keep two globals.
- **Unproven on hardware:** Weston 14 putting synchronized sub-surfaces with view alpha on overlay planes (bench T1 was not run). The tracer is the first proof; if Weston composites them, that is an erratum against the design, not a bead fix.
- **The budget's two-port numbers are estimates** (Q6); a Pi with two 4K screens may cut a dissolve that would have fit.
- **Hardware decode is CI-blind.** A GStreamer or Mesa update can break the SAND128 path with CI green; only a Pi run sees it.

## Integration points with existing code

- `player/rendering.py`: `LocalLayer`, `OutputComposition`, `PrepareResult`, `PresentationResult` are reused unchanged; `PlaneRenderer` is not yet a `Renderer` (S3 drops `FrameOnScreen` once the binding carries the rectangle and wires it into `player/service.py`).
- `player/wayland_frames.py`: `CLIENT_LIBRARY`, `FrameGrant`, `composition_tag` reused; not edited.
- `player/visibility.py`, `player/look.py`, `player/luts.py`, `player/pacing.py`: reused as they are.
- `appliance/display_host/weston.py`: `WestonBackend` gains `turn`; the bench reuses the class as its controller.
- `scripts/run_display_harness.py`, `scripts/release_plan.py`: the two new harnesses are in the chain and the node-pid1 suite; `scripts/render_bench/**` is never shipped.
