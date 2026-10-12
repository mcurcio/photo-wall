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
"""
import hashlib
import os
import pathlib
import struct
import subprocess
import sys
import time
import uuid
import zlib

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

from contracts.models import (  # noqa: E402
    Calibration,
    CornersFit,
    CoverFit,
    FrameProfile,
    Layer,
    OutputBinding,
    PhotoCrop,
    Variant,
)
from contracts.node_frame import PictureOperations  # noqa: E402
from contracts.photo_crop import CENTRED  # noqa: E402
from player.geometry import (  # noqa: E402
    compose_pixel,
    decode_channel,
    final_pixel,
    inside_opening,
    picture_source_uv,
)
from player.native import NativeOutput, NativeRenderer  # noqa: E402
from player.rendering import LocalLayer, OutputComposition  # noqa: E402
from player.wayland_frames import FrameGrant  # noqa: E402

WIDTH, HEIGHT = 64, 48                      # the picture
TOLERANCE = 0.015
QUADRANTS = ((255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255), (255, 255, 255, 255))
PROFILE = FrameProfile(width_px=1600, height_px=900, diagonal_inches=10)   # 16:9 trims the photo
BINDING = OutputBinding(output_id="headless", frame_id="frame", generation=1, profile=PROFILE)
OPENING = ((.1, .12), (.85, .08), (.9, .9), (.12, .82))
SMALL_OPENING = ((.2, .2), (.8, .25), (.75, .8), (.25, .75))
SKEWED = ((.1, .1), (.9, .2), (.8, .9), (.2, .8))
OPERATIONS = PictureOperations("headless", 3, (.9, .95, 1.0), 1.2, .8, 24 / 22)


def quadrant(x: int, y: int) -> tuple[int, int, int, int]:
    return QUADRANTS[(x >= WIDTH // 2) + 2 * (y >= HEIGHT // 2)]


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
    native = CapturingRenderer((NativeOutput("headless", "photo-wall-headless", 640, 480),))
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


def texel_colour(u: float, v: float) -> tuple[float, float, float] | None:
    """The picture's linear colour at source UV (u, v); None within 2 texels of a quadrant seam
    (where linear filtering blends two quadrants)."""
    tx, ty = u * WIDTH, v * HEIGHT
    if abs(tx - WIDTH / 2) < 2 or abs(ty - HEIGHT / 2) < 2:
        return None
    rgba = quadrant(min(WIDTH - 1, int(tx)), min(HEIGHT - 1, int(ty)))
    return tuple(decode_channel(value / 255) for value in rgba[:3])


def close(actual, expected) -> bool:
    return max(abs(a - b) for a, b in zip(actual, expected, strict=True)) <= TOLERANCE


def check_fit(native: CapturingRenderer, fit, operations: PictureOperations | None, *,
              profile: FrameProfile = PROFILE, crop: PhotoCrop = CENTRED) -> tuple[int, int]:
    """Every sample equals the reference: inside, the picture through `final_pixel`; outside,
    opaque black. The (inside, outside) sample counts."""
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
            linear = texel_colour(*uv)
            if linear is None:
                continue
            expected = final_pixel(linear, applied, inside=True)
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
TALL_BINDING = OutputBinding(output_id="headless", frame_id="frame", generation=1, profile=TALL)


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


CASES = (cover_matches_the_reference(0), cover_matches_the_reference(7),
         cover_matches_the_reference(90), outside_the_opening_stays_black_at_low_contrast,
         corners_fit_matches_the_reference, operations_change_the_picture_and_the_tag,
         no_trim_and_no_gain_uniforms, alpha_and_black_layers_compose_as_the_reference,
         a_corrupt_picture_fails_and_the_frame_still_presents,
         the_diagnostic_shows_only_while_the_output_is_unbound, crop_offset_keeps_the_left_edge)


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
