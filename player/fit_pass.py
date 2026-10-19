"""The fit pass: one picture finished once, at its finish size (player/planes.py `finish_size`: the
smaller of its cropped source and its Frame's placement), for the display controller to show 1:1 or
enlarged to the placement, never shrunk (render redesign, run ledger .claude/runs/render-planes/
s1-page.md; owner, 2026-10-18: "the look-and-feel should be applied at the minimum of the asset size
and the frame size"). Photos once; a video once per decoded frame (owner, gate answer Q1,
2026-10-17: video carries the same look as photos).

One GPU draw from the source to the target: Central's crop window, scaled to the finish size,
turned for the screen, decoded to linear light (a photo's sRGB, a video's BT.709), then the look
(player/look_shader.py: the Frame's paper under the room's light and its mat shadow, the paper
profile's LUT, the Display's software settings, encode, contrast, gamma, dither), written as stored
8-bit sRGB values; a picture with transparency (a PNG) pre-multiplied. GL on the current context
only: no EGL, GBM, window system or decoder here, so the harness draws it on software GL.

Rules this module owns (one home each):
- **The target's layout** (`target_size`): the picture's pixels as it hangs, turned for the screen,
  so 90 and 270 swap width and height; the screen shows the buffer with a matching buffer transform
  and never copies it (bench T5).
- **What a draw computes**: per target pixel, player/geometry.py's scalar reference
  (`buffer_to_picture`, `fit_source_uv`, then `final_pixel` with the Frame's `picture_lights` and
  the pixel's place in the picture as the opening's), within one level of 256. The look works in the
picture's own coordinates (0..1) and the Frame's physical size, so a picture finished smaller looks
the same, enlarged.

Rules it does NOT own: the size a picture is finished at (player/planes.py `finish_size`); the look's formulas (player/geometry.py, mirrored by player/look_shader.py);
where the target's memory comes from (player/gpu.py `Targets`); when a picture is drawn again
(player/plane_renderer.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

from contracts.display_turn import DisplayTurn, hanging
from contracts.models import PhotoCrop
from contracts.node_frame import PictureOperations
from player.geometry import FrameLights, buffer_to_picture, fit_source_uv
from player.look_shader import LOOK_GLSL, LUT_UNIT, look_uniforms, upload_lut

SourceEncoding = Literal["srgb", "bt709"]


class FitFailed(RuntimeError):
    """A draw the GPU did not finish: args[0] is "gpu_error". The Frame keeps its last picture."""


@dataclass(frozen=True)
class FitSource:
    """What the fit pass samples: a GL texture (`external`: GL_TEXTURE_EXTERNAL_OES, a decoder frame
    from player/gpu.py `import_frame`; else GL_TEXTURE_2D RGBA8, a still from `upload`), its `size`
    in pixels, its `encoding`, and whether it may carry transparency (`alpha`: a PNG)."""
    texture: int
    external: bool
    size: tuple[int, int]
    encoding: SourceEncoding
    alpha: bool


@dataclass(frozen=True)
class FitRequest:
    """One picture to finish: its `source`; Central's `crop` (contracts/photo_crop.py; None only for
    a source with exactly the Frame's aspect); the Frame profile's `aspect`; the picture's `size`
    (width, height) as it hangs (player/planes.py `finish_size`); the screen's
    `turn`; the Output's picture `operations`; the Frame's `lights` (player/geometry.py
    `picture_lights`); the paper profile's `lut` body (None: no LUT step)."""
    source: FitSource
    crop: PhotoCrop | None
    aspect: float
    size: tuple[int, int]
    turn: DisplayTurn
    operations: PictureOperations
    lights: FrameLights
    lut: bytes | None


class DrawTarget(Protocol):
    """Where a draw lands: a complete GL framebuffer of `size` (width, height) as laid out
    (player/gpu.py `Target` in production; a plain texture's framebuffer in the harness)."""

    @property
    def framebuffer(self) -> int: ...

    @property
    def size(self) -> tuple[int, int]: ...


def target_size(size: tuple[int, int], turn: DisplayTurn) -> tuple[int, int]:
    """The target's (width, height) for a picture `size` = (width, height) as it hangs on a screen
    turned `turn` (module rule). ValueError("display_turn") for any other turn. Pure.

    The swap is the screen's own (contracts/display_turn.py `hanging`): a picture as it hangs is
    laid out the other way round on a screen hung a quarter turned, as the screen's mode is."""
    return hanging(size, turn)


GL_TEXTURE_EXTERNAL_OES: Final = 0x8D65     # OES_EGL_image_external, not in every PyOpenGL module

