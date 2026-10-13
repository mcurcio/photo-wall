"""1c R2 real-GL harness: the native renderer's pixels equal player/geometry.py's reference.

Run only inside the display harness container (scripts/run_display_harness.py), after
native_player_resource_harness.py, with `player` and `contracts` mounted read-only at /repo and
`tests` at /smoke. It starts its own headless Weston (kiosk shell, pixman) and drives the real
NativeRenderer on software GL, reading each render's pixels back with glReadPixels and comparing
them, within 0.015, to the scalar reference (`picture_source_uv`, `final_pixel`, and
`compose_pixel` with neutral operations). The display host is a fake `_frames` (one admitted
grant, the Output's picture operations, every tag recorded), as in 1b P1a case 4. Its picture is
a 64x48 PNG of four flat quadrants (red, green, blue, white) written here with the stdlib.

1. cover_at_<angle>_degrees_matches_the_reference (0, 7, 90): Cover through the one matrix, with
   non-neutral picture operations, equals the reference at sampled pixels; black outside. The
   binding's 16:9 profile sets both the cover rectangle's aspect and the photo's crop (one fit,
   owner 2026-10-11), so a renderer that sized either another way puts the quadrants off the
   reference's.
2. outside_the_opening_stays_black_at_low_contrast: contrast slope 0.5 lifts black inside the
   opening, and every sampled pixel outside the opening is exactly (0, 0, 0): the mask is last.
3. corners_fit_matches_the_reference: a skewed CornersFit at 90 degrees draws the reference's
   pixels (`picture_source_uv` of the same fit, `compose_pixel`).
4. operations_change_the_picture_and_the_tag: new operations with picture change 7 redraw at
   once, the pixels follow them, and the tag names change 7 (None before any operations).
5. no_trim_and_no_gain_uniforms: neither linked program has a `crop`, `gain`, `rotation`,
   `cover` or `inverse_map` uniform; the new ones are live.
6-8. Moved from tests/native/smoke.py (deleted; it ran in no CI job): alpha and a black layer
   compose as `compose_pixel`, a corrupt picture fails its prepare without stopping the Frame,
   and the enrollment diagnostic shows only while the Output is unbound. Its video assertions
   are not moved: this image's GStreamer cannot link H.264 pads yet (errata, the G8 finding (8)).
9. crop_offset_keeps_the_left_edge (roadmap 5a P1): the picture on a 9:16 binding profile in
   Cover at 0 degrees, its layer carrying `PhotoCrop(offset=0)`: sampled pixels equal the
   reference (`picture_source_uv` with that crop) and show only the left quadrants' colours
   (red, blue); with offset 1 only the right ones (green, white). A renderer that ignored the
   crop, or swapped its axes, draws the centre's four colours.
10. print_sliders_match_the_reference (print look, 2026-10-12, P1): saturation 120 %, brightness
   110 % with the highlight rolloff's knee at 0.4, a cool white balance, contrast 1.1 and a black floor 0.1 equal
   `final_pixel` (steps 2, 4, 6): the saturated red rolls off toward white, so its green and blue
   rise above the floor only through the rolloff's fade.
11. one_light_from_the_top_matches: the binding's Mat (8 mm deep, one soft light from the wall's
   up) shades a band under the top edge, equal to `final_pixel` with `mat_shade`'s shade.
12. cover_at_90_shades_the_wall_up_edge: the same light on a Frame turned 90 degrees shades the
   output's right edge (the picture's up), not its top.
13. neutral_program_takes_no_new_step: neutral v5 operations and Mat() leave every new step's
   switch (lut_on, saturation_on, rolloff_on, floor_on, mat_on) at 0 in the linked program, and
   the pixels are the v4 reference's.
14. trial_mat_drawn: while Position's trial is drawn its mat shades the picture, though the
   binding's Mat() casts none; when the trial ends the binding's (no shadow) is drawn again.
15. lut_matches_the_reference (slice P2, rule 3): a picture of four mid-tone quadrants whose
   operations name a LUT the Player does not hold yet is drawn at once with the sliders alone
   (lut_on 0, pixels = `final_pixel` without the LUT) and its tag never names the change; once the
   LUT is held (`PlayerLuts.hold`, as the service does after its digest-checked fetch) the surface
   draws again, the pixels equal `final_pixel` with the table (a steep, channel-turning one,
   mixed in at 0.8, so a half-texel sampling slip fails) and the tag names the change.
16. two_coloured_lights_match (R6, the owner's window): a cool light from the side (90 degrees,
   7500 K) and a warm one from the top (0 degrees, 3000 K) on the binding's Mat: every sample,
   the two bands and the corner both shade included, equals `final_pixel` with `mat_shade`'s
   shade, so the shader's loop past the first light and its uniform arrays (`light_toward`,
   `light_band`, `light_weight`) are held to the reference; the side band reads warmer than the
   top band, and the corner darker than both.
17-19. <pattern>_pattern_matches_the_reference (display tuning, decision 0024, slice PL1), for
   White, Grey ramp and Black level: with Position's trial grid on (a trial on a fallback
   composition with no layers) and its deep mat, a Cover fit turned 30 degrees and brightness
   80 % with a black floor, operations naming the pattern draw `final_pixel` with the opening's
   unit square (`pattern_signal`) at every sample clear of a step's or a cell's edge, black
   outside the opening: the look patterns shaded by the mat, the Black level raw. A renderer that
   never set `test_pattern` draws the grid instead.
20-23. target_<stage>_matches_the_reference[_portrait] (calibration targets, decision 0026, slice
   P3): on a 22-inch 4:3 Frame (447 x 335 mm, the whole screen target in view) with Position's
   deep mat, a brightness of 80 % and a black floor, Target: pre-software and post-software drawn
   in a Cover fit turned 30 degrees and in a portrait opening (Cover at 90, the target turned a
   quarter clockwise) equal `final_pixel` with the target (`target_signal`) at every sample clear
   of a patch's, a module's or the opening's edge and at every patch centre in view: pre to the
   exact code (raw everywhere), post within the tolerance (through the steps, never the mat), and
   the dark box raw black (0) under the floor in both.
24. the_target_qr_names_the_port: the pre-software target over the whole output; with no player
   id the QR's square is all light; once the renderer is given one, the pixel nearest every
   module's centre (the quiet zone included) is that module of `target_modules(TargetCode(player,
   "headless", 9))`, to the exact code. The modules decode with OpenCV (tests/
   test_player_calibration_target.py); this image has no OpenCV and its 640 x 480 output gives a
   module about 1.3 px, so the harness holds the modules, not a decode.
"""
import ctypes
import hashlib
import os
import pathlib
import struct
import subprocess
import sys
import time
import uuid
import zlib
from types import SimpleNamespace

