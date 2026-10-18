"""Render redesign S1, bead B4 (run ledger .claude/runs/render-planes/s1-page.md): the fit pass's
pixels equal player/geometry.py's scalar reference on software GL.

Run only inside the display harness container (scripts/run_display_harness.py), with `player` and
`contracts` mounted read-only at /repo and `tests` at /smoke. It makes its own EGL surfaceless
GLES 3 context on Mesa's software GL (no window system, no render node), drives the real
`player.fit_pass.FitPass` into plain texture framebuffers and reads each target back with
glReadPixels: a framebuffer object's row 0 is its memory's first row, the target's top. The
picture is a 64x48 RGBA image of four flat quadrants (red, green, blue, white), uploaded here.

The reference of target pixel (x, y): (u, v) = `buffer_to_picture` at its centre, the source point
`fit_source_uv`, the quadrant's colour there, `decode_channel`, then `final_pixel` inside, with the
Frame's `picture_lights`, (u, v) as the opening's point (the identity `to_opening`), the LUT and
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
7. both_passes_carry_the_one_look: the fit pass's linked programs and the frame pass's
   (player/native.py `_frame_source`, linked here) each hold LOOK_GLSL verbatim.

The new path's device and decode on the same libraries the Pi runs (the container's Debian
PyOpenGL and GStreamer 1.26; the GBM targets and the hardware decoder have no CI and are the Pi
tracer's):
8. textures_are_deleted_through_gles3: player/gpu.py `delete_textures` deletes two textures on
   PyOpenGL's GLES3 (its glDeleteTextures takes GLES's (n, textures)).
9. a_still_decodes_on_gstreamer: player/decode.py `StillDecode` decodes an opaque and a
   transparent PNG (the quadrants, written here) to their RGBA pixels and alpha flag, through
   GStreamer 1.26's caps structures.
"""
import ctypes
import os
import sys
import time
import zlib
from pathlib import Path

sys.path[:0] = ["/repo", "/smoke"]
os.environ.update(LIBGL_ALWAYS_SOFTWARE="1", PYOPENGL_PLATFORM="egl", EGL_PLATFORM="surfaceless")

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
from player import native as native_module  # noqa: E402
from player.fit_pass import FitPass, FitRequest, FitSource, target_size  # noqa: E402
from player.geometry import (  # noqa: E402
    buffer_to_picture,
    decode_channel,
    final_pixel,
    fit_source_uv,
    picture_lights,
)
from player.look_shader import LOOK_GLSL  # noqa: E402

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


# -- The GL context: EGL surfaceless, GLES 3, software ---------------------------------------------

EGL_DEFAULT_DISPLAY = None
EGL_PLATFORM_SURFACELESS_MESA = 0x31DD
EGL_NONE, EGL_RENDERABLE_TYPE, EGL_SURFACE_TYPE = 0x3038, 0x3040, 0x3033
EGL_OPENGL_ES3_BIT, EGL_OPENGL_ES_API, EGL_CONTEXT_MAJOR_VERSION = 0x40, 0x30A0, 0x3098


def make_context() -> None:
    """An EGL surfaceless GLES 3 context, current on this thread (ctypes on libEGL)."""
    egl = ctypes.CDLL("libEGL.so.1")
    egl.eglGetPlatformDisplay.restype = ctypes.c_void_p
    egl.eglGetPlatformDisplay.argtypes = (ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p)
    egl.eglInitialize.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int))
    egl.eglChooseConfig.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_void_p),
                                    ctypes.c_int, ctypes.POINTER(ctypes.c_int))
    egl.eglCreateContext.restype = ctypes.c_void_p
    egl.eglCreateContext.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))
    egl.eglMakeCurrent.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    display = egl.eglGetPlatformDisplay(EGL_PLATFORM_SURFACELESS_MESA, EGL_DEFAULT_DISPLAY, None)
    assert display, "egl_display"
    major, minor = ctypes.c_int(), ctypes.c_int()
    assert egl.eglInitialize(display, ctypes.byref(major), ctypes.byref(minor)), "egl_initialize"
    assert egl.eglBindAPI(EGL_OPENGL_ES_API), "egl_bind_api"
    attributes = (ctypes.c_int * 5)(EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT, EGL_SURFACE_TYPE, 0, EGL_NONE)
    config, count = ctypes.c_void_p(), ctypes.c_int()
    assert egl.eglChooseConfig(display, attributes, ctypes.byref(config), 1, ctypes.byref(count)) and count.value, \
        "egl_config"
    context = egl.eglCreateContext(display, config, None, (ctypes.c_int * 3)(EGL_CONTEXT_MAJOR_VERSION, 3, EGL_NONE))
    assert context, "egl_context"
    assert egl.eglMakeCurrent(display, None, None, context), "egl_make_current"
    print(f"EGL {major.value}.{minor.value} surfaceless GLES 3", flush=True)


make_context()
from OpenGL import GLES3 as gl  # noqa: E402  (after the context: PyOpenGL resolves on it)


class PlainTarget:
    """A DrawTarget: an RGBA8 texture's framebuffer, `size` as laid out."""

    def __init__(self, size: tuple[int, int]):
        self.size = size
        self.texture = int(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.texture)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, *size, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
        self.framebuffer = int(gl.glGenFramebuffers(1))
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.framebuffer)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_TEXTURE_2D, self.texture, 0)
        assert gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) == gl.GL_FRAMEBUFFER_COMPLETE, "framebuffer"
        gl.glClearColor(.5, .25, .75, 1.0)          # anything but the answer
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)

    def read(self) -> bytes:
        """The target's RGBA bytes, its top row first."""
        gl.glFinish()
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.framebuffer)
        width, height = self.size
        pixels = (ctypes.c_ubyte * (width * height * 4))()
        gl.glReadPixels(0, 0, width, height, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, pixels)
        return bytes(pixels)

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
    frame = native_module._frame_source(native_module._Steps(True, 0))
    shaders = []
    for text, kind in ((native_module._VERTEX, gl.GL_VERTEX_SHADER), (frame, gl.GL_FRAGMENT_SHADER)):
        shader = int(gl.glCreateShader(kind))
        gl.glShaderSource(shader, text)
        gl.glCompileShader(shader)
        assert gl.glGetShaderiv(shader, gl.GL_COMPILE_STATUS), ("frame_pass_compile", gl.glGetShaderInfoLog(shader))
        shaders.append(shader)
    program = int(gl.glCreateProgram())
    for shader in shaders:
        gl.glAttachShader(program, shader)
    gl.glLinkProgram(program)
    assert gl.glGetProgramiv(program, gl.GL_LINK_STATUS), ("frame_pass_link", gl.glGetProgramInfoLog(program))
    assert any(look in text for text in sources_of(program)), "frame_pass_without_look"
    gl.glDeleteProgram(program)
    for shader in shaders:
        gl.glDeleteShader(shader)


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


CASES = (turn_matches_the_reference(0, 0), turn_matches_the_reference(90, 1), turn_matches_the_reference(270, 2),
         crop_offset_shows_the_photos_edge(0, {0, 2}), crop_offset_shows_the_photos_edge(1, {1, 3}),
         transparency_is_stored_pre_multiplied, black_is_opaque_black, bt709_is_decoded_as_video,
         a_wrong_target_size_is_refused, both_passes_carry_the_one_look, textures_are_deleted_through_gles3,
         a_still_decodes_on_gstreamer)


def main() -> None:
    global PASS, PHOTO
    PASS = FitPass(gl)
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
    assert not failed, ("failed", failed)


if __name__ == "__main__":
    main()