# A triangle over the whole target; the fragment shader works from gl_FragCoord alone.
_VERTEX = """#version 300 es
void main() {
    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    gl_Position = vec4(p*2.0-1.0, 0.0, 1.0);
}
"""
_STEP_SWITCHES = "// The step set: _fragment writes it here.\n"
_EXTENSION = "// The source's sampler extension: _fragment writes it here.\n"
_SAMPLER = "SOURCE_SAMPLER"
# A target's memory rows run top-down from GL's row 0 (a framebuffer object, unlike a window), so
# gl_FragCoord is the target pixel as laid out (y down, centres at +0.5): `buffer_to_picture`'s
# point and `final_pixel`'s dither_at.
_FRAGMENT = """#version 300 es
""" + _EXTENSION + """precision highp float;
precision highp int;
out vec4 color;
""" + _STEP_SWITCHES + LOOK_GLSL + """
uniform SOURCE_SAMPLER media;
// geometry.py `buffer_to_picture`, affine in the target's pixels: the picture's (u, v) at the
// target's origin and per pixel along x and y (the turn is in these three).
uniform vec2 picture_origin;
uniform vec2 picture_per_x;
uniform vec2 picture_per_y;
// geometry.py `fit_source_uv`: the crop window (left, top, right, bottom) over the picture.
uniform vec4 window;
uniform int video;
uniform int source_alpha;
// geometry.py `decode_channel` with video: BT.709's transfer to linear light.
vec3 video_decoded(vec3 value) {
    value = clamp(value, 0.0, 1.0);
    return mix(pow((value+0.099)/1.099, vec3(1.0/0.45)), value/4.5, lessThan(value, vec3(0.081)));
}
void main() {
    vec2 picture = picture_origin+gl_FragCoord.x*picture_per_x+gl_FragCoord.y*picture_per_y;
    vec4 sampled = texture(media, mix(window.xy, window.zw, picture));
    vec3 linear = video == 1 ? video_decoded(sampled.rgb) : srgb_decoded(sampled.rgb);
    vec3 stored = look(linear, picture, ivec2(gl_FragCoord.xy));
    // A picture that carries transparency is stored pre-multiplied (AR24); an opaque one at alpha 1.
    color = source_alpha == 1 ? vec4(stored*sampled.a, sampled.a) : vec4(stored, 1.0);
}
"""


def _fragment(external: bool, lut_on: bool) -> str:
    """The fit pass's fragment shader for one source kind and step set (`FitPass._program`)."""
    return (_FRAGMENT
            .replace(_EXTENSION, "#extension GL_OES_EGL_image_external_essl3 : require\n"
                     "precision highp samplerExternalOES;\n" if external else "")
            .replace(_STEP_SWITCHES, f"const int lut_on = {int(lut_on)};\n")
            .replace(_SAMPLER, "samplerExternalOES" if external else "sampler2D"))


def _picture_axes(size: tuple[int, int], turn: DisplayTurn) -> tuple[tuple[float, float], ...]:
    """`buffer_to_picture` on a `size` target as its origin and its steps per pixel along x and y:
    the map is affine (a quarter turn and a stretch), so three points state it."""
    width, height = size
    origin = buffer_to_picture(0, 0, size, turn)
    along_x, along_y = buffer_to_picture(width, 0, size, turn), buffer_to_picture(0, height, size, turn)
    return (origin, tuple((a - o) / width for a, o in zip(along_x, origin, strict=True)),
            tuple((a - o) / height for a, o in zip(along_y, origin, strict=True)))


