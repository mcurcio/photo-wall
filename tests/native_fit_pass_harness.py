"""Render redesign S1, bead B4 (run ledger .claude/runs/render-planes/s1-page.md): the fit pass's
pixels equal player/geometry.py's scalar reference on software GL.

Run only inside the display harness container (scripts/run_display_harness.py), with `player` and
`contracts` mounted read-only at /repo and `tests` at /smoke. It draws on the harnesses' one EGL
surfaceless GLES 3 context on Mesa's software GL (tests/harness_device.py `context`), drives the real
`player.fit_pass.FitPass` into plain texture framebuffers and reads each target back
(harness_device.py `read_rgba`: a framebuffer object's row 0 is its memory's first row, the
target's top). The
picture is a 64x48 RGBA image of four flat quadrants (red, green, blue, white), uploaded here.

The reference of target pixel (x, y): (u, v) = `buffer_to_picture` at its centre, the source point
`fit_source_uv`, the quadrant's colour there, `decode_channel`, then `final_pixel` inside, with the
Frame's `picture_lights`, (u, v) as the picture's point for the lights and the pattern, the LUT and
`dither_at` (x, y); pre-multiplied by the source's alpha when it carries one. Samples within 1.5
source pixels of a quadrant's edge are skipped (the GPU's filter mixes there). Every sample is
within one level of 256 (ONE_LEVEL codes) in every channel. The LUT is sampled only at its grid's
corners (the quadrants' colours are 0 or 1 per channel), where software GL's 8-bit filter is exact.

1. turn_<0|90|270>_matches_the_reference: the quadrants on a 4:3 Frame (128 x 96 as it hangs) with
   non-neutral operations, a wash and a far light's mat shadow and a channel-turning LUT; at 90
   and 270 the target is 96 x 128 and its top-left pixel shows the picture's top-right (90) or
   bottom-left (270) quadrant, as the reference puts them.
2. crop_offset_<0|1>_shows_the_photos_<left|right>_edge: a 9:16 Frame (45 x 80) keeps the left
   quadrants (red, blue) at offset 0 and the right ones (green, white) at offset 1.
3. transparency_is_stored_pre_multiplied: the quadrants at alpha 128 write the reference's values
   times 128/255 and alpha 128.
4. black_is_opaque_black: `black` over a drawn target writes (0, 0, 0, 255) everywhere.
5. bt709_is_decoded_as_video: a 2D source marked bt709 equals the reference decoded as video.
6. a_wrong_target_size_is_refused: a target the size of the unturned picture at 90 degrees is
   ValueError("fit_target_size") and nothing is drawn.
7. both_passes_carry_the_one_look: the fit pass's linked programs and the pattern pass's each hold
   LOOK_GLSL verbatim.

The new path's device and decode on the same libraries the Pi runs (the container's Debian
PyOpenGL and GStreamer 1.26; the GBM targets and the hardware decoder have no CI and are the Pi
tracer's):
8. textures_are_deleted_through_gles3: player/gpu.py `delete_textures` deletes two textures on
   PyOpenGL's GLES3 (its glDeleteTextures takes GLES's (n, textures)).
9. a_still_decodes_on_gstreamer: player/decode.py `StillDecode` decodes an opaque and a
   transparent PNG (the quadrants, written here) to their RGBA pixels and alpha flag, through
   GStreamer 1.26's caps structures.

The pattern pass (render redesign S3, bead B2: run ledger .claude/runs/render-planes/s3-page.md),
the real `player.pattern_pass.PatternPass` into the whole screen's target: a 64 x 48 screen with
a 16:9 Frame placed 32 x 18 at (16, 15). The reference of target pixel (x, y) is the pass's module
docstring's chain: `buffer_to_picture` over the screen, `placement_uv`, then `final_pixel` (the
grid's photo `commissioning_grid`), `dither_at` (x, y). Every pixel inside the placement is held to
it within one level, except where the reference changes within PATTERN_EDGE pixels of the centre
(a ramp step, a cell, a grid line, a board texel or a QR module's edge: float rounding may fall
either side); every pixel outside is exactly opaque black.
10. pattern_<name>_turn_<0|90>_matches_the_reference: White (non-neutral operations, the LUT bound
   and unread), Grey ramp, Black level, both calibration targets with the QR of a fixed TargetCode
   and with none (the two differ), and the commissioning grid (the far light's mat shadow), also
   through the LUT, sampled between its grid points there, so held to LUT_BETWEEN_GRID_POINTS; at
   90 the target is 48 x 64.
11. a_pattern_target_needs_its_board: ValueError("board"), nothing drawn.
12. a_wrong_pattern_target_size_is_refused: the unturned screen's target at 90 is
   ValueError("pattern_target_size"), nothing drawn.
"""
import dataclasses
import sys
import time
import zlib
from pathlib import Path