sys.path[:0] = ["/repo", "/smoke"]
RUNTIME = pathlib.Path("/tmp/pw-pixels")
RUNTIME.mkdir(mode=0o700, exist_ok=True)
SOCKET = "wayland-pixels"
WESTON_LOG = pathlib.Path("/tmp/pw-pixels-weston.log")
PHOTO = RUNTIME / "quadrants.png"
os.environ.update(XDG_RUNTIME_DIR=str(RUNTIME), WAYLAND_DISPLAY=SOCKET,
                  GDK_BACKEND="wayland", LIBGL_ALWAYS_SOFTWARE="1", PYOPENGL_PLATFORM="egl",
                  GSETTINGS_BACKEND="memory")

from gi.repository import GLib  # noqa: E402

from contracts.calibration_target import (  # noqa: E402
    QR_MM,
    QR_QUIET_MODULES,
    SCREEN_TARGET_MM,
    Surface,
    TargetCode,
    calibration_target,
)
from contracts.lut import LUT_BYTES, LUT_GRID, LutRef, lut_offset  # noqa: E402
from contracts.models import (  # noqa: E402
    Calibration,
    CornersFit,
    CoverFit,
    FrameProfile,
    Layer,
    Light,
    Mat,
    OutputBinding,
    PhotoCrop,
    Variant,
)
from contracts.node_frame import LutMix, PictureOperations  # noqa: E402
from contracts.photo_crop import CENTRED  # noqa: E402
from contracts.tuning import TARGET_PATTERNS, TestPattern  # noqa: E402
from player.calibration_target import target_modules  # noqa: E402
from player.geometry import (  # noqa: E402
    TargetDraw,
    compose_pixel,
    decode_channel,
    encode_srgb,
    final_pixel,
    homography,
    inside_opening,
    inverse,
    mat_shade,
    mat_shadow,
    opening_size_mm,
    pattern_signal,
    picture_source_uv,
    target_signal,
    transform,
)
from player.luts import PlayerLuts  # noqa: E402
from player.native import NativeOutput, NativeRenderer  # noqa: E402
from player.rendering import LocalLayer, OutputComposition  # noqa: E402
from player.wayland_frames import FrameGrant  # noqa: E402

WIDTH, HEIGHT = 64, 48                      # the picture
TOLERANCE = 0.015
QUADRANTS = ((255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255), (255, 255, 255, 255))
PROFILE = FrameProfile(width_px=1600, height_px=900, diagonal_inches=10)   # 16:9 trims the photo
BINDING = OutputBinding(output_id="headless", frame_id="frame", generation=1, profile=PROFILE, mat=Mat())
OPENING = ((.1, .12), (.85, .08), (.9, .9), (.12, .82))
SMALL_OPENING = ((.2, .2), (.8, .25), (.75, .8), (.25, .75))
SKEWED = ((.1, .1), (.9, .2), (.8, .9), (.2, .8))
OPERATIONS = PictureOperations("headless", 3, (.9, .95, 1.0), 1.2, .8, 24 / 22)