class FitPass:
    """The fit pass's programs on the current GL context (module rules)."""

    def __init__(self, gl: Any) -> None:
        """Compile and link the programs (one per source kind and step set: a step switched
        off is a constant of its program) with `gl`, PyOpenGL's GLES 3 module or one like it.
        FitFailed("gpu_error") when a program does not link.

        The still sources' programs are linked here; an external source's on its first draw (it
        needs OES_EGL_image_external_essl3, which a context without a decoder may lack)."""
        self._gl = gl
        self._programs: dict[tuple[bool, bool], int] = {}
        self._lut_texture = 0
        self._lut_table: bytes | None = None
        try:
            self._vertex_array = int(gl.glGenVertexArrays(1))
        except Exception as error:
            raise FitFailed("gpu_error") from error
        for lut_on in (False, True):
            self._program(False, lut_on)

    def _program(self, external: bool, lut_on: bool) -> int:
        program = self._programs.get((external, lut_on))
        if program is not None:
            return program
        gl = self._gl
        shaders: list[int] = []
        program = 0
        try:
            for source, kind in ((_VERTEX, gl.GL_VERTEX_SHADER),
                                 (_fragment(external, lut_on), gl.GL_FRAGMENT_SHADER)):
                shader = int(gl.glCreateShader(kind))
                shaders.append(shader)
                gl.glShaderSource(shader, source)
                gl.glCompileShader(shader)
                if not gl.glGetShaderiv(shader, gl.GL_COMPILE_STATUS):
                    raise FitFailed("gpu_error", gl.glGetShaderInfoLog(shader))
            program = int(gl.glCreateProgram())
            for shader in shaders:
                gl.glAttachShader(program, shader)
            gl.glLinkProgram(program)
            if not gl.glGetProgramiv(program, gl.GL_LINK_STATUS):
                raise FitFailed("gpu_error", gl.glGetProgramInfoLog(program))
        except Exception as error:
            if program:
                gl.glDeleteProgram(program)
            if isinstance(error, FitFailed):
                raise
            raise FitFailed("gpu_error") from error
        finally:
            for shader in shaders:
                gl.glDeleteShader(shader)
        self._programs[(external, lut_on)] = program
        return program

    def draw(self, request: FitRequest, target: DrawTarget) -> None:
        """Finish `request`'s picture into `target`, whose size must be `target_size(request.size,
        request.turn)` (ValueError("fit_target_size") otherwise). Submits the draw;
        the caller flushes before the target goes to the screen. FitFailed("gpu_error") when GL
        reports an error."""
        size = target.size
        if tuple(size) != target_size(request.size, request.turn):
            raise ValueError("fit_target_size")
        source = request.source
        origin, per_x, per_y = _picture_axes(size, request.turn)
        left, top = fit_source_uv(0, 0, request.aspect, source.size, request.crop)
        right, bottom = fit_source_uv(1, 1, request.aspect, source.size, request.crop)
        gl = self._gl
        try:
            lut_on = request.lut is not None
            program = self._program(source.external, lut_on)
            self._bind(target, program)
            if lut_on:
                self._bind_lut(request.lut)
            gl.glActiveTexture(gl.GL_TEXTURE0)
            gl.glBindTexture(GL_TEXTURE_EXTERNAL_OES if source.external else gl.GL_TEXTURE_2D, source.texture)

            def uniform(name: str) -> int:
                return gl.glGetUniformLocation(program, name)

            gl.glUniform1i(uniform("media"), 0)
            gl.glUniform2f(uniform("picture_origin"), *origin)
            gl.glUniform2f(uniform("picture_per_x"), *per_x)
            gl.glUniform2f(uniform("picture_per_y"), *per_y)
            gl.glUniform4f(uniform("window"), left, top, right, bottom)
            gl.glUniform1i(uniform("video"), int(source.encoding == "bt709"))
            gl.glUniform1i(uniform("source_alpha"), int(source.alpha))
            look_uniforms(gl, program, request.operations, request.lights, dither_height=size[1],
                          lut_bound=lut_on)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
            failed = gl.glGetError() != gl.GL_NO_ERROR
        except FitFailed:
            raise
        except Exception as error:
            raise FitFailed("gpu_error") from error
        if failed:
            raise FitFailed("gpu_error")

    def _bind(self, target: DrawTarget, program: int) -> None:
        gl = self._gl
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, target.framebuffer)
        gl.glViewport(0, 0, *target.size)
        gl.glDisable(gl.GL_BLEND)
        gl.glBindVertexArray(self._vertex_array)
        gl.glUseProgram(program)

    def _bind_lut(self, table: bytes) -> None:
        """The paper profile's LUT on LUT_UNIT, uploaded when it is another table than the last."""
        gl = self._gl
        if not self._lut_texture:
            self._lut_texture, self._lut_table = int(gl.glGenTextures(1)), None
        if self._lut_table is not table and self._lut_table != table:
            upload_lut(gl, self._lut_texture, table)
            self._lut_table = table
        gl.glActiveTexture(gl.GL_TEXTURE0 + LUT_UNIT)
        gl.glBindTexture(gl.GL_TEXTURE_3D, self._lut_texture)

    def black(self, target: DrawTarget) -> None:
        """Fill `target` with opaque black: a black layer's picture."""
        gl = self._gl
        try:
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, target.framebuffer)
            gl.glViewport(0, 0, *target.size)
            gl.glClearColor(0.0, 0.0, 0.0, 1.0)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT)
            failed = gl.glGetError() != gl.GL_NO_ERROR
        except Exception as error:
            raise FitFailed("gpu_error") from error
        if failed:
            raise FitFailed("gpu_error")

    def close(self) -> None:
        """Delete the programs and the LUT texture."""
        gl = self._gl
        for program in self._programs.values():
            gl.glDeleteProgram(program)
        self._programs.clear()
        if self._lut_texture:
            gl.glDeleteTextures(1, [self._lut_texture])
            self._lut_texture, self._lut_table = 0, None
        if self._vertex_array:
            gl.glDeleteVertexArrays(1, [self._vertex_array])
            self._vertex_array = 0


__all__ = ["DrawTarget", "FitFailed", "FitPass", "FitRequest", "FitSource", "SourceEncoding", "target_size"]