sys.path[:0] = ["/repo", "/smoke"]
from harness_device import context, read_rgba, texture_framebuffer  # noqa: E402

gl = context()      # before any player import: PyOpenGL resolves on it

from contracts.calibration_target import TargetCode  # noqa: E402
from contracts.frame_fit import Placement  # noqa: E402
from contracts.frame_lighting import (  # noqa: E402
    AT_FULL,
    FarLight,
    FrameLighting,
    Paper,
    WashLight,
)
from contracts.lut import LUT_BYTES, LUT_GRID, lut_offset  # noqa: E402
from contracts.models import FrameProfile, PhotoCrop  # noqa: E402
from contracts.node_frame import PictureOperations  # noqa: E402
from contracts.node_picture import DisplayLight  # noqa: E402
from contracts.room_light import FixedColour, FixedEntry, RoomLightsDocument  # noqa: E402
from contracts.tuning import TARGET_PATTERNS, TestPattern  # noqa: E402
from player.calibration_target import target_modules  # noqa: E402
from player.fit_pass import FitPass, FitRequest, FitSource, target_size  # noqa: E402
from player.geometry import (  # noqa: E402
    buffer_to_picture,
    commissioning_grid,
    decode_channel,
    final_pixel,
    fit_source_uv,
    picture_board,
    picture_lights,
    placement_uv,
)
from player.look_shader import LOOK_GLSL  # noqa: E402
from player.pattern_pass import PatternPass, PatternRequest  # noqa: E402

WIDTH, HEIGHT = 64, 48                      # the picture
QUADRANTS = ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255))
ONE_LEVEL = 255 / 256                       # one level of 256, in 8-bit codes
EDGE = 1.5                                  # source pixels skipped either side of a quadrant's edge
STEP = 3                                    # target pixels between samples
OPERATIONS = PictureOperations("screen", 3, (.9, .95, 1.0), .8, 24 / 22, DisplayLight(250, .01))
PROFILE = FrameProfile(width_px=1600, height_px=1200, diagonal_inches=20)        # 4:3
PORTRAIT = FrameProfile(width_px=900, height_px=1600, diagonal_inches=20)        # 9:16
LIGHTING = FrameLighting(mat_depth_mm=8, paper=Paper(whiteness=0.9, ink_density=1.8), lights=(
    WashLight(light="room", lux=200, reference=AT_FULL),
    FarLight(light="lamp", lux=600, reference=AT_FULL, from_degrees=20, steep_degrees=30, softness_degrees=20)))
ROOM = RoomLightsDocument(change=1, lights={
    "room": FixedEntry(colour=FixedColour(kelvin=6500)), "lamp": FixedEntry(colour=FixedColour(kelvin=3000))})


def turning_table() -> bytes:
    """contracts/lut.py's layout: grid point (r, g, b) holds a colour of all three, channels turned
    (r -> g, g -> b, b -> r) and lifted off black, so the LUT step changes every quadrant."""
    body = bytearray(LUT_BYTES)
    top = LUT_GRID - 1
    for r in range(LUT_GRID):
        for g in range(LUT_GRID):
            for b in range(LUT_GRID):
                offset = lut_offset(r, g, b)
                body[offset:offset + 3] = bytes((round(255 * (.2 + .6 * b / top)), round(255 * (.1 + .8 * r / top)),
                                                 round(255 * (.3 + .5 * g / top))))
    return bytes(body)


LUT = turning_table()


class PlainTarget:
    """A DrawTarget: an RGBA8 texture's framebuffer, `size` as laid out."""

    def __init__(self, size: tuple[int, int]):
        self.size = size
        self.texture, self.framebuffer = texture_framebuffer(gl, size)
        gl.glClearColor(.5, .25, .75, 1.0)          # anything but the answer
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)

    def read(self) -> bytes:
        """The target's RGBA bytes, its top row first."""
        return read_rgba(gl, self.framebuffer, self.size)

    def free(self) -> None:
        gl.glDeleteFramebuffers(1, [self.framebuffer])
        gl.glDeleteTextures(1, [self.texture])