def quadrant(x: int, y: int, palette=QUADRANTS) -> tuple[int, int, int, int]:
    return palette[(x >= WIDTH // 2) + 2 * (y >= HEIGHT // 2)]


def png_fixture(path: pathlib.Path = PHOTO, colour=quadrant) -> Variant:
    """A 64x48 RGBA PNG (stdlib zlib) of `colour(x, y)`, written once to `path`."""
    if not path.exists():
        rows = b"".join(b"\x00" + b"".join(bytes(colour(x, y)) for x in range(WIDTH))
                        for y in range(HEIGHT))

        def chunk(kind: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + kind + data
                    + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
        path.write_bytes(b"\x89PNG\r\n\x1a\n"
                         + chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 6, 0, 0, 0))
                         + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    return variant_of(path)


def variant_of(path: pathlib.Path) -> Variant:
    payload = path.read_bytes()
    return Variant(asset_id="asset-" + hashlib.sha256(payload).hexdigest()[:16], recipe_id="e" * 64, sha256=hashlib.sha256(payload).hexdigest(), size=len(payload),
                   media_type="image/png", width=WIDTH, height=HEIGHT)


def layer(assignment_id: str, path: pathlib.Path | None, variant: Variant | None,
          alpha: float = 1.0, crop: PhotoCrop = CENTRED) -> LocalLayer:
    """A picture layer, or with no variant a black one."""
    return LocalLayer(Layer(assignment_id=assignment_id, run_id="run", output_id="headless",
                            frame_id="frame", binding_generation=1, start=0, end=10**9,
                            media_origin=0, variant=variant,
                            presentation="media" if variant else "black",
                            crop=crop if variant else None),
                      path, 0.0, alpha)


def composition(variant: Variant, calibration: Calibration, *layers: LocalLayer,
                binding: OutputBinding = BINDING) -> OutputComposition:
    return OutputComposition(binding, calibration, layers or (layer("quadrants", PHOTO, variant),))


class Frames:
    """The display host faked: one admitted grant, the Output's picture operations, every tag."""

    def __init__(self):
        self.grant_ = FrameGrant(grant_id=uuid.uuid4(), binding_generation=BINDING.generation,
                                 config_revision=BINDING.configuration_revision,
                                 frame_id=BINDING.frame_id, admitted=True)
        self.operations: PictureOperations | None = None
        self.tags: list[tuple[str, int | None]] = []

    def grant(self, _output_id, _composition=None):
        return self.grant_

    def trial(self, _composition, _grant):
        return None

    def picture(self, _output_id):
        return self.operations

    def tag_rendered_buffer(self, _window, _grant, tag, _primitives=None, *, picture_change=None):
        self.tags.append((tag, picture_change))

    def close(self):
        pass


class CapturingRenderer(NativeRenderer):
    """The real renderer; after each render, the GTK framebuffer's pixels (rows bottom-up)."""
    captured: tuple[int, int, bytes] | None = None

    def _render(self, area, context, surface):
        result = super()._render(area, context, surface)
        gl = self.GL
        width = max(1, area.get_allocated_width() * area.get_scale_factor())
        height = max(1, area.get_allocated_height() * area.get_scale_factor())
        self.captured = (width, height, bytes(gl.glReadPixels(0, 0, width, height, gl.GL_RGBA,
                                                              gl.GL_UNSIGNED_BYTE)))
        return result


def spin(seconds: float) -> None:
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        while context.iteration(False):
            pass
        time.sleep(0.005)


def until(call, seconds: float = 8.0):
    """Spin the main context until `call()` is truthy or `seconds` pass; its last answer."""
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while True:
        answer = call()
        if answer or time.monotonic() >= end:
            return answer
        while context.iteration(False):
            pass
        time.sleep(0.005)


def renderer() -> tuple[CapturingRenderer, Frames]:
    native = CapturingRenderer((NativeOutput("headless", "photo-wall-headless", 640, 480),), luts=PlayerLuts())
    native.set_unbound_outputs((), None)
    frames = Frames()
    native._frames = frames
    spin(1.0)                               # the first render compiles the surface's programs
    return native, frames


def show(native: CapturingRenderer, shown: OutputComposition) -> None:
    presented = until(lambda: native.present(shown).status == "presented")
    assert presented, ("not_presented", native.present(shown), native.diagnostics())
    spin(0.2)


def pixel(native: CapturingRenderer, i: int, j: int) -> tuple[float, ...]:
    """Output pixel column i, row j from the top, as 0..1 RGBA."""
    width, height, data = native.captured
    offset = ((height - 1 - j) * width + i) * 4
    return tuple(value / 255 for value in data[offset:offset + 4])


def samples(native: CapturingRenderer, corners, step: int = 7):
    """Pixel centres (i, j, x, y) clear of the opening's edges (2 px either side)."""
    width, height, _ = native.captured
    for j in range(step // 2, height, step):
        for i in range(step // 2, width, step):
            x, y = (i + .5) / width, (j + .5) / height
            near = {inside_opening(corners, x + dx / width, y + dy / height)
                    for dx in (-2, 0, 2) for dy in (-2, 0, 2)}
            if len(near) == 1:
                yield i, j, x, y


def texel_colour(u: float, v: float, palette=QUADRANTS) -> tuple[float, float, float] | None:
    """The picture's linear colour at source UV (u, v); None within 2 texels of a quadrant seam
    (where linear filtering blends two quadrants)."""
    tx, ty = u * WIDTH, v * HEIGHT
    if abs(tx - WIDTH / 2) < 2 or abs(ty - HEIGHT / 2) < 2:
        return None
    rgba = quadrant(min(WIDTH - 1, int(tx)), min(HEIGHT - 1, int(ty)), palette)
    return tuple(decode_channel(value / 255) for value in rgba[:3])


def close(actual, expected) -> bool:
    return max(abs(a - b) for a, b in zip(actual, expected, strict=True)) <= TOLERANCE


def exact(actual, expected) -> bool:
    """The same 8-bit code in every channel: the Black level's raw codes 1..20 (and 0 around them)
    are its reading (Full keeps them apart, Limited merges pairs), so no tolerance hides an offset,
    a rounding or a dither."""
    return all(round(a * 255) == round(b * 255) for a, b in zip(actual, expected, strict=True))


def check_fit(native: CapturingRenderer, fit, operations: PictureOperations | None, *,
              profile: FrameProfile = PROFILE, crop: PhotoCrop = CENTRED,
              mat: Mat = Mat(), palette=QUADRANTS, lut: bytes | None = None) -> tuple[int, int]:
    """Every sample equals the reference: inside, the picture through `final_pixel` (shaded by
    `mat`'s shadow on this fit); outside, opaque black. The (inside, outside) sample counts."""
    shadow = mat_shadow(fit, profile, mat)
    width, height, _ = native.captured
    applied = operations or PictureOperations("headless", 1, (1.0, 1.0, 1.0), 1.0, 1.0, 1.0)
    counts = [0, 0]
    for i, j, x, y in samples(native, fit.corners):
        uv = picture_source_uv(x, y, fit, (width, height), profile.width_px / profile.height_px,
                               WIDTH, HEIGHT, crop)
        if uv is None:
            expected = final_pixel((0.0, 0.0, 0.0), applied, inside=False)
            counts[1] += 1
        else:
            linear = texel_colour(*uv, palette)
            if linear is None:
                continue
            shade = None if shadow is None else mat_shade(shadow, x, y)
            expected = final_pixel(linear, applied, inside=True, shade=shade, lut=lut)
            counts[0] += 1
        actual = pixel(native, i, j)
        assert close(actual, expected), ("pixel", fit.mode, fit.rotation, i, j, actual, expected)
    return counts[0], counts[1]


def cover_matches_the_reference(angle: float):
    def case(native: CapturingRenderer, frames: Frames, variant: Variant) -> None:
        frames.operations = OPERATIONS
        fit = CoverFit(corners=OPENING, rotation=angle)
        show(native, composition(variant, Calibration(fit=fit)))
        inside, outside = check_fit(native, fit, OPERATIONS)
        print(f"COVER {angle} inside={inside} outside={outside}", flush=True)
        assert inside > 500 and outside > 100, (inside, outside)
    case.__name__ = f"cover_at_{angle}_degrees_matches_the_reference"
    return case


def outside_the_opening_stays_black_at_low_contrast(native, frames, variant) -> None:
    low = PictureOperations("headless", 4, (1.0, 1.0, 1.0), 1.0, .5, 1.0)
    frames.operations = low
    fit = CoverFit(corners=SMALL_OPENING, rotation=7)
    show(native, composition(variant, Calibration(fit=fit)))
    until(lambda: frames.tags and frames.tags[-1][1] == 4)
    width, height, data = native.captured
    outside = 0
    for i, j, x, y in samples(native, fit.corners, step=5):
        if not inside_opening(fit.corners, x, y):
            outside += 1
            offset = ((height - 1 - j) * width + i) * 4
            assert data[offset:offset + 3] == b"\x00\x00\x00", ("not_black", i, j, data[offset:offset + 4])
    inside, _ = check_fit(native, fit, low)
    print(f"LOW_CONTRAST outside={outside} inside={inside}", flush=True)
    assert outside > 1000 and inside > 200, (outside, inside)


def corners_fit_matches_the_reference(native, frames, variant) -> None:
    frames.operations = None
    fit = CornersFit(corners=SKEWED, rotation=90)
    show(native, composition(variant, Calibration(fit=fit)))
    width, height, _ = native.captured
    compared = 0
    for i, j, x, y in samples(native, SKEWED):
        uv = picture_source_uv(x, y, fit, (width, height), PROFILE.width_px / PROFILE.height_px, WIDTH, HEIGHT,
                               CENTRED)
        if uv is None:
            expected = compose_pixel([])
        else:
            if texel_colour(*uv) is None:
                continue
            rgba = quadrant(min(WIDTH - 1, int(uv[0] * WIDTH)), min(HEIGHT - 1, int(uv[1] * HEIGHT)))
            expected = compose_pixel([(tuple(value / 255 for value in rgba), 1, False)])
        actual = pixel(native, i, j)
        assert close(actual, expected), ("corners_pixel", i, j, actual, expected)
        compared += 1
    print(f"CORNERS compared={compared}", flush=True)
    assert compared > 600, compared


def operations_change_the_picture_and_the_tag(native, frames, variant) -> None:
    frames.operations = None
    fit = CoverFit(corners=OPENING)
    shown = composition(variant, Calibration(fit=fit))
    show(native, shown)
    assert frames.tags and frames.tags[-1][1] is None, ("tag_before_operations", frames.tags[-3:])
    check_fit(native, fit, None)
    changed = PictureOperations("headless", 7, (1.0, .9, .8), .5, 1.0, 1.0)
    frames.operations = changed
    native.present(shown)
    assert until(lambda: frames.tags and frames.tags[-1][1] == 7), ("tag_after", frames.tags[-3:])
    inside, _ = check_fit(native, fit, changed)
    print(f"OPERATIONS tag={frames.tags[-1]} inside={inside}", flush=True)
    assert inside > 500, inside


def no_trim_and_no_gain_uniforms(native, frames, variant) -> None:
    show(native, composition(variant, Calibration(fit=CoverFit())))
    surface = native._surfaces["headless"]
    surface.area.make_current()
    gl = native.GL
    layer, final = surface.programs
    for program in (layer, final):
        for name in ("crop", "gain", "rotation", "cover", "inverse_map"):
            assert gl.glGetUniformLocation(program, name) == -1, ("retired_uniform", program, name)
    for program, name in ((layer, "output_to_picture"), (layer, "photo"), (layer, "opening"),
                          (final, "rgb_multipliers"), (final, "light_factor"),
                          (final, "contrast_slope"), (final, "gamma_exponent"),
                          (final, "opening")):
        assert gl.glGetUniformLocation(program, name) != -1, ("missing_uniform", name)


def centre(native: CapturingRenderer) -> tuple[float, ...]:
    width, height, _ = native.captured
    return pixel(native, width // 2, height // 2)


def alpha_and_black_layers_compose_as_the_reference(native, frames, variant) -> None:
    """Moved from tests/native/smoke.py: straight alpha and a black layer's fade compose in linear
    premultiplied source-over, as `compose_pixel` (the default position, Cover at angle 0 on the
    whole output, covers the centre)."""
    frames.operations = None
    red = png_fixture(RUNTIME / "red.png", lambda _x, _y: (255, 0, 0, 128))
    white = png_fixture(RUNTIME / "white.png", lambda _x, _y: (255, 255, 255, 255))
    show(native, composition(variant, Calibration(), layer("red", RUNTIME / "red.png", red)))
    expected = compose_pixel([((1, 0, 0, 128 / 255), 1, False)])
    assert close(centre(native), expected), ("alpha", centre(native), expected)
    native.release("red")
    show(native, composition(variant, Calibration(), layer("white", RUNTIME / "white.png", white),
                             layer("black", None, None, .5)))
    expected = compose_pixel([((1, 1, 1, 1), 1, False), ((0, 0, 0, 1), .5, False)])
    assert close(centre(native), expected), ("black_layer", centre(native), expected)


def a_corrupt_picture_fails_and_the_frame_still_presents(native, frames, variant) -> None:
    """Moved from tests/native/smoke.py: a picture that does not decode fails its prepare; the
    Frame's other composition still presents."""
    corrupt_path = RUNTIME / "corrupt.png"
    corrupt_path.write_bytes(b"not a PNG")
    corrupt = layer("corrupt", corrupt_path, variant_of(corrupt_path))
    assert until(lambda: native.prepare(corrupt).status == "failed"), native.prepare(corrupt)
    show(native, composition(variant, Calibration(fit=CoverFit())))
    native.release("corrupt")


def the_diagnostic_shows_only_while_the_output_is_unbound(native, frames, variant) -> None:
    """Moved from tests/native/smoke.py, on real GTK widgets."""
    surface = native._surfaces["headless"]
    native.set_unbound_outputs(("headless",), "p-" + "a" * 32)
    assert surface.diagnostic.get_visible()
    assert "configuration received" in surface.diagnostic_label.get_text()
    native.set_unbound_outputs((), "p-" + "a" * 32)
    assert not surface.diagnostic.get_visible()


TALL = FrameProfile(width_px=900, height_px=1600, diagonal_inches=10)     # 9:16 cuts the sides
TALL_BINDING = OutputBinding(output_id="headless", frame_id="frame", generation=1, profile=TALL, mat=Mat())


def crop_offset_keeps_the_left_edge(native, frames, variant) -> None:
    """Roadmap 5a P1: the layer's crop picks the window. The 4:3 picture on a 9:16 Frame keeps
    (9/16)/(4/3) of its width: offset 0 its left part (red over blue), offset 1 its right part
    (green over white). Each sampled pixel is named by its nearest quadrant colour."""
    frames.operations = None
    fit = CoverFit(corners=OPENING)
    sides = {0: {0, 2}, 1: {1, 3}}          # indices into QUADRANTS
    for offset, colours in sides.items():
        crop = PhotoCrop(offset=offset)
        show(native, composition(variant, Calibration(fit=fit),
                                 layer(f"crop{offset}", PHOTO, variant, crop=crop),
                                 binding=TALL_BINDING))
        inside, _ = check_fit(native, fit, None, profile=TALL, crop=crop)
        width, height, _ = native.captured
        seen = set()
        for i, j, x, y in samples(native, fit.corners):
            uv = picture_source_uv(x, y, fit, (width, height), TALL.width_px / TALL.height_px,
                                   WIDTH, HEIGHT, crop)
            if uv is None or texel_colour(*uv) is None:
                continue
            actual = pixel(native, i, j)[:3]
            seen.add(min(range(4), key=lambda index: sum(
                abs(a - c / 255) for a, c in zip(actual, QUADRANTS[index][:3], strict=True))))
        print(f"CROP offset={offset} inside={inside} colours={sorted(seen)}", flush=True)
        assert inside > 300, (offset, inside)
        assert seen == colours, ("crop_colours", offset, sorted(seen), sorted(colours))
        native.release(f"crop{offset}")


# -- The print look (design /print-look, 2026-10-12, slice P1) ------------------------------------

PRINT = PictureOperations("headless", 5, (.9, .95, 1.0), 1.1, 1.1, 1.0, saturation=1.2,
                          highlight_knee=.4, black_floor=.1)
TOP_LIGHT = Light(from_degrees=0, steep_degrees=30, softness_degrees=20, strength_tenths=15,
                  colour_kelvin=3000)
DEEP_MAT = Mat(depth_mm=8, lights=(TOP_LIGHT,))      # a band of ~10..22 mm: tens of pixels here
COOL_SIDE = Light(from_degrees=90, steep_degrees=30, softness_degrees=20, strength_tenths=15,
                  colour_kelvin=7500)
WINDOW_MAT = Mat(depth_mm=8, lights=(COOL_SIDE, TOP_LIGHT))
TRIAL_SHA = "a" * 64


def print_sliders_match_the_reference(native, frames, variant) -> None:
    frames.operations = PRINT
    fit = CoverFit(corners=OPENING)
    show(native, composition(variant, Calibration(fit=fit)))
    until(lambda: frames.tags and frames.tags[-1][1] == 5)
    inside, outside = check_fit(native, fit, PRINT)
    red = final_pixel(tuple(decode_channel(v / 255) for v in QUADRANTS[0][:3]), PRINT, inside=True)
    print(f"PRINT inside={inside} outside={outside} red={red}", flush=True)
    assert red[1] > encode_srgb(PRINT.black_floor) + .05, red   # the fade toward white lifts green
    assert inside > 500 and outside > 100, (inside, outside)


def shaded_binding(mat: Mat) -> OutputBinding:
    return BINDING.model_copy(update={"mat": mat})


def one_light_from_the_top_matches(native, frames, variant) -> None:
    frames.operations = None
    fit = CoverFit(corners=OPENING)
    show(native, composition(variant, Calibration(fit=fit), binding=shaded_binding(DEEP_MAT)))
    inside, _ = check_fit(native, fit, None, mat=DEEP_MAT)
    shadow = mat_shadow(fit, PROFILE, DEEP_MAT)
    top, middle = mat_shade(shadow, .5, .12), mat_shade(shadow, .5, .5)
    print(f"ONE_LIGHT inside={inside} top={top} middle={middle}", flush=True)
    assert top[0] < .8 and middle == (1.0, 1.0, 1.0), (top, middle)
    assert inside > 500, inside


def cover_at_90_shades_the_wall_up_edge(native, frames, variant) -> None:
    frames.operations = None
    fit = CoverFit(corners=OPENING, rotation=90)
    show(native, composition(variant, Calibration(fit=fit), binding=shaded_binding(DEEP_MAT)))
    inside, _ = check_fit(native, fit, None, mat=DEEP_MAT)
    shadow = mat_shadow(fit, PROFILE, DEEP_MAT)
    right, top = mat_shade(shadow, .86, .5), mat_shade(shadow, .5, .12)
    print(f"TURNED inside={inside} right={right} top={top}", flush=True)
    assert right[0] < .8 and top == (1.0, 1.0, 1.0), (right, top)
    assert inside > 500, inside


def two_coloured_lights_match(native, frames, variant) -> None:
    frames.operations = None
    fit = CoverFit(corners=OPENING)
    show(native, composition(variant, Calibration(fit=fit), binding=shaded_binding(WINDOW_MAT)))
    inside, _ = check_fit(native, fit, None, mat=WINDOW_MAT)
    shadow = mat_shadow(fit, PROFILE, WINDOW_MAT)
    side, top, corner = mat_shade(shadow, .86, .5), mat_shade(shadow, .5, .12), mat_shade(shadow, .86, .12)
    print(f"TWO_LIGHTS inside={inside} side={side} top={top} corner={corner}", flush=True)
    assert len(shadow.lights) == 2
    assert side[0] / side[2] > 1 > top[0] / top[2], (side, top)
    assert all(c < min(s, t) for c, s, t in zip(corner, side, top, strict=True)), (corner, side, top)
    assert inside > 500, inside


def neutral_program_takes_no_new_step(native, frames, variant) -> None:
    neutral = PictureOperations("headless", 6, (1.0, 1.0, 1.0), 1.0, 1.0, 1.0)
    frames.operations = neutral
    fit = CoverFit(corners=OPENING)
    show(native, composition(variant, Calibration(fit=fit)))
    until(lambda: frames.tags and frames.tags[-1][1] == 6)
    check_fit(native, fit, neutral)
    surface = native._surfaces["headless"]
    surface.area.make_current()
    gl = native.GL
    final = surface.programs[1]
    switches = {name: switch(native, name)
                for name in ("lut_on", "saturation_on", "rolloff_on", "floor_on", "mat_on")}
    print(f"NEUTRAL switches={switches}", flush=True)
    assert switches == dict.fromkeys(switches, 0), switches
    for name in ("lut", "lut_amount", "saturation", "highlight_knee", "peak", "black_floor", "to_opening", "mat_size",
                 "light_count", "light_toward", "light_band", "light_weight"):
        assert gl.glGetUniformLocation(final, name) != -1, ("missing_uniform", name)


class TrialFrames(Frames):
    """The display host with Position's trial drawn: its calibration and its draft mat."""

    def __init__(self, fit, mat: Mat):
        super().__init__()
        self.active = SimpleNamespace(
            candidate=SimpleNamespace(trial_id=uuid.uuid4(), generation=1, sequence=1,
                                      candidate_sha256=TRIAL_SHA),
            calibration=Calibration(fit=fit), primitives=fit.corners, mat=mat)

    def trial(self, _composition, _grant):
        return self.active


def trial_mat_drawn(native, frames, variant) -> None:
    fit = CoverFit(corners=OPENING)
    trial = TrialFrames(fit, DEEP_MAT)
    native._frames = trial
    shown = composition(variant, Calibration(fit=fit))       # the binding's mat is Mat()
    show(native, shown)
    assert until(lambda: trial.tags and trial.tags[-1][0] == "trial-" + TRIAL_SHA), trial.tags[-3:]
    spin(0.3)
    during, _ = check_fit(native, fit, None, mat=DEEP_MAT)
    trial.active = None
    native.present(shown)
    assert until(lambda: trial.tags and not trial.tags[-1][0].startswith("trial-")), trial.tags[-3:]
    spin(0.3)
    after, _ = check_fit(native, fit, None)
    print(f"TRIAL_MAT during={during} after={after}", flush=True)
    assert during > 500 and after > 500, (during, after)


MIDTONES = ((40, 90, 200, 255), (230, 140, 20, 255), (120, 200, 60, 255), (180, 60, 150, 255))
LUT_REF = LutRef("cd" * 32, "lut33-perceptual-relcol-bpc-1")


def steep(index: int) -> int:
    """Black up to encoded 0.75, then four times the identity's slope to white: a sampling slip
    of half a texel (where the grid's points sit) moves the bright quadrants' colour by ~0.04,
    past the tolerance. (On an identity table the slip is at most 1/64, under it.)"""
    return round(255 * min(1.0, max(0.0, 4 * index / (LUT_GRID - 1) - 3)))


def turning_table() -> bytes:
    """contracts/lut.py's layout: grid point (r, g, b) holds (steep(g), steep(b), steep(r))."""
    body = bytearray(LUT_BYTES)
    for r in range(LUT_GRID):
        for g in range(LUT_GRID):
            for b in range(LUT_GRID):
                offset = lut_offset(r, g, b)
                body[offset:offset + 3] = bytes((steep(g), steep(b), steep(r)))
    return bytes(body)


def switch(native: CapturingRenderer, name: str) -> int:
    surface = native._surfaces["headless"]
    surface.area.make_current()
    gl = native.GL
    value = (ctypes.c_int * 1)()
    gl.glGetUniformiv(surface.programs[1], gl.glGetUniformLocation(surface.programs[1], name), value)
    return value[0]


def lut_matches_the_reference(native, frames, variant) -> None:
    luts = PlayerLuts()
    native._luts = luts
    picture = png_fixture(RUNTIME / "midtones.png", lambda x, y: quadrant(x, y, MIDTONES))
    looked = PictureOperations("headless", 8, (1.0, 1.0, 1.0), 1.0, 1.0, 1.0, saturation=1.1,
                               black_floor=.02, lut=LutMix(LUT_REF, .8))
    frames.operations = looked
    fit = CoverFit(corners=OPENING)
    shown = composition(picture, Calibration(fit=fit), layer("midtones", RUNTIME / "midtones.png", picture))
    show(native, shown)
    spin(0.3)
    before, _ = check_fit(native, fit, looked, palette=MIDTONES)
    assert luts.wanted() == (LUT_REF,), luts.wanted()
    assert switch(native, "lut_on") == 0
    assert frames.tags and all(change is None for _, change in frames.tags), ("claimed_early", frames.tags[-3:])
    table = turning_table()
    luts.hold(LUT_REF, table)
    native.present(shown)
    assert until(lambda: frames.tags and frames.tags[-1][1] == 8), ("tag_after_hold", frames.tags[-3:])
    spin(0.2)
    after, _ = check_fit(native, fit, looked, palette=MIDTONES, lut=table)
    assert switch(native, "lut_on") == 1
    print(f"LUT before={before} after={after}", flush=True)
    assert before > 500 and after > 500, (before, after)


def pattern_matches_the_reference(pattern: TestPattern, mat: Mat = DEEP_MAT):
    """The pattern drawn as the scalar reference: inside the opening (turned with the Frame), black
    outside. With DEEP_MAT the opening's square comes from the mat's shadow; with Mat() (no shading
    mat, the common Frame) from the opening alone. The Black level to the exact code."""
    def case(native: CapturingRenderer, frames: Frames, variant: Variant) -> None:
        fit = CoverFit(corners=OPENING, rotation=30)
        trial = TrialFrames(fit, mat)
        trial.operations = PictureOperations("headless", 9, (1.0, 1.0, 1.0), .8, 1.0, 1.0,
                                             black_floor=.05, test_pattern=pattern)
        native._frames = trial
        show(native, OutputComposition(BINDING, Calibration(fit=fit), (), True))    # the grid's canvas
        assert until(lambda: trial.tags and trial.tags[-1][0] == "trial-" + TRIAL_SHA), trial.tags[-3:]
        spin(0.3)
        width, height, _ = native.captured
        to_opening = inverse(homography(fit.corners))
        shadow = mat_shadow(fit, PROFILE, mat)
        same = exact if pattern is TestPattern.BLACK_LEVEL else close
        counts = [0, 0, 0]       # inside, outside, lit
        for i, j, x, y in samples(native, fit.corners, step=5):
            u, v = transform(to_opening, x, y)
            inside = inside_opening(fit.corners, x, y)
            around = [transform(to_opening, x + dx / width, y + dy / height)
                      for dx in (-1.5, 0, 1.5) for dy in (-1.5, 0, 1.5)]
            near = {pattern_signal(pattern, *point) for point in around}
            # the ramp's smooth half varies by design: compared wherever all of it is that half
            smooth = pattern is TestPattern.GREY_RAMP and min(point[1] for point in around) >= .5
            if inside and not smooth and len(near) != 1:
                continue        # a step's or a cell's edge
            shade = None if shadow is None else mat_shade(shadow, x, y)
            expected = final_pixel((0.0, 0.0, 0.0), trial.operations, inside=inside, shade=shade,
                                   opening_uv=(u, v))
            actual = pixel(native, i, j)
            counts[0 if inside else 1] += 1
            counts[2] += inside and max(expected[:3]) > 0
            assert same(actual, expected), ("pattern_pixel", pattern.value, i, j, actual, expected)
        print(f"PATTERN {pattern.value} mat={shadow is not None} inside={counts[0]} outside={counts[1]} "
              f"lit={counts[2]}", flush=True)
        assert counts[0] > 500 and counts[1] > 100 and counts[2] > 20, counts
    suffix = "" if mat is DEEP_MAT else "_without_a_mat"
    case.__name__ = f"{pattern.name.lower()}_pattern_matches_the_reference{suffix}"
    return case


# -- The calibration targets (decision 0026, slice P3) ---------------------------------------------

TARGET_PROFILE = FrameProfile(width_px=1600, height_px=1200, diagonal_inches=22)    # 447 x 335 mm
TARGET_BINDING = BINDING.model_copy(update={"profile": TARGET_PROFILE})
HARNESS_PLAYER = "p-" + "0123456789abcdef" * 2
TARGET_CODE = TargetCode(HARNESS_PLAYER, "headless", 9)


def show_target(native: CapturingRenderer, pattern: TestPattern, fit, mat: Mat) -> TrialFrames:
    """The target drawn over Position's trial grid on TARGET_BINDING, brightness 80 % and a floor."""
    trial = TrialFrames(fit, mat)
    trial.operations = PictureOperations("headless", 9, (1.0, .95, .9), .8, 1.1, 1.0,
                                         black_floor=.05, test_pattern=pattern)
    native._frames = trial
    show(native, OutputComposition(TARGET_BINDING, Calibration(fit=fit), (), True))
    assert until(lambda: trial.tags and trial.tags[-1][0] == "trial-" + TRIAL_SHA), trial.tags[-3:]
    spin(0.3)
    return trial


def target_output_point(x_mm: float, y_mm: float, fit, opening_mm) -> tuple[float, float]:
    """Where on the output (fractions) the target's (x_mm, y_mm) is drawn: `target_point` undone,
    then the opening's corners."""
    width, height = opening_mm
    dx, dy = x_mm - SCREEN_TARGET_MM[0] / 2, y_mm - SCREEN_TARGET_MM[1] / 2
    u, v = (dx / width + .5, dy / height + .5) if width >= height else (.5 - dy / width, dx / height + .5)
    return transform(homography(fit.corners), u, v)


def target_matches_the_reference(pattern: TestPattern, rotation: float):
    def case(native: CapturingRenderer, frames: Frames, variant: Variant) -> None:
        native.set_player(HARNESS_PLAYER)
        fit = CoverFit(corners=OPENING, rotation=rotation)
        trial = show_target(native, pattern, fit, DEEP_MAT)
        width, height, _ = native.captured
        draw = TargetDraw(opening_size_mm(fit, TARGET_PROFILE), target_modules(TARGET_CODE))
        to_opening = inverse(homography(fit.corners))
        shadow = mat_shadow(fit, TARGET_PROFILE, DEEP_MAT)
        same = exact if pattern is TestPattern.TARGET_PRE_SOFTWARE else close

        def expected_at(x: float, y: float):
            return final_pixel((0.0, 0.0, 0.0), trial.operations, inside=inside_opening(fit.corners, x, y),
                               shade=mat_shade(shadow, x, y), opening_uv=transform(to_opening, x, y), target=draw)

        counts = [0, 0, 0, 0]       # inside, outside, the dark box's raw black, patch centres
        for i, j, x, y in samples(native, fit.corners, step=5):
            near = {target_signal(pattern, *transform(to_opening, x + dx / width, y + dy / height), draw)
                    for dx in (-1.5, 0, 1.5) for dy in (-1.5, 0, 1.5)}
            if len(near) != 1:
                continue        # a patch's or a module's edge
            inside = inside_opening(fit.corners, x, y)
            expected, actual = expected_at(x, y), pixel(native, i, j)
            assert same(actual, expected), ("target_pixel", pattern.value, rotation, i, j, actual, expected)
            counts[0 if inside else 1] += 1
            box = inside and near == {((0.0, 0.0, 0.0), True)} and pattern is TestPattern.TARGET_POST_SOFTWARE
            if box or (inside and pattern is TestPattern.TARGET_PRE_SOFTWARE and expected[:3] == (0.0,) * 3):
                assert actual[:3] == (0.0, 0.0, 0.0), ("raw_black", i, j, actual)
                counts[2] += 1
        for patch in (p for g in calibration_target().groups for p in g.patches if p.surface is Surface.SCREEN):
            x, y = target_output_point(*patch.rect.centre(), fit, draw.opening_mm)
            i, j = int(x * width), int(y * height)
            if not all(inside_opening(fit.corners, (i + .5 + dx) / width, (j + .5 + dy) / height)
                       for dx in (-2, 2) for dy in (-2, 2)):
                continue
            expected, actual = expected_at((i + .5) / width, (j + .5) / height), pixel(native, i, j)
            assert same(actual, expected), ("patch_centre", pattern.value, rotation, patch.role.value, i, j,
                                            actual, expected)
            counts[3] += 1
        print(f"TARGET {pattern.value} rotation={rotation} inside={counts[0]} outside={counts[1]} "
              f"raw_black={counts[2]} patches={counts[3]}", flush=True)
        assert counts[0] > 500 and counts[1] > 100 and counts[2] > 100 and counts[3] > 40, counts
    turn = "" if rotation % 180 < 45 else "_portrait"
    case.__name__ = f"{pattern.name.lower()}_matches_the_reference{turn}"
    return case


def the_target_qr_names_the_port(native, frames, variant) -> None:
    fit = CoverFit()            # the whole output: about 1.4 px a mm, a module about 1.3 px
    trial = show_target(native, TestPattern.TARGET_PRE_SOFTWARE, fit, Mat())
    width, height, _ = native.captured
    modules = target_modules(TARGET_CODE)
    draw = TargetDraw(opening_size_mm(fit, TARGET_PROFILE), modules)
    side = len(modules) + 2 * QR_QUIET_MODULES
    square = calibration_target().qr
    centres = []                # (i, j, the module's signal), the pixel nearest each module's centre
    for row in range(side):
        for column in range(side):
            r, c = row - QR_QUIET_MODULES, column - QR_QUIET_MODULES
            dark = 0 <= r < len(modules) and 0 <= c < len(modules) and modules[r][c]
            x, y = target_output_point(square.x_mm + (column + .5) * QR_MM / side,
                                       square.y_mm + (row + .5) * QR_MM / side, fit, draw.opening_mm)
            i, j = int(x * width), int(y * height)
            # the pixel lies in its module (the reference agrees), so a slip of one module fails
            signal, _ = target_signal(trial.operations.test_pattern,
                                      *transform(inverse(homography(fit.corners)), (i + .5) / width, (j + .5) / height),
                                      draw)
            assert signal == ((0.0 if dark else 1.0),) * 3, ("module_pixel", row, column, i, j, signal)
            centres.append((i, j, 0.0 if dark else 1.0))
    unnamed = [pixel(native, i, j) for i, j, _ in centres]
    assert all(exact(actual, (1.0, 1.0, 1.0, 1.0)) for actual in unnamed), "a QR drawn with no player id"
    first_dark = next((i, j) for i, j, value in centres if value == 0.0)
    native.set_player(HARNESS_PLAYER)
    assert until(lambda: not exact(pixel(native, *first_dark), (1.0, 1.0, 1.0, 1.0)), 3), "no redraw on set_player"
    spin(0.2)
    dark = 0
    for i, j, value in centres:
        actual = pixel(native, i, j)
        assert exact(actual, (value, value, value, 1.0)), ("qr_module", i, j, actual, value)
        dark += value == 0.0
    print(f"QR modules={len(modules)} centres={len(centres)} dark={dark}", flush=True)
    assert len(modules) >= 21 and dark > 100, (len(modules), dark)


def pattern_cases(pattern: TestPattern):
    """Every pattern's cases: a calibration target in two fits, any other through the opening."""
    if pattern in TARGET_PATTERNS:
        return (target_matches_the_reference(pattern, 30), target_matches_the_reference(pattern, 90))
    return (pattern_matches_the_reference(pattern),)


CASES = (cover_matches_the_reference(0), cover_matches_the_reference(7),
         cover_matches_the_reference(90), outside_the_opening_stays_black_at_low_contrast,
         corners_fit_matches_the_reference, operations_change_the_picture_and_the_tag,
         no_trim_and_no_gain_uniforms, alpha_and_black_layers_compose_as_the_reference,
         a_corrupt_picture_fails_and_the_frame_still_presents,
         the_diagnostic_shows_only_while_the_output_is_unbound, crop_offset_keeps_the_left_edge,
         print_sliders_match_the_reference, one_light_from_the_top_matches,
         cover_at_90_shades_the_wall_up_edge, neutral_program_takes_no_new_step, trial_mat_drawn,
         lut_matches_the_reference, two_coloured_lights_match,
         *(case for pattern in TestPattern for case in pattern_cases(pattern)),
         pattern_matches_the_reference(TestPattern.BLACK_LEVEL, Mat()), the_target_qr_names_the_port)


def main() -> None:
    variant = png_fixture()
    with WESTON_LOG.open("w") as log:
        weston = subprocess.Popen(
            ["weston", "--backend=headless-backend.so", "--renderer=pixman",
             "--shell=kiosk-shell.so", "--width=640", "--height=480", "--idle-time=0",
             f"--socket={SOCKET}", "--no-config"],
            stdout=log, stderr=log)
        try:
            for _ in range(100):
                if (RUNTIME / SOCKET).exists():
                    break
                time.sleep(0.05)
            failed = []
            for case in CASES:
                native, frames = renderer()     # each case on its own surface: one red stays one red
                try:
                    case(native, frames, variant)
                    print(f"PASS {case.__name__}", flush=True)
                except AssertionError as error:
                    failed.append(case.__name__)
                    print(f"FAIL {case.__name__}: {error!r}"[:4000], flush=True)
                finally:
                    native._frames = None
                    native.close()
                    spin(0.2)
            assert not failed, ("failed", failed)
        finally:
            weston.terminate()
            try:
                weston.wait(timeout=3)
            except subprocess.TimeoutExpired:
                weston.kill()
                weston.wait()


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        if WESTON_LOG.exists():
            print(WESTON_LOG.read_text(errors="replace"), flush=True)
        raise
