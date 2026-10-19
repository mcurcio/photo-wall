"""The pattern pass: what a screen shows in place of a Frame's pictures while its Display is tuned or
its Position is tried with nothing playing (render redesign S3, run ledger .claude/runs/render-planes/
s3-page.md, bead B2): the Output's test pattern (decision 0024 as 0028 amends it), a calibration
target (decisions 0026, 0027), or Position's commissioning grid, drawn once into the screen's ground,
a picture of the whole screen as it hangs and turned for it, black outside the Frame.

A pattern is never a plane of its own: it is the screen's ground, shown with no planes above it
(player/plane_renderer.py), so the display controller still scans out one opaque picture and the
look's mask stays last ("outside every Frame's opening stays black", contracts/tuning.py). GL on the
current context only, like player/fit_pass.py: no EGL, GBM or window system here, so the harness
draws it on software GL.

Rules this module owns (one home each):
- **Which pattern** (`PatternRequest`): the Output's `operations.test_pattern` when it has one, else
  the commissioning grid (player/geometry.py `final_pixel` reads the same field, so its precedence
  has one home there).
- **What a draw computes**: per target pixel (x, y) as laid out, player/geometry.py's scalar
  reference: `buffer_to_picture` over the whole screen's target gives the screen point, `placement_uv`
  the Frame's picture point (u, v) or None outside the Frame; then `final_pixel` with `inside` = the
  point is in the Frame, `opening_uv` = (u, v), the photo `commissioning_grid(u, v)` for the grid
  ((0, 0, 0) for a test pattern, which replaces it), the Frame's `lights`, its `lut`, `board` and
  `dither_at` = (x, y); written as stored 8-bit values, opaque. Within one level of 256.
- **The shader's pattern codes** (`PATTERN_CODES`): each TestPattern's code in the GLSL, 0 for the
  commissioning grid.

Rules it does NOT own: the patterns, the board and the grid (player/geometry.py `pattern_signal`,
`board_signal`, `board_point`, `commissioning_grid`; the GLSL here is written from them, their
constants written in from their homes), the look (player/look_shader.py), the board's raster and QR
layout (contracts/calibration_target.py), the QR's modules (player/calibration_target.py
`target_modules`), the target's layout and its map to the picture (player/fit_pass.py `target_size`,
`_picture_axes`), when a pattern is drawn again (player/plane_renderer.py).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from contracts.calibration_target import (
    QR_COPY_PLACES,
    QR_PERIOD,
    QR_QUIET_MODULES,
    QR_SQUARES,
    SCREEN_COLUMNS_MAX,
    SCREEN_ROWS,
    SQUARE_PX,
    QrModules,
    Surface,
    board_raster,
)
from contracts.display_turn import DisplayTurn
from contracts.frame_fit import Placement
from contracts.node_frame import PictureOperations
from contracts.tuning import SIGNAL_LEVELS, TARGET_PATTERNS, TestPattern
from player.fit_pass import _VERTEX, DrawTarget, FitFailed, _picture_axes, target_size
from player.geometry import (
    BLACK_LEVEL_PATCH,
    BLACK_LEVEL_STEPS,
    GREY_RAMP_STEPS,
    GRID_LINE,
    GRID_LINE_WIDTH,
    GRID_LINES,
    GRID_SLATE,
    BoardDraw,
    FrameLights,
)
from player.look_shader import LOOK_GLSL, LUT_UNIT, look_uniforms, upload_lut

PATTERN_CODES: Final[Mapping[TestPattern | None, int]] = {
    None: 0, TestPattern.WHITE: 1, TestPattern.GREY_RAMP: 2, TestPattern.BLACK_LEVEL: 3,
    TestPattern.TARGET_PRE_SOFTWARE: 4, TestPattern.TARGET_POST_SOFTWARE: 5}
"""Each pattern's code in the pass's GLSL (`#define PATTERN_<NAME> <code>`); None, 0: the commissioning
grid (`PATTERN_COMMISSIONING_GRID`). The one home of the codes."""


@dataclass(frozen=True)
class PatternRequest:
    """One screen ground with a Frame's pattern: the screen's `size` as it hangs (the target is
    player/fit_pass.py `target_size(size, turn)`), the Frame's `placement` on it
    (contracts/frame_fit.py `place`; black outside it), the screen's `turn`, the Output's picture
    `operations` (their `test_pattern` picks the pattern: module rule), the Frame's `lights`
    (player/geometry.py `picture_lights`; the commissioning grid's look), its paper profile's `lut`
    body (None: no LUT step) and, for a calibration target, its `board` (player/geometry.py
    `picture_board`; None otherwise)."""
    size: tuple[int, int]
    placement: Placement
    turn: DisplayTurn
    operations: PictureOperations
    lights: FrameLights
    lut: bytes | None
    board: BoardDraw | None


_BOARD_UNIT: Final = 2      # the screen board's raster (unit 0 unused; the LUT on LUT_UNIT)
_QR_UNIT: Final = 3         # the QR's modules


def _vec3(value: tuple[float, float, float]) -> str:
    return "vec3(" + ", ".join(repr(float(channel)) for channel in value) + ")"


# player/geometry.py's, contracts/calibration_target.py's and contracts/tuning.py's constants and the
# codes, written in so each has one home.
_PATTERN_CONSTANTS = (
    "".join(f"#define PATTERN_{'COMMISSIONING_GRID' if pattern is None else pattern.name} {code}\n"
            for pattern, code in PATTERN_CODES.items())
    + f"const float GREY_RAMP_STEPS = {float(GREY_RAMP_STEPS)!r};\n"
    f"const float BLACK_LEVEL_STEPS = {float(BLACK_LEVEL_STEPS)!r};\n"
    f"const float SIGNAL_LEVELS = {float(SIGNAL_LEVELS)!r};\n"
    f"const float BLACK_LEVEL_PATCH = {BLACK_LEVEL_PATCH!r};\n"
    f"const vec3 GRID_SLATE = {_vec3(GRID_SLATE)};\n"
    f"const vec3 GRID_LINE = {_vec3(GRID_LINE)};\n"
    f"const float GRID_LINES = {float(GRID_LINES)!r};\n"
    f"const float GRID_LINE_WIDTH = {GRID_LINE_WIDTH!r};\n"
    f"const float SCREEN_ROWS = {float(SCREEN_ROWS)!r};\n"
    f"const float SQUARE_PX = {float(SQUARE_PX)!r};\n"
    f"const int QR_PERIOD = {QR_PERIOD};\n"
    f"#define QR_PERIODS {-(-SCREEN_COLUMNS_MAX // QR_PERIOD)}\n"
    f"#define QR_COPY_COUNT {len(QR_COPY_PLACES)}\n"
    "const ivec2 QR_COPY_PLACES[QR_COPY_COUNT] = ivec2[QR_COPY_COUNT]("
    + ", ".join(f"ivec2({row}, {offset})" for row, offset in QR_COPY_PLACES) + ");\n"
    f"const int QR_SQUARES = {QR_SQUARES};\n"
    f"const float QR_QUIET_MODULES = {float(QR_QUIET_MODULES)!r};\n")
_STEP_SWITCHES = "// The step set: _fragment writes it here.\n"
# A target's rows run top-down from GL's row 0 (a framebuffer object), so gl_FragCoord is the target
# pixel as laid out (y down, centres at +0.5): `buffer_to_picture`'s point and the dither's pixel.
_FRAGMENT = """#version 300 es
precision highp float;
precision highp int;
out vec4 color;
""" + _STEP_SWITCHES + _PATTERN_CONSTANTS + LOOK_GLSL + """
uniform int pattern_code;           // PATTERN_CODES
// geometry.py `buffer_to_picture` over the whole screen, affine in the target's pixels: the screen's
// fractions at the target's origin and per pixel along x and y (the turn is in these three).
uniform vec2 screen_origin;
uniform vec2 screen_per_x;
uniform vec2 screen_per_y;
uniform vec2 screen_size;           // the screen's pixels as it hangs
uniform vec4 placement;             // the Frame's x, y, width, height on it, in those pixels
uniform sampler2D board_raster;
uniform sampler2D board_qr;
uniform int board_qr_modules;       // 0: no code, every QR square draws light
uniform int board_columns;
uniform float board_aspect;
uniform int board_portrait;
// geometry.py `pattern_signal`: a look pattern's or the Black level's encoded signal at the Frame's
// picture point.
vec3 pattern_signal(vec2 p) {
    p = clamp(p, 0.0, 1.0);
    float value = 0.0;
    if (pattern_code == PATTERN_WHITE) value = 1.0;
    else if (pattern_code == PATTERN_GREY_RAMP) {
        if (p.y < 0.5) value = min(floor(p.x*GREY_RAMP_STEPS), GREY_RAMP_STEPS-1.0)/(GREY_RAMP_STEPS-1.0);
        else value = p.x;
    } else {
        float band = (p.y-1.0/3.0)*3.0;
        if (band >= 0.0 && band < 1.0) {
            float position = p.x*BLACK_LEVEL_STEPS;
            float cell = min(floor(position), BLACK_LEVEL_STEPS-1.0);
            float margin = (1.0-BLACK_LEVEL_PATCH)/2.0;
            float across = position-cell;
            if (across >= margin && across < 1.0-margin && band >= margin && band < 1.0-margin)
                value = (cell+1.0)/SIGNAL_LEVELS;
        }
    }
    return vec3(value);
}
// geometry.py `commissioning_grid`: slate with light lines, in linear light.
vec3 commissioning_grid(vec2 p) {
    vec2 across = fract(p*GRID_LINES);
    return across.x < GRID_LINE_WIDTH || across.y < GRID_LINE_WIDTH ? GRID_LINE : GRID_SLATE;
}
// geometry.py `board_point`: the screen board's point (column, row) in squares at the Frame's
// picture point, its columns centred along the long side, a quarter turn clockwise in a portrait one.
vec2 board_point(vec2 p) {
    float n = board_aspect*SCREEN_ROWS;
    float offset = (n-float(board_columns))*0.5;
    if (board_portrait == 0) return vec2(p.x*n-offset, p.y*SCREEN_ROWS);
    return vec2(p.y*n-offset, (1.0-p.x)*SCREEN_ROWS);
}
// geometry.py `board_signal`: black off the board; in a QR copy's squares its module (dark 0; light
// and the quiet zone 1; no code: all light), the modules and their quiet zone filling QR_SQUARES;
// else the raster's pixel (row 0 at the board's top).
float board_signal(vec2 point) {
    if (any(lessThan(point, vec2(0.0))) || point.x >= float(board_columns) || point.y >= SCREEN_ROWS)
        return 0.0;
    for (int k = 0; k < QR_PERIODS; k++) {
        for (int c = 0; c < QR_COPY_COUNT; c++) {
            ivec2 place = QR_COPY_PLACES[c];
            int column = k*QR_PERIOD+place.y;
            if (column+QR_SQUARES > board_columns) continue;
            vec2 square = point-vec2(float(column), float(place.x));
            if (any(lessThan(square, vec2(0.0))) || any(greaterThanEqual(square, vec2(float(QR_SQUARES)))))
                continue;
            if (board_qr_modules == 0) return 1.0;
            float side = float(board_qr_modules)+2.0*QR_QUIET_MODULES;
            ivec2 cell = ivec2(min(floor(square/float(QR_SQUARES)*side), vec2(side-1.0)))-ivec2(int(QR_QUIET_MODULES));
            if (all(greaterThanEqual(cell, ivec2(0))) && all(lessThan(cell, ivec2(board_qr_modules))))
                return texelFetch(board_qr, cell, 0).r > 0.5 ? 0.0 : 1.0;
            return 1.0;
        }
    }
    return texelFetch(board_raster, ivec2(floor(point*SQUARE_PX)), 0).r;
}
void main() {
    // geometry.py `placement_uv`: the Frame's picture point, or outside it (black: the mask last).
    vec2 screen = screen_origin+gl_FragCoord.x*screen_per_x+gl_FragCoord.y*screen_per_y;
    vec2 uv = (screen*screen_size-placement.xy)/placement.zw;
    if (any(lessThan(uv, vec2(0.0))) || any(greaterThanEqual(uv, vec2(1.0)))) {
        color = vec4(0.0, 0.0, 0.0, 1.0);
        return;
    }
    ivec2 at = ivec2(gl_FragCoord.xy);
    // geometry.py `final_pixel`: the grid is the Frame's photo under its look; the look patterns and
    // the post-software target go through the Display's steps only; the Black level and the
    // pre-software target are raw.
    if (pattern_code == PATTERN_COMMISSIONING_GRID) {
        color = vec4(look(commissioning_grid(uv), uv, at), 1.0);
        return;
    }
    vec3 encoded;
    if (pattern_code == PATTERN_TARGET_PRE_SOFTWARE || pattern_code == PATTERN_TARGET_POST_SOFTWARE)
        encoded = vec3(board_signal(board_point(uv)));
    else
        encoded = pattern_signal(uv);
    if (pattern_code == PATTERN_BLACK_LEVEL || pattern_code == PATTERN_TARGET_PRE_SOFTWARE) {
        color = vec4(encoded, 1.0);
        return;
    }
    color = vec4(displayed(srgb_decoded(encoded), at), 1.0);
}
"""


def _fragment(lut_on: bool) -> str:
    """The pass's fragment shader for one step set (`PatternPass.__init__`)."""
    return _FRAGMENT.replace(_STEP_SWITCHES, f"const int lut_on = {int(lut_on)};\n")


class PatternPass:
    """The pattern pass's programs on the current GL context (module rules)."""

    def __init__(self, gl: Any) -> None:
        """Compile and link the programs (one per step set: the LUT step a compile-time constant, as
        player/fit_pass.py) and upload the screen board's raster (contracts/calibration_target.py
        `board_raster(Surface.SCREEN)`, R8, once) with `gl`, PyOpenGL's GLES 3 module or one like it.
        player/fit_pass.py `FitFailed("gpu_error")` when a program does not link."""
        self._gl = gl
        self._programs: dict[bool, int] = {}
        self._lut_texture = 0
        self._lut_table: bytes | None = None
        self._board_texture = 0
        self._qr_texture = 0
        self._qr_modules: QrModules | None = None
        self._vertex_array = 0
        try:
            self._vertex_array = int(gl.glGenVertexArrays(1))
            for lut_on in (False, True):
                self._programs[lut_on] = self._link(_fragment(lut_on))
            raster = board_raster(Surface.SCREEN)
            gl.glActiveTexture(gl.GL_TEXTURE0 + _BOARD_UNIT)
            self._board_texture = self._texture(raster.width, raster.height, raster.pixels)
            gl.glActiveTexture(gl.GL_TEXTURE0)
            failed = gl.glGetError() != gl.GL_NO_ERROR
        except Exception as error:
            self.close()
            if isinstance(error, FitFailed):
                raise
            raise FitFailed("gpu_error") from error
        if failed:
            self.close()
            raise FitFailed("gpu_error")

    def _link(self, fragment: str) -> int:
        """One program of the pass's vertex shader (player/fit_pass.py's whole-target triangle) and
        `fragment`; FitFailed("gpu_error", log) when either does not compile or they do not link."""
        gl = self._gl
        shaders: list[int] = []
        program = 0
        try:
            for source, kind in ((_VERTEX, gl.GL_VERTEX_SHADER), (fragment, gl.GL_FRAGMENT_SHADER)):
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
        except Exception:
            if program:
                gl.glDeleteProgram(program)
            raise
        finally:
            for shader in shaders:
                gl.glDeleteShader(shader)
        return program

    def _texture(self, width: int, height: int, data: bytes) -> int:
        """A new R8 texture of `data` (rows top first) bound on the active unit, read with
        texelFetch (nearest, so it is complete without mipmaps)."""
        gl = self._gl
        texture = int(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
        for parameter, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST),
                                 (gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST),
                                 (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                                 (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
            gl.glTexParameteri(gl.GL_TEXTURE_2D, parameter, value)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_R8, width, height, 0, gl.GL_RED, gl.GL_UNSIGNED_BYTE, data)
        return texture

    def draw(self, request: PatternRequest, target: DrawTarget) -> None:
        """Draw `request`'s ground into `target`, whose size must be the whole screen's target
        (ValueError("pattern_target_size") otherwise); a calibration target without `board` is
        ValueError("board"). The QR's modules are uploaded when `request.board.modules` differs from
        the last drawn. Submits the draw; the caller fences it (player/gpu.py `Gpu.fence`).
        FitFailed("gpu_error") when GL reports an error."""
        size = tuple(target.size)
        if size != target_size(request.size, request.turn):
            raise ValueError("pattern_target_size")
        pattern = request.operations.test_pattern
        board = request.board
        if pattern in TARGET_PATTERNS and board is None:
            raise ValueError("board")
        origin, per_x, per_y = _picture_axes(size, request.turn)
        placement = request.placement
        gl = self._gl
        try:
            lut_on = request.lut is not None
            program = self._programs[lut_on]
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, target.framebuffer)
            gl.glViewport(0, 0, *size)
            gl.glDisable(gl.GL_BLEND)
            gl.glBindVertexArray(self._vertex_array)
            gl.glUseProgram(program)
            if lut_on:
                self._bind_lut(request.lut)
            modules = self._bind_board(board.modules if board is not None else None)

            def uniform(name: str) -> int:
                return gl.glGetUniformLocation(program, name)

            gl.glUniform1i(uniform("pattern_code"), PATTERN_CODES[pattern])
            gl.glUniform2f(uniform("screen_origin"), *origin)
            gl.glUniform2f(uniform("screen_per_x"), *per_x)
            gl.glUniform2f(uniform("screen_per_y"), *per_y)
            gl.glUniform2f(uniform("screen_size"), *(float(side) for side in request.size))
            gl.glUniform4f(uniform("placement"), *(float(value) for value in (
                placement.x, placement.y, placement.width, placement.height)))
            gl.glUniform1i(uniform("board_raster"), _BOARD_UNIT)
            gl.glUniform1i(uniform("board_qr"), _QR_UNIT)
            gl.glUniform1i(uniform("board_qr_modules"), modules)
            gl.glUniform1i(uniform("board_columns"), board.columns if board is not None else 0)
            gl.glUniform1f(uniform("board_aspect"), board.aspect if board is not None else 1.0)
            gl.glUniform1i(uniform("board_portrait"), int(board is not None and board.portrait))
            look_uniforms(gl, program, request.operations, request.lights, dither_height=size[1],
                          lut_bound=lut_on)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
            failed = gl.glGetError() != gl.GL_NO_ERROR
        except Exception as error:
            raise FitFailed("gpu_error") from error
        if failed:
            raise FitFailed("gpu_error")

    def _bind_lut(self, table: bytes) -> None:
        """The paper profile's LUT on LUT_UNIT, uploaded when it is another table than the last.
        Texture unit 0 is the active unit after."""
        gl = self._gl
        if not self._lut_texture:
            self._lut_texture, self._lut_table = int(gl.glGenTextures(1)), None
        if self._lut_table is not table and self._lut_table != table:
            upload_lut(gl, self._lut_texture, table)
            self._lut_table = table
        gl.glActiveTexture(gl.GL_TEXTURE0 + LUT_UNIT)
        gl.glBindTexture(gl.GL_TEXTURE_3D, self._lut_texture)
        gl.glActiveTexture(gl.GL_TEXTURE0)

    def _bind_board(self, modules: QrModules | None) -> int:
        """The board's raster on _BOARD_UNIT and the QR of `modules` on _QR_UNIT (R8, 255 for a dark
        module), uploaded when they differ from the last drawn; the module count, 0 for None (every
        QR square draws light, the texture unread). Texture unit 0 is the active unit after."""
        gl = self._gl
        try:
            gl.glActiveTexture(gl.GL_TEXTURE0 + _BOARD_UNIT)
            gl.glBindTexture(gl.GL_TEXTURE_2D, self._board_texture)
            gl.glActiveTexture(gl.GL_TEXTURE0 + _QR_UNIT)
            if modules is None:
                return 0
            if not self._qr_texture or modules != self._qr_modules:
                if self._qr_texture:
                    gl.glDeleteTextures(1, [self._qr_texture])
                    self._qr_texture, self._qr_modules = 0, None
                data = bytes(255 if dark else 0 for row in modules for dark in row)
                self._qr_texture = self._texture(len(modules), len(modules), data)
                self._qr_modules = modules
            gl.glBindTexture(gl.GL_TEXTURE_2D, self._qr_texture)
            return len(modules)
        finally:
            gl.glActiveTexture(gl.GL_TEXTURE0)

    def close(self) -> None:
        """Delete the programs and the board's and QR's textures."""
        gl = self._gl
        for program in self._programs.values():
            gl.glDeleteProgram(program)
        self._programs.clear()
        for texture in (self._lut_texture, self._board_texture, self._qr_texture):
            if texture:
                gl.glDeleteTextures(1, [texture])
        self._lut_texture = self._board_texture = self._qr_texture = 0
        self._lut_table = self._qr_modules = None
        if self._vertex_array:
            gl.glDeleteVertexArrays(1, [self._vertex_array])
            self._vertex_array = 0


__all__ = ["PATTERN_CODES", "PatternPass", "PatternRequest"]