def quadrant(x: int, y: int) -> tuple[int, int, int]:
    return QUADRANTS[(x >= WIDTH // 2) + 2 * (y >= HEIGHT // 2)]


def source(alpha: int = 255) -> int:
    """The quadrants as a GL_TEXTURE_2D RGBA8 (straight alpha `alpha`), its top row first."""
    data = b"".join(bytes((*quadrant(x, y), alpha)) for y in range(HEIGHT) for x in range(WIDTH))
    texture = int(gl.glGenTextures(1))
    gl.glActiveTexture(gl.GL_TEXTURE0)
    gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
    for parameter, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR), (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR),
                             (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE), (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
        gl.glTexParameteri(gl.GL_TEXTURE_2D, parameter, value)
    gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, WIDTH, HEIGHT, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, data)
    return texture


def request(texture: int, *, turn=0, profile=PROFILE, placement=Placement(0, 0, 128, 96), crop=None,
            encoding="srgb", alpha=False, lut=LUT) -> FitRequest:
    return FitRequest(source=FitSource(texture, False, (WIDTH, HEIGHT), encoding, alpha), crop=crop,
                      aspect=profile.width_px / profile.height_px, placement=placement, turn=turn,
                      operations=OPERATIONS, lights=picture_lights(LIGHTING, ROOM, profile, OPERATIONS.light),
                      lut=lut)


def reference(fit: FitRequest, size: tuple[int, int], x: int, y: int, source_alpha: int = 255):
    """The scalar reference of target pixel (x, y) (module docstring); None near a quadrant's edge."""
    u, v = buffer_to_picture(x + .5, y + .5, size, fit.turn)
    s, t = fit_source_uv(u, v, fit.aspect, fit.source.size, fit.crop)
    column, row = s * WIDTH, t * HEIGHT
    if abs(column - WIDTH / 2) < EDGE or abs(row - HEIGHT / 2) < EDGE:
        return None
    colour = quadrant(min(WIDTH - 1, int(column)), min(HEIGHT - 1, int(row)))
    linear = tuple(decode_channel(c / 255, fit.source.encoding == "bt709") for c in colour)
    pixel = final_pixel(linear, fit.operations, inside=True, lights=fit.lights, x=u, y=v, lut=fit.lut,
                        opening_uv=(u, v), dither_at=(x, y))
    if not fit.source.alpha:
        return pixel
    share = source_alpha / 255
    return (*(value * share for value in pixel[:3]), share)


def drawn(fit: FitRequest) -> tuple[PlainTarget, bytes]:
    target = PlainTarget(target_size(fit.placement, fit.turn))
    PASS.draw(fit, target)
    return target, target.read()


def at(data: bytes, size: tuple[int, int], x: int, y: int) -> tuple[int, ...]:
    offset = (y * size[0] + x) * 4
    return tuple(data[offset:offset + 4])


def compare(fit: FitRequest, source_alpha: int = 255) -> int:
    """Draw `fit` and hold every sample to the reference; the count compared."""
    target, data = drawn(fit)
    try:
        count = 0
        width, height = target.size
        for y in range(1, height, STEP):
            for x in range(1, width, STEP):
                expected = reference(fit, target.size, x, y, source_alpha)
                if expected is None:
                    continue
                actual = at(data, target.size, x, y)
                error = max(abs(a - e * 255) for a, e in zip(actual, expected, strict=True))
                assert error <= ONE_LEVEL, ("pixel", fit.turn, x, y, actual, [round(e * 255, 3) for e in expected])
                count += 1
        assert count > width * height / STEP ** 2 / 2, ("too_few_samples", count)
        return count
    finally:
        target.free()


def shown_quadrant(fit: FitRequest, x: int, y: int) -> int:
    """Which quadrant (0 to 3) the reference shows at target pixel (x, y)."""
    size = target_size(fit.placement, fit.turn)
    s, t = fit_source_uv(*buffer_to_picture(x + .5, y + .5, size, fit.turn), fit.aspect, fit.source.size, fit.crop)
    return (s >= .5) + 2 * (t >= .5)


def turn_matches_the_reference(turn: int, top_left: int):
    def case() -> None:
        fit = request(PHOTO, turn=turn)
        width, height = target_size(fit.placement, fit.turn)
        assert (width, height) == ((128, 96) if turn % 180 == 0 else (96, 128)), (turn, width, height)
        assert shown_quadrant(fit, 2, 2) == top_left, ("reference_top_left", turn)
        print(f"  turn {turn}: {compare(fit)} samples", flush=True)
    case.__name__ = f"turn_{turn}_matches_the_reference"
    return case


def crop_offset_shows_the_photos_edge(offset: int, quadrants: set[int]):
    def case() -> None:
        fit = request(PHOTO, profile=PORTRAIT, placement=Placement(0, 0, 45, 80), crop=PhotoCrop(offset=offset))
        compare(fit)
        size = target_size(fit.placement, fit.turn)
        target, data = drawn(fit)
        target.free()
        looked = {quadrant_of(at(data, size, x, y), fit, size, x, y) for y in range(2, 80, 4) for x in range(2, 45, 4)}
        assert looked - {None} == quadrants, ("crop", offset, looked)
    case.__name__ = f"crop_offset_{offset}_shows_the_photos_{'left' if offset == 0 else 'right'}_edge"
    return case


def quadrant_of(actual, fit: FitRequest, size, x: int, y: int) -> int | None:
    """The quadrant whose reference colour the drawn pixel is (None near an edge)."""
    if reference(fit, size, x, y) is None:
        return None
    u, v = buffer_to_picture(x + .5, y + .5, size, fit.turn)
    for index, colour in enumerate(QUADRANTS):
        linear = tuple(decode_channel(c / 255) for c in colour)
        expected = final_pixel(linear, fit.operations, inside=True, lights=fit.lights, x=u, y=v, lut=fit.lut,
                               dither_at=(x, y))
        if max(abs(a - e * 255) for a, e in zip(actual, expected, strict=True)) <= ONE_LEVEL:
            return index
    raise AssertionError(("no_quadrant", x, y, actual))


def transparency_is_stored_pre_multiplied() -> None:
    texture = source(alpha=128)
    try:
        fit = request(texture, alpha=True)
        compare(fit, source_alpha=128)
        target, data = drawn(fit)
        target.free()
        assert {data[offset + 3] for offset in range(0, len(data), 4)} == {128}, "alpha"
    finally:
        gl.glDeleteTextures(1, [texture])


def black_is_opaque_black() -> None:
    fit = request(PHOTO)
    target, _ = drawn(fit)
    try:
        PASS.black(target)
        data = target.read()
        assert set(data[i:i + 4] for i in range(0, len(data), 4)) == {bytes((0, 0, 0, 255))}, "black"
    finally:
        target.free()


def bt709_is_decoded_as_video() -> None:
    srgb = request(PHOTO, lut=None)
    video = request(PHOTO, encoding="bt709", lut=None)
    compare(video)
    pictures = []
    for fit in (srgb, video):
        target, data = drawn(fit)
        target.free()
        pictures.append(data)
    assert pictures[0] != pictures[1], "the encoding changes nothing"


def a_wrong_target_size_is_refused() -> None:
    fit = request(PHOTO, turn=90)
    target = PlainTarget((128, 96))
    try:
        before = target.read()
        try:
            PASS.draw(fit, target)
        except ValueError as error:
            assert error.args == ("fit_target_size",), error.args
        else:
            raise AssertionError("drawn into a target of the wrong size")
        assert target.read() == before, "drew before refusing"
    finally:
        target.free()


def sources_of(program: int) -> list[bytes]:
    return [gl.glGetShaderSource(int(shader)) for shader in gl.glGetAttachedShaders(program)]


def both_passes_carry_the_one_look() -> None:
    look = LOOK_GLSL.encode()
    programs = list(PASS._programs.values())       # the still sources' programs, with and without the LUT
    assert len(programs) >= 2, programs
    for program in programs:
        assert any(look in text for text in sources_of(program)), ("fit_pass_without_look", program)
    patterns = list(PATTERNS._programs.values())  # with and without the LUT
    assert len(patterns) >= 2, patterns
    for program in patterns:
        assert any(look in text for text in sources_of(program)), ("pattern_pass_without_look", program)


def textures_are_deleted_through_gles3() -> None:
    from player.gpu import delete_textures

    names = [int(gl.glGenTextures(1)) for _ in range(2)]
    for name in names:                  # a name is a texture once bound
        gl.glBindTexture(gl.GL_TEXTURE_2D, name)
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
    assert all(gl.glIsTexture(name) for name in names), "textures_made"
    delete_textures(gl, *names)
    assert not any(gl.glIsTexture(name) for name in names), "textures_kept"


def png(path: Path, alpha: int | None) -> None:
    """The quadrants as an 8-bit PNG: RGB, or RGBA at `alpha`."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")

    channels = 3 if alpha is None else 4
    rows = b"".join(b"\x00" + b"".join(bytes(quadrant(x, y)) + (b"" if alpha is None else bytes((alpha,)))
                                       for x in range(WIDTH)) for y in range(HEIGHT))
    header = WIDTH.to_bytes(4, "big") + HEIGHT.to_bytes(4, "big") + bytes((8, 2 if channels == 3 else 6, 0, 0, 0))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows))
                     + chunk(b"IEND", b""))


def a_still_decodes_on_gstreamer() -> None:
    from player.decode import STILL_TIMEOUT, StillDecode

    for alpha in (None, 200):
        path = Path(f"/tmp/pw-still-{alpha}.png")
        png(path, alpha)
        decode = StillDecode(path)
        end = time.monotonic() + STILL_TIMEOUT + 1
        while (result := decode.result()) == "pending" and time.monotonic() < end:
            time.sleep(0.01)
        decode.close()
        assert result not in ("pending", "failed"), ("still", alpha, result)
        assert result.size == (WIDTH, HEIGHT) and result.alpha == (alpha is not None), (result.size, result.alpha)
        for x, y in ((8, 8), (56, 8), (8, 40), (56, 40)):
            offset = y * result.stride + x * 4
            wanted = (*quadrant(x, y), 255 if alpha is None else alpha)
            assert tuple(result.pixels[offset:offset + 4]) == wanted, (alpha, x, y, result.pixels[offset:offset + 4])


# -- The pattern pass (cases 10-12) ----------------------------------------------------------------

SCREEN = (64, 48)
PLACEMENT = Placement(16, 15, 32, 18)
WIDE = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=27)      # 16:9, as the placement
PATTERN_EDGE = 0.002                        # target pixels either side of a sample's centre
# A LUT sampled between its grid points: software GL filters it with 8-bit weights, so a sample
# there is held to two levels (the commissioning grid's slate is no grid corner: worst seen 1.035
# levels, llvmpipe, 2026-10-18); the same grid without the LUT is held to ONE_LEVEL.
LUT_BETWEEN_GRID_POINTS = 2 * ONE_LEVEL
TARGET_CODE = TargetCode("p-" + "0123456789abcdef" * 2, "screen", 3)
MODULES = target_modules(TARGET_CODE)


def pattern_request(pattern: TestPattern | None, *, turn=0, lut=None, modules=None) -> PatternRequest:
    board = picture_board(WIDE, modules) if pattern in TARGET_PATTERNS else None
    return PatternRequest(size=SCREEN, placement=PLACEMENT, turn=turn,
                          operations=dataclasses.replace(OPERATIONS, test_pattern=pattern),
                          lights=picture_lights(LIGHTING, ROOM, WIDE, OPERATIONS.light), lut=lut, board=board)


def pattern_reference(request: PatternRequest, size: tuple[int, int], x: int, y: int, dx=0.0, dy=0.0):
    """The scalar reference of target pixel (x, y) (module docstring), its point moved (dx, dy);
    None outside the placement."""
    uv = placement_uv(*buffer_to_picture(x + .5 + dx, y + .5 + dy, size, request.turn), request.placement,
                      request.size)
    if uv is None:
        return None
    pattern = request.operations.test_pattern
    photo = commissioning_grid(*uv) if pattern is None else (0.0, 0.0, 0.0)
    return final_pixel(photo, request.operations, inside=True, lights=request.lights, x=uv[0], y=uv[1],
                       lut=request.lut, opening_uv=uv, board=request.board, dither_at=(x, y))


def pattern_drawn(request: PatternRequest) -> tuple[tuple[int, int], bytes]:
    target = PlainTarget(target_size(Placement(0, 0, *request.size), request.turn))
    try:
        PATTERNS.draw(request, target)
        return target.size, target.read()
    finally:
        target.free()


def pattern_compare(request: PatternRequest, tolerance: float = ONE_LEVEL) -> bytes:
    """Draw `request` and hold every pixel to the reference within `tolerance` codes (module
    docstring); the drawn bytes."""
    size, data = pattern_drawn(request)
    width, height = size
    inside = compared = 0
    for y in range(height):
        for x in range(width):
            actual = at(data, size, x, y)
            expected = pattern_reference(request, size, x, y)
            if expected is None:
                assert actual == (0, 0, 0, 255), ("outside", request.turn, x, y, actual)
                continue
            inside += 1
            near = [pattern_reference(request, size, x, y, dx, dy)
                    for dx, dy in ((PATTERN_EDGE, 0), (-PATTERN_EDGE, 0), (0, PATTERN_EDGE), (0, -PATTERN_EDGE))]
            if any(max(abs(a - b) for a, b in zip(other, expected, strict=True)) * 255 > .5 for other in near):
                continue
            error = max(abs(a - e * 255) for a, e in zip(actual, expected, strict=True))
            assert error <= tolerance, ("pixel", request.turn, x, y, actual, [round(e * 255, 3) for e in expected])
            compared += 1
    assert inside == PLACEMENT.width * PLACEMENT.height, ("placement", inside)
    assert compared > inside * 3 / 4, ("too_few_samples", compared)
    return data


def pattern_matches_the_reference(name: str, pattern: TestPattern | None, turn: int, tolerance=ONE_LEVEL,
                                  **options):
    def case() -> None:
        assert target_size(Placement(0, 0, *SCREEN), turn) == ((64, 48) if turn == 0 else (48, 64)), turn
        drawn = pattern_compare(pattern_request(pattern, turn=turn, **options), tolerance)
        if pattern in TARGET_PATTERNS:      # the QR's modules reach the screen
            uncoded = pattern_compare(pattern_request(pattern, turn=turn, lut=options.get("lut")))
            assert uncoded != drawn, ("the code changes nothing", name)
        assert len({drawn[i:i + 4] for i in range(0, len(drawn), 4)}) > 2, ("flat", name)
    case.__name__ = f"pattern_{name}_turn_{turn}_matches_the_reference"
    return case


def pattern_refused(request: PatternRequest, size: tuple[int, int], reason: str) -> None:
    target = PlainTarget(size)
    try:
        before = target.read()
        try:
            PATTERNS.draw(request, target)
        except ValueError as error:
            assert error.args == (reason,), error.args
        else:
            raise AssertionError(("drawn", reason))
        assert target.read() == before, ("drew before refusing", reason)
    finally:
        target.free()


def a_pattern_target_needs_its_board() -> None:
    request = dataclasses.replace(pattern_request(TestPattern.TARGET_PRE_SOFTWARE), board=None)
    pattern_refused(request, SCREEN, "board")


def a_wrong_pattern_target_size_is_refused() -> None:
    pattern_refused(pattern_request(TestPattern.WHITE, turn=90), SCREEN, "pattern_target_size")


PATTERN_CASES = tuple(
    pattern_matches_the_reference(name, pattern, turn, **options)
    for turn in (0, 90)
    for name, pattern, options in (
        ("white", TestPattern.WHITE, {"lut": LUT}),
        ("grey_ramp", TestPattern.GREY_RAMP, {}),
        ("black_level", TestPattern.BLACK_LEVEL, {}),
        ("target_pre_software", TestPattern.TARGET_PRE_SOFTWARE, {"modules": MODULES}),
        ("target_post_software", TestPattern.TARGET_POST_SOFTWARE, {"modules": MODULES}),
        ("commissioning_grid", None, {}),
        ("commissioning_grid_through_the_lut", None, {"lut": LUT, "tolerance": LUT_BETWEEN_GRID_POINTS})))


CASES = (turn_matches_the_reference(0, 0), turn_matches_the_reference(90, 1), turn_matches_the_reference(270, 2),
         crop_offset_shows_the_photos_edge(0, {0, 2}), crop_offset_shows_the_photos_edge(1, {1, 3}),
         transparency_is_stored_pre_multiplied, black_is_opaque_black, bt709_is_decoded_as_video,
         a_wrong_target_size_is_refused, both_passes_carry_the_one_look, textures_are_deleted_through_gles3,
         a_still_decodes_on_gstreamer, *PATTERN_CASES, a_pattern_target_needs_its_board,
         a_wrong_pattern_target_size_is_refused)


def main() -> None:
    global PASS, PATTERNS, PHOTO
    PASS = FitPass(gl)
    PATTERNS = PatternPass(gl)
    PHOTO = source()
    failed = []
    try:
        for case in CASES:
            try:
                case()
                print(f"PASS {case.__name__}", flush=True)
            except Exception as error:   # noqa: BLE001 - a library's refusal is a failed case too
                failed.append(case.__name__)
                print(f"FAIL {case.__name__}: {error!r}"[:4000], flush=True)
    finally:
        PASS.close()
        PATTERNS.close()
    assert not failed, ("failed", failed)


if __name__ == "__main__":
    main()
