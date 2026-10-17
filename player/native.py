"""GTK3/GStreamer Renderer. Importing this module needs no native dependencies.

All public methods belong to one GLib thread. Streaming callbacks only retain
bounded Gst samples; they never touch GTK/GL or control-plane state.
"""

from __future__ import annotations

import math
import os
import threading
import time
import weakref
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from contracts.calibration_target import (
    QR_COPY_PLACES,
    QR_PERIOD,
    QR_QUIET_MODULES,
    QR_SQUARES,
    SCREEN_COLUMNS_MAX,
    SCREEN_ROWS,
    SQUARE_PX,
    Surface,
    TargetCode,
    board_raster,
)
from contracts.lut import LUT_GRID, LutRef
from contracts.models import MAT_LIGHTS_MAX, AppliedCalibration, Mat
from contracts.node_frame import PictureOperations
from contracts.tuning import TARGET_PATTERNS, TestPattern
from player.calibration_target import target_modules
from player.geometry import (
    BLACK_LEVEL_PATCH,
    BLACK_LEVEL_STEPS,
    GREY_RAMP_STEPS,
    LUMA,
    ROLLOFF_FADE_POWER,
    SIGNAL_LEVELS,
    BoardDraw,
    Matrix,
    MatShadow,
    board_draw,
    homography,
    inverse,
    mat_shadow,
    output_to_picture,
    peak,
    photo_rect,
    rolloff_skipped,
)
from player.look import changed, look, retimed, until_change
from player.luts import LutSource
from player.mainloop import FrameFollower, Tick, frame_time, watch_bus
from player.rendering import (
    PRESENTATION_FRESHNESS,
    REDRAW_RENEWAL,
    CapacityResult,
    LocalLayer,
    OutputComposition,
    PrepareResult,
    PresentationResult,
)
from player.visibility import visible_now


@dataclass(frozen=True)
class NativeOutput:
    output_id: str
    app_id: str
    width: int = 1920
    height: int = 1080

    def __post_init__(self):
        if not self.output_id or not self.app_id or len(self.app_id) > 255:
            raise ValueError("bounded Output and Wayland app IDs required")
        if not (1 <= self.width <= 8192 and 1 <= self.height <= 8192):
            raise ValueError("Output dimensions outside native bounds")


class SampleMailbox:
    """Two owned samples, with terminal close and generation-safe publication."""

    def __init__(self):
        self._lock = threading.Lock()
        self._queue: deque[tuple[int, Any]] = deque(maxlen=2)
        self._closed = False
        self._generation = 0

    def segment(self, generation: int) -> None:
        with self._lock:
            self._generation = generation
            self._queue.clear()

    def publish(self, sample: Any) -> None:
        with self._lock:
            if not self._closed:
                self._queue.append((self._generation, sample))

    def latest(self, generation: int) -> Any | None:
        with self._lock:
            entries = tuple(self._queue)
            self._queue.clear()
        return next((sample for gen, sample in reversed(entries) if gen == generation), None)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._queue.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._queue)


def _rgba_layout(width: int, height: int, stride: int, offset: int, size: int) -> int:
    if not (0 < width <= 16384 and 0 < height <= 16384):
        raise ValueError("invalid decoded dimensions")
    row = width*4
    if stride < row or offset < 0 or offset+(height-1)*stride+row > size:
        raise ValueError("invalid RGBA stride/offset/buffer length")
    return row


def packed_rgba(data: bytes | memoryview, width: int, height: int,
                stride: int, offset: int = 0) -> bytes:
    """Validate negotiated layout before bounded native byte slicing/copying."""
    row = _rgba_layout(width, height, stride, offset, len(data))
    if stride == row:
        return bytes(data[offset:offset+height*row])
    # This is a bounded row copy, never a Python pixel-processing loop.
    return b"".join(data[offset+y*stride:offset+y*stride+row] for y in range(height))


def _fingerprint(composition: OutputComposition) -> tuple:
    return (composition.binding, composition.calibration, composition.fallback,
            tuple((local.layer, local.path) for local in composition.layers))


def _renewal_due(ack: _Draw | None, now: float) -> bool:
    """Nothing acknowledged yet, or the last draw is REDRAW_RENEWAL old: paint again."""
    return ack is None or now - ack.completed_at >= REDRAW_RENEWAL


def _trial_key(frames, composition: OutputComposition, grant) -> object:
    """The active calibration trial's identity, so a trial edit or expiry is a change."""
    if frames is None:
        return None
    try:
        trial = frames.trial(composition, grant)
    except Exception:
        return object()     # unreadable here: always redraw, and _render reports it
    if trial is None:
        return None
    candidate = trial.candidate
    return (candidate.trial_id, candidate.generation, candidate.sequence,
            candidate.candidate_sha256)


def _trial_drawn(draw: _Draw) -> bool:
    """Whether `draw` shows a calibration trial (its edits arrive at any frame)."""
    return draw.applied_calibration is not None and draw.applied_calibration.trial_id is not None


def _drawn_fit(draw: _Draw):
    """The fit a draw shows: its applied calibration (a trial's), else its composition's."""
    return (draw.applied_calibration.calibration if draw.applied_calibration
            else draw.composition.calibration).fit


def _drawn_mat(draw: _Draw) -> Mat:
    """The mat a draw shows: its trial's (Position's trial carries the draft mat), else its
    binding's."""
    return draw.applied_mat if draw.applied_mat is not None else draw.composition.binding.mat


# The final pass's `test_pattern` uniform per contracts/tuning.py TestPattern (0: the photos). One
# home: the shader's PATTERN_* names are written in from here (`_FINAL_CONSTANTS`).
_PATTERN_CODES: dict[TestPattern | None, int] = {
    None: 0, TestPattern.WHITE: 1, TestPattern.GREY_RAMP: 2, TestPattern.BLACK_LEVEL: 3,
    TestPattern.TARGET_PRE_SOFTWARE: 4, TestPattern.TARGET_POST_SOFTWARE: 5}


def _final_uniforms(gl, program: int, operations: PictureOperations | None, opening,
                    shadow: MatShadow | None, *, lut_drawn: bool,
                    target: tuple[int, BoardDraw] | None = None) -> None:
    """The final pass's operations (neutral when None), the Frame's mat shadow (None: none) and the
    per-Output mask's opening. Each step's switch is player/geometry.py's skip rule, worked here,
    so a neutral step is never computed by the shader either. `lut_drawn`: the LUT
    `operations.lut` names is bound on texture unit 1 (`NativeRenderer._bind_lut`), so step 1
    mixes it in; otherwise step 1 is skipped. A test pattern (`operations.test_pattern`) needs the
    opening's unit square, so `to_opening` is set whenever a pattern or a shadow is on; with no
    Frame drawn yet the opening is `_NO_OPENING`, off the output, and the pattern stays unseen (a
    known limit: an Output shows a pattern only once a Frame has drawn there). `target`: for a
    calibration target, the QR's module count bound on _TARGET_QR_UNIT (0: no code, every QR
    square draws light) and the opening's BoardDraw (`board_draw`: its columns, aspect and turn;
    its modules unread, the count stands for them); the board's raster is bound on
    _TARGET_LAYOUT_UNIT (`NativeRenderer._bind_target`). None: no columns, so nothing is drawn."""
    def uniform(name: str) -> int:
        return gl.glGetUniformLocation(program, name)

    multipliers, light, contrast, gamma = _NEUTRAL if operations is None else (
        operations.rgb_multipliers, operations.light_factor, operations.contrast_slope,
        operations.gamma_exponent)
    gl.glUniform3f(uniform("rgb_multipliers"), *multipliers)
    gl.glUniform1f(uniform("light_factor"), light)
    gl.glUniform1f(uniform("contrast_slope"), contrast)
    gl.glUniform1f(uniform("gamma_exponent"), gamma)
    lut_on = lut_drawn and operations is not None and operations.lut is not None
    gl.glUniform1i(uniform("lut_on"), int(lut_on))
    gl.glUniform1i(uniform("lut"), _LUT_UNIT)       # never unit 0: two sampler types may not share one
    gl.glUniform1f(uniform("lut_amount"), operations.lut.amount if lut_on else 0.0)
    saturation_on = operations is not None and operations.saturation != 1.0
    gl.glUniform1i(uniform("saturation_on"), int(saturation_on))
    gl.glUniform1f(uniform("saturation"), operations.saturation if saturation_on else 1.0)
    rolloff_on = operations is not None and not rolloff_skipped(operations)
    gl.glUniform1i(uniform("rolloff_on"), int(rolloff_on))
    gl.glUniform1f(uniform("highlight_knee"), operations.highlight_knee if rolloff_on else 1.0)
    gl.glUniform1f(uniform("peak"), peak(operations) if rolloff_on else 1.0)
    floor_on = operations is not None and operations.black_floor > 0
    gl.glUniform1i(uniform("floor_on"), int(floor_on))
    gl.glUniform1f(uniform("black_floor"), operations.black_floor if floor_on else 0.0)
    pattern = None if operations is None else operations.test_pattern
    gl.glUniform1i(uniform("test_pattern"), _PATTERN_CODES[pattern])
    gl.glUniform1i(uniform("target_layout"), _TARGET_LAYOUT_UNIT)   # each sampler on its own unit
    gl.glUniform1i(uniform("target_qr"), _TARGET_QR_UNIT)
    modules, draw = target if target is not None else (0, None)
    gl.glUniform1i(uniform("target_qr_modules"), modules)
    gl.glUniform1i(uniform("target_columns"), draw.columns if draw is not None else 0)
    gl.glUniform1f(uniform("target_aspect"), draw.aspect if draw is not None else 1.0)
    gl.glUniform1i(uniform("target_portrait"), int(draw is not None and draw.portrait))
    if shadow is not None:
        _set_matrix(gl, program, "to_opening", shadow.to_opening)
    elif pattern is not None:
        _set_matrix(gl, program, "to_opening", inverse(homography(opening)))
    gl.glUniform1i(uniform("mat_on"), int(shadow is not None))
    if shadow is not None:
        lights = shadow.lights + (None,) * (MAT_LIGHTS_MAX - len(shadow.lights))
        gl.glUniform2f(uniform("mat_size"), *shadow.size_mm)
        gl.glUniform1i(uniform("light_count"), len(shadow.lights))
        gl.glUniform2fv(uniform("light_toward"), MAT_LIGHTS_MAX,
                        [v for light in lights for v in (light.toward if light else (0.0, 0.0))])
        gl.glUniform2fv(uniform("light_band"), MAT_LIGHTS_MAX,
                        [v for light in lights for v in ((light.dark_until_mm, light.lit_from_mm)
                                                         if light else (0.0, 0.0))])
        gl.glUniform3fv(uniform("light_weight"), MAT_LIGHTS_MAX,
                        [v for light in lights for v in (light.weight if light else (0.0, 0.0, 0.0))])
    _set_opening(gl, program, opening)


def _set_matrix(gl, program: int, name: str, matrix: Matrix) -> None:
    """A player/geometry.py row-major 3x3 as a GLSL mat3 uniform (column-major, untransposed)."""
    gl.glUniformMatrix3fv(gl.glGetUniformLocation(program, name), 1, False,
                          [matrix[row*3+column] for column in range(3) for row in range(3)])


def _picture_key(frames, composition: OutputComposition, luts: LutSource) -> object:
    """The Output's newest picture operations, so a picture change is a change to redraw; with a
    LUT named, whether it is held yet (the LUTs' generation), so its arrival redraws and the tag
    can then name the change (rule 3)."""
    if frames is None:
        return None
    try:
        operations = frames.picture(composition.binding.output_id)
    except Exception:
        return object()     # unreadable here: always redraw, and _render reports it
    if operations is None or operations.lut is None:
        return operations
    return operations, luts.generation()


def _shown(composition: OutputComposition, generations: tuple, sample_serials: tuple,
           frames, grant, luts: LutSource) -> tuple:
    """Everything a planning tick can change in a surface's pixels (G8 render on change,
    E-G8-3): layers, decoder generations, each video's newest sample, the calibration trial and
    the Output's picture operations (1c). How the layers look over time (a fade) is no tick's,
    and while a surface follows its frame clock it compares these on every frame
    (`NativeRenderer._moved`)."""
    return (_fingerprint(composition), generations, sample_serials,
            _trial_key(frames, composition, grant),
            _picture_key(frames, composition, luts))


def _gl_text(value: object) -> str | None:
    """A `glGetString` result as text (PyOpenGL answers bytes); None if the context had none."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return (value.strip() or None) if isinstance(value, str) else None


def diagnostic_text(output_id: str, player_id: str | None = None,
                    serial: str | None = None,
                    central_link_state: Literal["connecting", "reachable", "retrying"] = "connecting",
                    configuration_received: bool = False) -> str:
    """Local equipment status, never an authored Scene or playback claim."""
    lines = ["PHOTO WALL", "OS and Player running", f"Output {output_id}"]
    if central_link_state == "retrying":
        link = ("Central: retrying; last configuration received" if configuration_received
                else "Central: retrying; no configuration received")
    elif central_link_state == "reachable":
        link = ("Central: reachable; configuration received" if configuration_received
                else "Central: reachable; awaiting configuration")
    else:
        link = ("Central: connecting; last configuration received" if configuration_received
                else "Central: connecting")
    lines.append(link)
    if player_id is not None:
        lines.append("No Frame assigned")
        lines.append(f"Player p-…{player_id[-8:]}")
    if serial:
        lines.append(f"Serial {serial}")
    return "\n".join(lines)


@dataclass
class _Decoder:
    local: LocalLayer
    pipeline: Any
    sink: Any
    mailbox: SampleMailbox = field(default_factory=SampleMailbox)
    incarnation: int = 0
    generation: int = 0
    seek_sequence: int = 0
    target: float = 0
    sample: Any = None
    sample_serial: int = 0
    prepared: bool = False
    playing: bool = False
    eos: bool = False
    failure: str | None = None
    bus: Any = None
    # Every (GObject, handler id) and (Gst.Pad, probe id) this decoder made: _destroy_decoder
    # undoes each, so no element keeps a callback once the decoder is gone.
    handlers: list[tuple[Any, int]] = field(default_factory=list)
    probes: list[tuple[Any, int]] = field(default_factory=list)
    retry_at: float = 0
    deadline: float = 0


def _bus_message(Gst, decoder: _Decoder, message) -> None:
    if message.type == Gst.MessageType.ERROR:
        decoder.failure = "decode"
        decoder.prepared = False
        decoder.retry_at = time.monotonic()+1
    elif message.type == Gst.MessageType.EOS:
        decoder.eos = True


@dataclass
class _Draw:
    serial: int
    composition: OutputComposition
    generations: tuple[tuple[str, int, int], ...]
    completed_at: float = 0
    applied_calibration: AppliedCalibration | None = None
    # The trial's mat while a trial is drawn (Position's trial carries the draft mat); None: the
    # binding's (`_drawn_mat`).
    applied_mat: Mat | None = None
    primitives: tuple | None = None
    # What this draw shows (_shown): the one record present() compares against, so "a draw
    # is owed" is just "the newest draw, pending or else acknowledged, shows something else".
    shown: tuple | None = None
    # The frame time it was drawn at (`NativeRenderer._frame_time`); None until drawn.
    at: float | None = None


@dataclass
class _Surface:
    output: NativeOutput
    window: Any
    area: Any
    diagnostic: Any = None
    diagnostic_label: Any = None
    identify_banner: Any = None
    pending: _Draw | None = None
    acknowledged: _Draw | None = None
    # A failure lasts one episode: it ends when the window is mapped again or the display host
    # hands the Output a new grant. Read it through failed(), write it through fail().
    failure: str | None = None
    episode: int = 0
    failed_episode: int | None = None
    grant_key: tuple[UUID, int, int] | None = None
    textures: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    targets: list[tuple[int, int]] = field(default_factory=list)
    size: tuple[int, int] = (0, 0)
    programs: tuple[int, int] | None = None
    vao: int = 0
    # The Output's picture operations from the display host's shell (frame protocol v4), read
    # each render; None: neutral (none received yet, or no display host).
    operations: PictureOperations | None = None
    # The newest picture change this surface drew complete (`_tagged_change`); None before any.
    complete_change: int | None = None
    # The LUT texture (GL_TEXTURE_3D on _LUT_UNIT) and the LUT it holds; 0 and None: none yet.
    lut_texture: int = 0
    lut_ref: LutRef | None = None
    # The calibration target's textures (decision 0027): the screen board's raster (R8 on
    # _TARGET_LAYOUT_UNIT, uploaded once per GL context) and the QR (R8 on _TARGET_QR_UNIT) of
    # `target_code`, the (player id, Output, picture change) it names, rebuilt only when that
    # changes; 0 and None: none yet.
    target_layout: int = 0
    target_qr: int = 0
    target_code: tuple[str, str, int] | None = None
    target_qr_modules: int = 0
    # Watches the surface's frame clock while what its drawn composition shows may change
    # (`NativeRenderer._frame`); a still picture is not followed.
    follower: FrameFollower | None = None

    def fail(self, reason: str) -> None:
        self.failure, self.failed_episode = reason, self.episode

    def failed(self) -> str | None:
        """The failure of this episode; one from an earlier episode is cleared."""
        if self.failed_episode == self.episode:
            return self.failure
        self.failure = self.failed_episode = None
        return None

    def begin_episode(self) -> None:
        self.episode += 1

    def _tagged_change(self, operations: PictureOperations | None, lut_drawn: bool) -> int | None:
        """The picture change a draw's tag names (the one home of the Done rule, design rule 3:
        "only Done waits for" a LUT): `operations.picture_change` when the draw was complete (no
        LUT named, or its LUT drawn), else the newest change this surface drew complete; None
        before any. A change whose LUT is still loading is drawn with the sliders but never
        claimed, so the picture report's presented change, and so Done, waits for the LUT."""
        if operations is not None and (operations.lut is None or lut_drawn):
            self.complete_change = operations.picture_change
        return self.complete_change


def _observe_grant(surface: _Surface, grant) -> None:
    """A grant other than the surface's last one begins a new episode."""
    if grant is None:
        return
    key = (grant.grant_id, grant.binding_generation, grant.config_revision)
    if key != surface.grant_key:
        surface.grant_key = key
        surface.begin_episode()


_VERTEX = """#version 300 es
precision highp float;
out vec2 uv;
void main() {
    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    uv = vec2(p.x, 1.0-p.y);
    gl_Position = vec4(p*2.0-1.0, 0.0, 1.0);
}
"""
# The opening (the Frame's four corners, output fractions, origin top-left, clockwise): one GLSL
# copy of player/geometry.py `inside_opening`, used by the position pass (the per-Frame mask) and
# the final pass (the per-Output mask). Edges are inside.
_OPENING = """
uniform vec2 opening[4];
bool inside_opening(vec2 point) {
    for (int i = 0; i < 4; i++) {
        vec2 a = opening[i];
        vec2 b = opening[(i+1)%4];
        if ((b.x-a.x)*(point.y-a.y)-(b.y-a.y)*(point.x-a.x) < 0.0) return false;
    }
    return true;
}
"""
# The position pass (player/geometry.py `picture_source_uv`): the per-Frame mask first, then the
# fit's one matrix onto the picture's unit square, then the photo's rectangle (Photo fit).
_LAYER = """#version 300 es
precision highp float;
in vec2 uv;
out vec4 color;
uniform sampler2D media;
uniform mat3 output_to_picture;
uniform vec4 photo;
uniform int video;
uniform int black;
uniform int commissioning;
uniform float opacity;
""" + _OPENING + """
vec3 linear_rgb(vec3 value) {
    if (video == 1) {
        return mix(pow((value+0.099)/1.099, vec3(1.0/0.45)), value/4.5,
                   lessThan(value, vec3(0.081)));
    }
    return mix(pow((value+0.055)/1.055, vec3(2.4)), value/12.92,
               lessThanEqual(value, vec3(0.04045)));
}
void main() {
    if (!inside_opening(uv)) discard;
    vec3 q = output_to_picture * vec3(uv, 1.0);
    vec2 p = q.xy/q.z;
    if (any(lessThan(p, vec2(0.0))) || any(greaterThan(p, vec2(1.0)))) discard;
    if (commissioning == 1) {
        vec2 grid = fract(mix(photo.xy, photo.zw, p)*10.0);
        float line = float(grid.x < 0.035 || grid.y < 0.035);
        color = vec4(mix(vec3(0.2, 0.3, 0.45), vec3(0.9), line), 1.0);
        return;
    }
    if (black == 1) { color=vec4(0.0, 0.0, 0.0, opacity); return; }
    vec4 sample_color = texture(media, mix(photo.xy, photo.zw, p));
    float alpha = sample_color.a * opacity;
    color = vec4(linear_rgb(sample_color.rgb)*alpha, alpha);
}
"""
# The final pass (player/geometry.py `final_pixel`): contracts/node_frame.py PictureOperations
# in the print look's order, each step switched off (never computed) at its neutral value, then
# the Frame's mat shadow, then the per-Output mask LAST, so black stays black outside the opening
# whatever the picture settings and the shadow (owner 2026-10-10 q2). A test pattern (display
# tuning, step 0) replaces the composed picture here, so Position's grid and every Frame layer are
# replaced with no change to the layer pass; the Black level skips every step and the mat.
# geometry.py's constants are written in (`_FINAL_CONSTANTS`), so each has one home.
_LUT_UNIT = 1      # the LUT's texture unit; the picture is on unit 0
_TARGET_LAYOUT_UNIT = 2     # the screen board's raster (contracts.calibration_target.board_raster)
_TARGET_QR_UNIT = 3         # its QR's modules
_FINAL_CONSTANTS = (
    f"#define MAT_LIGHTS_MAX {MAT_LIGHTS_MAX}\n"
    f"const float LUT_GRID = {float(LUT_GRID)!r};\n"
    f"const vec3 LUMA = vec3({', '.join(repr(weight) for weight in LUMA)});\n"
    f"const float ROLLOFF_FADE_POWER = {ROLLOFF_FADE_POWER!r};\n"
    f"const float GREY_RAMP_STEPS = {float(GREY_RAMP_STEPS)!r};\n"
    f"const float BLACK_LEVEL_STEPS = {float(BLACK_LEVEL_STEPS)!r};\n"
    f"const float SIGNAL_LEVELS = {float(SIGNAL_LEVELS)!r};\n"
    f"const float BLACK_LEVEL_PATCH = {BLACK_LEVEL_PATCH!r};\n"
    + "".join(f"#define PATTERN_{pattern.name} {code}\n"
              for pattern, code in _PATTERN_CODES.items() if pattern is not None)
    + f"const float SCREEN_ROWS = {float(SCREEN_ROWS)!r};\n"
    f"const float SQUARE_PX = {float(SQUARE_PX)!r};\n"
    f"const int QR_PERIOD = {QR_PERIOD};\n"
    f"#define QR_PERIODS {-(-SCREEN_COLUMNS_MAX // QR_PERIOD)}\n"
    f"#define QR_COPY_COUNT {len(QR_COPY_PLACES)}\n"
    f"const ivec2 QR_COPY_PLACES[QR_COPY_COUNT] = ivec2[QR_COPY_COUNT]("
    + ", ".join(f"ivec2({row}, {offset})" for row, offset in QR_COPY_PLACES) + ");\n"
    f"const int QR_SQUARES = {QR_SQUARES};\n"
    f"const float QR_QUIET_MODULES = {float(QR_QUIET_MODULES)!r};\n")
_FINAL = """#version 300 es
precision highp float;
precision highp sampler3D;
in vec2 uv;
out vec4 color;
""" + _FINAL_CONSTANTS + """
uniform sampler2D picture;
uniform vec3 rgb_multipliers;
uniform float light_factor;
uniform float contrast_slope;
uniform float gamma_exponent;
uniform int lut_on;
uniform sampler3D lut;
uniform float lut_amount;
uniform int saturation_on;
uniform float saturation;
uniform int rolloff_on;
uniform float highlight_knee;
uniform float peak;
uniform int floor_on;
uniform float black_floor;
uniform int test_pattern;
uniform sampler2D target_layout;
uniform sampler2D target_qr;
uniform int target_qr_modules;
uniform int target_columns;
uniform float target_aspect;
uniform int target_portrait;
uniform int mat_on;
uniform mat3 to_opening;
uniform vec2 mat_size;
uniform int light_count;
uniform vec2 light_toward[MAT_LIGHTS_MAX];
uniform vec2 light_band[MAT_LIGHTS_MAX];
uniform vec3 light_weight[MAT_LIGHTS_MAX];
""" + _OPENING + """
vec3 srgb_encoded(vec3 value) {
    value = clamp(value, 0.0, 1.0);
    return mix(1.055*pow(value, vec3(1.0/2.4))-0.055, 12.92*value, lessThanEqual(value, vec3(0.0031308)));
}
vec3 srgb_decoded(vec3 value) {
    value = clamp(value, 0.0, 1.0);
    return mix(pow((value+0.055)/1.055, vec3(2.4)), value/12.92, lessThanEqual(value, vec3(0.04045)));
}
// Step 1 (geometry.py `lut_applied`): the grid's points sit at texel centres.
vec3 lut_applied(vec3 value) {
    vec3 encoded = srgb_encoded(value);
    vec3 sampled = texture(lut, encoded*((LUT_GRID-1.0)/LUT_GRID)+0.5/LUT_GRID).rgb;
    return srgb_decoded(mix(encoded, sampled, lut_amount));
}
// Step 4 (geometry.py `rolled_off`), through the shortfall 1 - g, so the peak is exactly white.
vec3 rolled_off(vec3 value) {
    float x = max(value.r, max(value.g, value.b));
    if (x <= highlight_knee) return value;
    float t = (min(x, peak)-highlight_knee)/(1.0-highlight_knee);
    float w = (peak-highlight_knee)/(1.0-highlight_knee);
    float shortfall = (w-t)*(w+t)/(w*w*(1.0+t));
    float y = 1.0-(1.0-highlight_knee)*shortfall;
    float fade = pow(max(1.0-shortfall, 0.0), ROLLOFF_FADE_POWER);
    return value*(y/x)*(1.0-fade)+y*fade;
}
// Step 7 (geometry.py `mat_shade`): the shade at an output point, measured in mat space.
vec3 mat_shade(vec2 point) {
    vec3 q = to_opening*vec3(point, 1.0);
    vec2 m = q.xy/q.z*mat_size;
    vec3 lit = vec3(1.0);
    vec3 total = vec3(1.0);
    for (int j = 0; j < MAT_LIGHTS_MAX; j++) {
        if (j >= light_count) break;
        vec2 toward = light_toward[j];
        float reach = 3.0e38;
        if (toward.x > 0.0) reach = min(reach, (mat_size.x-m.x)/toward.x);
        else if (toward.x < 0.0) reach = min(reach, m.x/-toward.x);
        if (toward.y > 0.0) reach = min(reach, (mat_size.y-m.y)/toward.y);
        else if (toward.y < 0.0) reach = min(reach, m.y/-toward.y);
        float s = max(reach, 0.0);
        vec2 band = light_band[j];          // (dark until, lit from), mm
        float reached;
        if (s >= band.y) reached = 1.0;
        else if (s < band.x) reached = 0.0;
        else {
            float k = (s-band.x)/(band.y-band.x);
            reached = k*k*(3.0-2.0*k);
        }
        lit += light_weight[j]*reached;
        total += light_weight[j];
    }
    return lit/total;
}
// Step 0 (geometry.py `pattern_signal`): the pattern's encoded signal at the opening's unit square.
vec3 pattern_signal(vec2 point) {
    vec3 q = to_opening*vec3(point, 1.0);
    vec2 p = clamp(q.xy/q.z, 0.0, 1.0);
    float value = 0.0;
    if (test_pattern == PATTERN_WHITE) value = 1.0;
    else if (test_pattern == PATTERN_GREY_RAMP) {
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
// The calibration targets v2 (geometry.py `board_point`): the screen board's point (column, row)
// in squares at the opening's unit square, its columns centred along the long side, turned a
// quarter clockwise in a portrait opening.
vec2 board_point(vec2 p) {
    float n = target_aspect*SCREEN_ROWS;
    float offset = (n-float(target_columns))*0.5;
    if (target_portrait == 0) return vec2(p.x*n-offset, p.y*SCREEN_ROWS);
    return vec2(p.y*n-offset, (1.0-p.x)*SCREEN_ROWS);
}
// geometry.py `board_signal`: the encoded signal at a board point. Black off the board; in a QR
// copy's squares its module (dark 0; light and the quiet zone 1; no code: all light), the modules
// and their quiet zone filling QR_SQUARES; else the raster's pixel (row 0 at the board's top).
float board_signal(vec2 point) {
    if (any(lessThan(point, vec2(0.0))) || point.x >= float(target_columns) || point.y >= SCREEN_ROWS)
        return 0.0;
    for (int k = 0; k < QR_PERIODS; k++) {
        for (int c = 0; c < QR_COPY_COUNT; c++) {
            ivec2 place = QR_COPY_PLACES[c];
            int column = k*QR_PERIOD+place.y;
            if (column+QR_SQUARES > target_columns) continue;
            vec2 square = point-vec2(float(column), float(place.x));
            if (any(lessThan(square, vec2(0.0))) || any(greaterThanEqual(square, vec2(float(QR_SQUARES)))))
                continue;
            if (target_qr_modules == 0) return 1.0;
            float side = float(target_qr_modules)+2.0*QR_QUIET_MODULES;
            ivec2 cell = ivec2(min(floor(square/float(QR_SQUARES)*side), vec2(side-1.0)))-ivec2(int(QR_QUIET_MODULES));
            if (all(greaterThanEqual(cell, ivec2(0))) && all(lessThan(cell, ivec2(target_qr_modules))))
                return texelFetch(target_qr, cell, 0).r > 0.5 ? 0.0 : 1.0;
            return 1.0;
        }
    }
    return texelFetch(target_layout, ivec2(floor(point*SQUARE_PX)), 0).r;
}
void main() {
    // The Black level is the raw signal: no step, no mat; the per-Output mask stays last.
    if (test_pattern == PATTERN_BLACK_LEVEL) {
        color = vec4(inside_opening(uv) ? pattern_signal(uv) : vec3(0.0), 1.0);
        return;
    }
    // A calibration target: pre-software is the board's signal raw (as the Black level), black
    // outside; post-software runs every step below but the mat shadow (a place effect, not a
    // setting the target judges).
    bool drawing_target = test_pattern == PATTERN_TARGET_PRE_SOFTWARE || test_pattern == PATTERN_TARGET_POST_SOFTWARE;
    vec3 value;
    if (drawing_target) {
        vec3 q = to_opening*vec3(uv, 1.0);
        float signal = board_signal(board_point(q.xy/q.z));
        if (test_pattern == PATTERN_TARGET_PRE_SOFTWARE) {
            color = vec4(inside_opening(uv) ? vec3(signal) : vec3(0.0), 1.0);
            return;
        }
        value = srgb_decoded(vec3(signal));
    } else {
        // Composition targets use OpenGL's lower-left orientation, unlike decoded bytes.
        value = test_pattern == 0 ? texture(picture, vec2(uv.x, 1.0-uv.y)).rgb
                                  : srgb_decoded(pattern_signal(uv));
    }
    if (lut_on == 1) value = lut_applied(value);
    if (saturation_on == 1) {
        float luminance = dot(LUMA, value);
        value = luminance+saturation*(value-luminance);
    }
    // Steps 3-4: the rolloff on brightness alone, white balance after (its white is the Display's).
    if (rolloff_on == 1) value = rolled_off(max(value*light_factor, 0.0))*rgb_multipliers;
    else value = max(value*rgb_multipliers*light_factor, 0.0);
    vec3 contrasted = (srgb_encoded(value)-0.5)*contrast_slope+0.5;
    vec3 adjusted = clamp(pow(max(contrasted, 0.0), vec3(gamma_exponent)), 0.0, 1.0);
    // Steps 6-7 in the panel's light (sRGB, the panel the encode is for).
    bool shaded = mat_on == 1 && !drawing_target;
    if (floor_on == 1 || shaded) {
        vec3 light = srgb_decoded(adjusted);
        if (floor_on == 1) light = black_floor+(1.0-black_floor)*light;
        if (shaded) light *= mat_shade(uv);
        adjusted = srgb_encoded(light);
    }
    if (!inside_opening(uv)) adjusted = vec3(0.0);
    color=vec4(adjusted, 1.0);
}
"""
# No Frame drawn yet: an opening off the output, so the per-Output mask leaves it all black.
_NO_OPENING = ((-2.0, -2.0), (-1.0, -2.0), (-1.0, -1.0), (-2.0, -1.0))
_NEUTRAL = ((1.0, 1.0, 1.0), 1.0, 1.0, 1.0)     # multipliers, light, contrast, gamma: no change


def _set_opening(gl, program: int, corners) -> None:
    gl.glUniform2fv(gl.glGetUniformLocation(program, "opening"), 4,
                    [value for point in corners for value in point])


class NativeRenderer:
    """Persistent GLArea surfaces; four software slots are deliberately unqualified."""

    def __init__(self, outputs: tuple[NativeOutput, ...], *, decoder_limit: int = 4,
                 texture_budget: int = 512*1024**2, prepare_timeout: float = 5,
                 serial: str | None = None, luts: LutSource):
        if (not outputs or len({o.output_id for o in outputs}) != len(outputs)
                or len({o.app_id for o in outputs}) != len(outputs)):
            raise ValueError("unique Output and app IDs required")
        if decoder_limit < 1 or texture_budget < 1 or not 0 < prepare_timeout <= 30:
            raise ValueError("positive native resource limits required")
        self._owner = threading.get_ident()
        self._closed = False
        self.gl_renderer: str | None = None  # GL_RENDERER of the first realized GL context
        self._serial_label = serial
        self._luts = luts       # where a picture's LUT comes from (print look; the service's PlayerLuts)
        self.decoder_limit, self.texture_budget = decoder_limit, texture_budget
        self.prepare_timeout = prepare_timeout
        self._decoders: dict[str, _Decoder] = {}
        self._surfaces: dict[str, _Surface] = {}
        self._player_id: str | None = None      # the registered id a calibration target's QR names
        self._serial = 0
        self._load_native()
        if not self.Gtk.init_check()[0]:
            raise RuntimeError("GTK cannot open the configured display")
        self.Gst.init(None)
        self._frames = None
        self._frame_timer = None
        if os.environ.get("PHOTO_WALL_DISPLAY_HOST") == "1":
            from player.wayland_frames import WaylandFrames
            display = self.Gdk.Display.get_default()
            if "Wayland" not in type(display).__name__:
                raise RuntimeError("node_display_requires_wayland")
            self._frames = WaylandFrames(display)
            self._frame_timer = Tick(self.GLib, int(REDRAW_RENEWAL*1000),
                                     self._renew_display_frames)
        diagnostic_style = self.Gtk.CssProvider()
        diagnostic_style.load_from_data(b"""
            .photo-wall-diagnostic {
                background-color: #0c141c;
            }
            .photo-wall-diagnostic label {
                color: #ffffff;
                font-size: 22px;
            }
        """)
        identify_style = self.Gtk.CssProvider()
        identify_style.load_from_data(b"""
            .photo-wall-identify { background-color: #ffea00; padding: 12px; }
            .photo-wall-identify label { color: #000000; font-size: 30px; font-weight: bold; }
        """)
        for output in outputs:
            window = self.Gtk.Window(type=self.Gtk.WindowType.TOPLEVEL)
            window.set_title(output.app_id)
            window.set_decorated(False)
            window.set_default_size(output.width, output.height)
            area = self.Gtk.GLArea()
            area.set_use_es(True)
            area.set_required_version(3, 0)
            area.set_auto_render(False)
            area.set_has_depth_buffer(False)
            area.set_has_stencil_buffer(False)
            overlay = self.Gtk.Overlay()
            overlay.add(area)
            diagnostic = self.Gtk.EventBox()
            diagnostic.set_visible_window(True)
            diagnostic.set_halign(self.Gtk.Align.FILL)
            diagnostic.set_valign(self.Gtk.Align.FILL)
            diagnostic.set_hexpand(True)
            diagnostic.set_vexpand(True)
            label = self.Gtk.Label(label=diagnostic_text(output.output_id, serial=serial))
            label.set_justify(self.Gtk.Justification.CENTER)
            diagnostic.add(label)
            diagnostic.get_style_context().add_class("photo-wall-diagnostic")
            diagnostic.get_style_context().add_provider(
                diagnostic_style, self.Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            label.get_style_context().add_provider(
                diagnostic_style, self.Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            overlay.add_overlay(diagnostic)
            identify = self.Gtk.EventBox()
            identify.set_visible_window(True)
            identify.set_halign(self.Gtk.Align.FILL)
            identify.set_valign(self.Gtk.Align.START)
            identify_label = self.Gtk.Label(label="IDENTIFY THIS OUTPUT")
            identify.add(identify_label)
            identify.get_style_context().add_class("photo-wall-identify")
            identify.get_style_context().add_provider(
                identify_style, self.Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            identify_label.get_style_context().add_provider(
                identify_style, self.Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            overlay.add_overlay(identify)
            identify.hide()
            window.add(overlay)
            surface = _Surface(output, window, area, diagnostic, label, identify)
            surface.follower = FrameFollower(
                self.GLib, area, lambda at, surface=surface: self._frame(surface, at))
            self._surfaces[output.output_id] = surface
            area.connect("render", self._render, surface)
            area.connect("unrealize", self._unrealize, surface)
            window.connect("realize", self._window_realized, surface)
            # GtkWindow's default map handler resets the backend app ID. Restore
            # it after that handler, before GLib paints the first content buffer.
            window.connect_after("map", self._window_mapped, surface)
            window.connect("delete-event", lambda *_: True)
            window.show_all()
            if surface.failed():
                self.close()
                raise RuntimeError("cannot establish per-Output Wayland placement")
            window.fullscreen()
            area.queue_render()

    def set_unbound_outputs(self, output_ids: tuple[str, ...], player_id: str | None,
                            central_link_state: Literal["connecting", "reachable", "retrying"] = "reachable",
                            configuration_received: bool = True) -> None:
        """Show enrollment diagnostics only on connected Outputs without a binding."""
        self._thread()
        unbound = set(output_ids)
        for output_id, surface in self._surfaces.items():
            if output_id in unbound:
                surface.diagnostic_label.set_text(diagnostic_text(
                    output_id, player_id, self._serial_label, central_link_state,
                    configuration_received))
                surface.diagnostic.show()
            else:
                surface.diagnostic.hide()

    def set_identify_output(self, output_id: str | None) -> None:
        """Show or hide the conspicuous banner on one Output."""
        self._thread()
        for key, surface in self._surfaces.items():
            if key == output_id:
                surface.identify_banner.get_child().set_text("IDENTIFY THIS OUTPUT  •  " + key)
                surface.identify_banner.show_all()
            else:
                surface.identify_banner.hide()

    def set_player(self, player_id: str | None) -> None:
        """The Player's registered id (None: none yet), which a calibration target's QR names; a
        change redraws every Output, so a drawn target's QR follows it."""
        self._thread()
        if player_id == self._player_id:
            return
        self._player_id = player_id
        for surface in self._surfaces.values():
            surface.area.queue_render()

    def _load_native(self) -> None:
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        gi.require_version("Gst", "1.0")
        gi.require_version("GstVideo", "1.0")
        from gi.repository import Gdk, GLib, Gst, GstVideo, Gtk
        from OpenGL import GL
        self.Gtk, self.Gdk, self.GLib, self.Gst, self.GstVideo = Gtk, Gdk, GLib, Gst, GstVideo
        self.GL = GL

    def _thread(self) -> None:
        if threading.get_ident() != self._owner:
            raise RuntimeError("NativeRenderer requires its owning GLib thread")
        if self._closed:
            raise RuntimeError("NativeRenderer is closed")

    def _renew_display_frames(self) -> bool:
        if self._closed:
            return False
        # Static photos need real tagged buffer commits too. GTK still owns the
        # paint/commit; no timer or frame callback manufactures display evidence.
        # A surface present() renewed within REDRAW_RENEWAL needs no second paint.
        now = time.monotonic()
        for surface in self._surfaces.values():
            if _renewal_due(surface.acknowledged, now):
                surface.area.queue_render()
        return True

    def _frame(self, surface: _Surface, at: float) -> float | None:
        """One frame of the surface's frame clock at frame time `at`: queue a render when what the
        drawn composition shows moved since it was drawn (`_moved`); answer the seconds until it
        may next move (0: watch the next frame), None once nothing can (FrameFollower). A pending
        draw has its own render queued."""
        drawn = surface.acknowledged
        if self._closed or drawn is None or drawn.at is None or not self._follows(drawn):
            return None
        if self._moved(surface, drawn, at):
            surface.area.queue_render()
            return 0.0
        if _trial_drawn(drawn):
            return 0.0
        return max(0.0, until_change(drawn.composition, drawn.at) - (at - drawn.at))

    def _follows(self, draw: _Draw) -> bool:
        """Whether what `draw` shows may change without a new composition, so its surface follows
        the frame clock: a fade or a playing video lies ahead of it (player/look.py), or it shows
        a calibration trial, whose edits come from Display Host at any frame."""
        return _trial_drawn(draw) or until_change(draw.composition, draw.at) is not None

    def _moved(self, surface: _Surface, drawn: _Draw, at: float) -> bool:
        """Whether `drawn`, drawn again at frame time `at` with its decoders as they are, would
        draw other pixels: its look changed (a fade), or anything present() compares (`_shown`:
        a video's newer sample, a trial edit, the Output's picture operations) did. False while
        a decoder it used was sought or replaced, or its grant is not admitted: present() owes a
        new draw then."""
        if not self._current(drawn.generations):
            return False
        if changed(drawn.composition, drawn.at, at):
            return True
        frames = getattr(self, "_frames", None)
        if frames is not None:
            grant = frames.grant(surface.output.output_id, drawn.composition)
            if grant is None or not grant.admitted:
                return False
        for key, _, _ in drawn.generations:
            if self._decoders[key].local.layer.variant.duration:
                self._read_sample(self._decoders[key])
        return self._shown_now(drawn.composition, drawn.generations) != drawn.shown

    def _shown_now(self, composition: OutputComposition, generations: tuple) -> tuple:
        """`_shown` of `composition` drawn with `generations` now: its decoders' newest samples,
        the trial and the picture operations Display Host holds at this moment."""
        frames = getattr(self, "_frames", None)
        grant = (frames.grant(composition.binding.output_id, composition)
                 if frames is not None else None)
        return _shown(composition, generations,
                      tuple(self._decoders[key].sample_serial for key, _, _ in generations),
                      frames, grant, self._luts)

    def _frame_time(self, area) -> float:
        """The time of the frame being drawn on this machine's monotonic clock: the area's frame
        clock's, else now."""
        clock = area.get_frame_clock()
        return time.monotonic() if clock is None else frame_time(self.GLib, clock)

    def _current(self, generations: tuple) -> bool:
        """The decoders a draw used still hold those decodes (none was sought or replaced)."""
        return all(key in self._decoders and (self._decoders[key].incarnation,
                                              self._decoders[key].generation) == (incarnation, generation)
                   for key, incarnation, generation in generations)

    def _ready(self, composition: OutputComposition) -> bool:
        """Every layer seen in `composition` has a decoded picture to draw."""
        return all(local.layer.variant is None or (
            (decoder := self._decoders.get(local.layer.assignment_id)) is not None
            and decoder.prepared and not decoder.failure)
            for local in visible_now(composition.layers))

    def _window_mapped(self, window, surface) -> None:
        """Every map begins a new episode: a remap (an output blip) gets a fresh surface."""
        surface.begin_episode()
        self._window_realized(window, surface)

    def _window_realized(self, window, surface) -> None:
        gdk_window = window.get_window()
        if "Wayland" in type(gdk_window).__name__:
            # Noble ships the public GTK C API but no GdkWayland typelib.
            # Use the owned GObject capsule, never hash(obj) as a guessed pointer.
            try:
                import ctypes
                library = ctypes.CDLL("libgdk-3.so.0")
                capsule_pointer = ctypes.pythonapi.PyCapsule_GetPointer
                capsule_pointer.argtypes = (ctypes.py_object, ctypes.c_char_p)
                capsule_pointer.restype = ctypes.c_void_p
                pointer = capsule_pointer(gdk_window.__gpointer__, None)
                if not pointer:
                    raise ValueError("missing native window pointer")
                setter = library.gdk_wayland_window_set_application_id
                setter.argtypes = (ctypes.c_void_p, ctypes.c_char_p)
                setter.restype = None
                setter(pointer, surface.output.app_id.encode("utf-8"))
            except (AttributeError, TypeError, ValueError, ImportError, OSError):
                surface.fail("wayland_app_id")
                return
        cursor = self.Gdk.Cursor.new_for_display(window.get_display(), self.Gdk.CursorType.BLANK_CURSOR)
        gdk_window.set_cursor(cursor)

    def _element(self, factory: str):
        element = self.Gst.ElementFactory.make(factory)
        if element is None:
            raise RuntimeError(f"missing GStreamer element {factory}")
        return element

    def _new_decoder(self, local: LocalLayer) -> _Decoder:
        assert local.layer.variant is not None and local.path is not None
        Gst = self.Gst
        pipeline = Gst.Pipeline.new(None)
        source = self._element("filesrc")
        # A native pipeline never receives a URL or shell expression.
        source.set_property("location", str(local.path))
        convert, caps, sink = (self._element(name) for name in
                               ("videoconvert", "capsfilter", "appsink"))
        caps.set_property("caps", Gst.Caps.from_string("video/x-raw,format=RGBA"))
        sink.set_property("emit-signals", True)
        sink.set_property("max-buffers", 2)
        sink.set_property("drop", True)
        sink.set_property("sync", True)
        sink.set_property("enable-last-sample", False)
        video = local.layer.variant.media_type == "video/mp4"
        if video:
            demux, parser, decode = (self._element(n) for n in ("qtdemux", "h264parse", "avdec_h264"))
            parser.set_name("parse")    # pad_added finds it by name, never through a closure
            elements = (source, demux, parser, decode, convert, caps, sink)
        else:
            decode = self._element("jpegdec" if local.layer.variant.media_type == "image/jpeg"
                                   else "pngdec")
            elements = (source, decode, convert, caps, sink)
        for element in elements:
            pipeline.add(element)
        if video:
            if not source.link(demux) or not parser.link(decode):
                raise RuntimeError("cannot link local H.264 decoder")
        elif not source.link(decode):
            raise RuntimeError("cannot link local image decoder")
        for a, b in ((decode, convert), (convert, caps), (caps, sink)):
            if not a.link(b):
                raise RuntimeError("cannot link RGBA decoder")
        self._serial += 1
        decoder = _Decoder(local, pipeline, sink, incarnation=self._serial,
                           deadline=time.monotonic()+self.prepare_timeout)
        # Every callback holds the decoder weakly: a strong one made the cycle pipeline -> sink
        # -> callback -> decoder -> pipeline, which leaked each pipeline and its bus's two fds.
        owner = weakref.ref(decoder)

        def pad_added(_demux, pad):
            current = owner()
            if current is None:
                return
            negotiated = pad.get_current_caps()
            if negotiated and negotiated.get_structure(0).get_name() == "video/x-h264":
                target = current.pipeline.get_by_name("parse").get_static_pad("sink")
                if not target.is_linked():
                    pad.link(target)

        def sample_ready(appsink, preroll):
            current = owner()
            if current is None:
                return Gst.FlowReturn.OK
            sample = appsink.emit("pull-preroll" if preroll else "pull-sample")
            if sample is not None:
                current.mailbox.publish(sample)
            return Gst.FlowReturn.OK

        def event_probe(_pad, info):
            current = owner()
            if current is None:
                return Gst.PadProbeReturn.OK
            event = info.get_event()
            if event is not None and event.type == Gst.EventType.SEGMENT:
                if event.get_seqnum() == current.seek_sequence:
                    current.mailbox.segment(current.generation)
            return Gst.PadProbeReturn.OK

        def bus_message(_bus, message):
            current = owner()
            if current is not None:
                _bus_message(Gst, current, message)

        if video:
            decoder.handlers.append((demux, demux.connect("pad-added", pad_added)))
        decoder.handlers.append((sink, sink.connect("new-preroll", sample_ready, True)))
        decoder.handlers.append((sink, sink.connect("new-sample", sample_ready, False)))
        pad = sink.get_static_pad("sink")
        decoder.probes.append((pad, pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, event_probe)))
        decoder.bus = pipeline.get_bus()
        watch_bus(decoder.bus)
        decoder.handlers.append((decoder.bus, decoder.bus.connect("message", bus_message)))
        if pipeline.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
            self._destroy_decoder(decoder)
            raise RuntimeError("decoder refused PAUSED")
        return decoder

    def _destroy_decoder(self, decoder: _Decoder) -> None:
        """The one teardown: undoes every handler, probe and bus watch the decoder made and stops
        its pipeline. A second call is a no-op."""
        decoder.mailbox.close()
        for instance, handler in decoder.handlers:
            instance.disconnect(handler)
        for pad, probe in decoder.probes:
            pad.remove_probe(probe)
        if decoder.bus is not None:
            decoder.bus.remove_signal_watch()
        decoder.pipeline.set_state(self.Gst.State.NULL)
        decoder.sample = decoder.bus = None
        decoder.handlers, decoder.probes = [], []

    def _sample_position(self, sample) -> float | None:
        pts = sample.get_buffer().pts
        if pts == self.Gst.CLOCK_TIME_NONE:
            return None
        stream = sample.get_segment().to_stream_time(self.Gst.Format.TIME, pts)
        return None if stream == self.Gst.CLOCK_TIME_NONE else stream/self.Gst.SECOND

    def _read_sample(self, decoder: _Decoder) -> None:
        sample = decoder.mailbox.latest(decoder.generation)
        if sample is None:
            return
        variant = decoder.local.layer.variant
        info = self.GstVideo.VideoInfo.new_from_caps(sample.get_caps())
        if info is None or (info.width, info.height) != (variant.width, variant.height):
            decoder.failure = "decode"
            decoder.prepared = False
            return
        buffer = sample.get_buffer()
        meta = self.GstVideo.buffer_get_video_meta(buffer)
        stride, offset = (meta.stride[0], meta.offset[0]) if meta else (info.stride[0], info.offset[0])
        try:
            _rgba_layout(info.width, info.height, stride, offset, buffer.get_size())
        except ValueError:
            decoder.failure, decoder.prepared = "decode", False
            decoder.retry_at = time.monotonic()+1
            return
        position = self._sample_position(sample) if variant.duration else 0
        if variant.duration and (position is None or position < decoder.target-.150):
            return
        decoder.sample, decoder.prepared = sample, True
        decoder.sample_serial += 1

    def _seek(self, decoder: _Decoder, position: float) -> bool:
        Gst = self.Gst
        for surface in self._surfaces.values():
            if surface.pending and any(local.layer.assignment_id == decoder.local.layer.assignment_id
                                       for local in surface.pending.composition.layers):
                surface.pending = None
        decoder.pipeline.set_state(Gst.State.PAUSED)
        decoder.playing = False
        decoder.prepared = False
        decoder.generation += 1
        decoder.target = position
        decoder.deadline = time.monotonic()+self.prepare_timeout
        decoder.eos = False
        # Invalidate old samples immediately; the segment probe admits the new generation.
        decoder.mailbox.segment(-1)
        decoder.seek_sequence = Gst.util_seqnum_next()
        event = Gst.Event.new_seek(1, Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                                  Gst.SeekType.SET, int(position*Gst.SECOND),
                                  Gst.SeekType.NONE, -1)
        event.set_seqnum(decoder.seek_sequence)
        if not decoder.pipeline.send_event(event):
            decoder.failure = "decode"
            return False
        return True

    def _estimate(self, compositions: tuple[OutputComposition, ...]) -> tuple[int, int]:
        variants = {key: d.local.layer.variant for key, d in self._decoders.items()}
        for composition in compositions:
            for local in composition.layers:
                if local.layer.variant is not None:
                    variants[local.layer.assignment_id] = local.layer.variant
        # 2 linear RGBA16F targets per Output + one RGBA8 texture per assignment.
        target_bytes = sum(max(s.output.width*s.output.height, s.size[0]*s.size[1])*16
                           for s in self._surfaces.values())
        return len(variants), target_bytes + sum(v.width*v.height*4 for v in variants.values())

    def capacity(self, compositions: tuple[OutputComposition, ...]) -> CapacityResult:
        self._thread()
        count, size = self._estimate(compositions)
        surfaces_ok = all(surface.programs is not None and not surface.failed()
                          for surface in self._surfaces.values())
        return CapacityResult(surfaces_ok and count <= self.decoder_limit
                              and size <= self.texture_budget, qualified=False)

    def prepare(self, local: LocalLayer) -> PrepareResult:
        self._thread()
        if (local.layer.output_id not in self._surfaces or not math.isfinite(local.position)
                or local.position < 0 or not math.isfinite(local.alpha) or not 0 <= local.alpha <= 1):
            return PrepareResult("failed", "decode")
        surface = self._surfaces[local.layer.output_id]
        if surface.failed():
            return PrepareResult("failed", "decode")
        if local.layer.presentation == "black":
            return PrepareResult("prepared" if surface.programs else "pending")
        key = local.layer.assignment_id
        decoder = self._decoders.get(key)
        if decoder and (decoder.local.layer.variant != local.layer.variant
                        or decoder.local.path != local.path
                        or decoder.local.layer.output_id != local.layer.output_id):
            return PrepareResult("failed", "decode")
        if decoder and decoder.failure and time.monotonic() >= decoder.retry_at:
            self._destroy_decoder(decoder)
            del self._decoders[key]
            decoder = None
        if decoder is None:
            variant = local.layer.variant
            count, size = self._estimate(())
            if count >= self.decoder_limit or size+variant.width*variant.height*4 > self.texture_budget:
                return PrepareResult("failed", "capacity")
            # Cache worker validates file identity. Only absolute local paths enter filesrc.
            if local.path is None or not isinstance(local.path, Path) or not local.path.is_absolute():
                return PrepareResult("failed", "decode")
            try:
                decoder = self._new_decoder(local)
                self._decoders[key] = decoder
            except (RuntimeError, TypeError, ValueError):
                return PrepareResult("failed", "decode")
        self._read_sample(decoder)
        if not decoder.prepared and time.monotonic() >= decoder.deadline:
            decoder.failure = "decode"
            decoder.retry_at = time.monotonic()+1
        if decoder.failure:
            return PrepareResult("failed", "decode")
        if local.layer.variant.duration and decoder.prepared:
            if decoder.playing:
                success, position = decoder.pipeline.query_position(self.Gst.Format.TIME)
                drift = abs(position/self.Gst.SECOND-local.position) if success else math.inf
            else:
                position = self._sample_position(decoder.sample)
                drift = abs(position-local.position) if position is not None else math.inf
            if decoder.eos or drift > .150:
                if not self._seek(decoder, local.position):
                    return PrepareResult("failed", "decode")
        return PrepareResult("prepared" if decoder.prepared and surface.programs else "pending")

    def _generations(self, composition: OutputComposition) -> tuple:
        return tuple((local.layer.assignment_id,
                      self._decoders[local.layer.assignment_id].incarnation,
                      self._decoders[local.layer.assignment_id].generation)
                     for local in visible_now(composition.layers) if local.layer.variant is not None
                     and local.layer.assignment_id in self._decoders)

    def present(self, composition: OutputComposition) -> PresentationResult:
        self._thread()
        surface = self._surfaces.get(composition.binding.output_id)
        if surface is None:
            return PresentationResult("failed", "decode")
        frames = getattr(self, "_frames", None)
        grant = frames.grant(composition.binding.output_id, composition) if frames is not None else None
        _observe_grant(surface, grant)
        if surface.failed():
            return PresentationResult("failed", "decode")
        if frames is not None:
            if grant is None or not grant.admitted or not grant.matches(composition):
                # Nothing drawn before admission stands: a cleared acknowledgment makes the
                # first present() after admission draw. A pending draw needs no reset here:
                # _render drops it while its grant is not admitted.
                surface.acknowledged = None
                surface.area.queue_render()
                return PresentationResult("pending")
        if surface.pending and _fingerprint(surface.pending.composition) != _fingerprint(composition):
            surface.pending = None
        if not self.capacity((composition,)).available:
            return PresentationResult("failed", "capacity")
        visible = visible_now(composition.layers)
        for local in visible:
            result = self.prepare(local)
            if result.status != "prepared":
                return PresentationResult(result.status, result.code)
        visible_ids = {local.layer.assignment_id for local in visible}
        for key, decoder in self._decoders.items():
            if decoder.local.layer.output_id != composition.binding.output_id:
                continue
            playing = key in visible_ids and decoder.local.layer.variant.duration is not None
            if playing != decoder.playing:
                if decoder.pipeline.set_state(self.Gst.State.PLAYING if playing else self.Gst.State.PAUSED
                                              ) == self.Gst.StateChangeReturn.FAILURE:
                    decoder.failure = "decode"
                    return PresentationResult("failed", "decode")
                decoder.playing = playing
        generations = self._generations(composition)
        # Render on change: a new draw only when what it shows changed (layers, decoder
        # generations, a new video sample, a trial edit) or the last one is due for renewal. A
        # static photo is not redrawn every tick (G8); a changing look is drawn on the frame clock.
        shown = self._shown_now(composition, generations)
        # The newest draw is the pending one, else the acknowledged one: whatever _render
        # dropped or drew, present() reads it from those two and nothing else.
        ack = surface.acknowledged
        newest = surface.pending or ack
        now = time.monotonic()
        if (newest is None or newest.shown != shown or retimed(newest.composition, composition)
                or _renewal_due(ack, now)):
            self._serial += 1
            surface.pending = _Draw(self._serial, composition, generations, shown=shown)
            surface.area.queue_render()
        # Presented stays keyed on layers and generations only: a playing video's newer sample
        # or a frame of a crossfade never makes the composition already drawn read as pending.
        if (ack and _fingerprint(ack.composition) == _fingerprint(composition)
                and ack.generations == generations
                and now-ack.completed_at < PRESENTATION_FRESHNESS):
            return PresentationResult("presented", composition=ack.composition,
                                      presented_at=ack.completed_at,
                                      applied_calibration=ack.applied_calibration if getattr(self, "_frames", None) else None)
        return PresentationResult("pending")

    def release(self, assignment_id: str) -> None:
        self._thread()
        decoder = self._decoders.pop(assignment_id, None)
        if decoder:
            self._destroy_decoder(decoder)
        for surface in self._surfaces.values():
            if surface.pending and any(local.layer.assignment_id == assignment_id
                                       for local in surface.pending.composition.layers):
                surface.pending = None
            if surface.acknowledged and any(local.layer.assignment_id == assignment_id
                                            for local in surface.acknowledged.composition.layers):
                surface.acknowledged = None
            surface.area.make_current()
            if surface.area.get_error() is None:
                texture = surface.textures.pop(assignment_id, None)
                if texture:
                    self.GL.glDeleteTextures([texture[0]])

    def diagnostics(self) -> dict:
        self._thread()
        return {"qualified": False, "resident_decoders": len(self._decoders),
                "estimated_texture_bytes": self._estimate(())[1],
                "outputs": {key: {"size": surface.size, "failure": surface.failed(),
                                  "draw_serial": surface.acknowledged.serial if surface.acknowledged else None,
                                  "draw_time": surface.acknowledged.completed_at if surface.acknowledged else None,
                                  "positions": {local.layer.assignment_id: local.position for local in
                                                surface.acknowledged.composition.layers}
                                  if surface.acknowledged else {}}
                            for key, surface in self._surfaces.items()}}

    def _program(self, fragment: str) -> int:
        gl = self.GL
        shaders = []
        program = 0
        try:
            for source, kind in ((_VERTEX, gl.GL_VERTEX_SHADER), (fragment, gl.GL_FRAGMENT_SHADER)):
                shader = int(gl.glCreateShader(kind))
                shaders.append(shader)
                gl.glShaderSource(shader, source)
                gl.glCompileShader(shader)
                if not gl.glGetShaderiv(shader, gl.GL_COMPILE_STATUS):
                    raise RuntimeError("native shader compilation failed")
            program = int(gl.glCreateProgram())
            for shader in shaders:
                gl.glAttachShader(program, shader)
            gl.glLinkProgram(program)
            if not gl.glGetProgramiv(program, gl.GL_LINK_STATUS):
                raise RuntimeError("native shader link failed")
            return program
        except Exception:
            if program:
                gl.glDeleteProgram(program)
            raise
        finally:
            for shader in shaders:
                gl.glDeleteShader(shader)

    def _texture(self, width: int, height: int, *, floating: bool = False, data=None) -> int:
        gl = self.GL
        texture = int(gl.glGenTextures(1))
        try:
            gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
            gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA16F if floating else gl.GL_RGBA8,
                            width, height, 0, gl.GL_RGBA, gl.GL_HALF_FLOAT if floating else gl.GL_UNSIGNED_BYTE,
                            data)
        except Exception:
            gl.glDeleteTextures([texture])
            raise
        return texture

    def _targets(self, surface: _Surface, width: int, height: int) -> None:
        gl = self.GL
        if surface.programs is None:
            layer_program = self._program(_LAYER)
            try:
                final_program = self._program(_FINAL)
            except Exception:
                gl.glDeleteProgram(layer_program)
                raise
            surface.programs = layer_program, final_program
            surface.vao = int(gl.glGenVertexArrays(1))
        if surface.size == (width, height):
            return
        old_size = surface.size
        surface.size = (width, height)
        old_targets = old_size[0]*old_size[1]*16 if surface.targets else 0
        if self._estimate(())[1]+old_targets > self.texture_budget:
            surface.size = old_size
            raise RuntimeError("resized Output exceeds texture budget")
        targets = []
        try:
            for _ in range(2):
                texture = self._texture(width, height, floating=True)
                try:
                    framebuffer = int(gl.glGenFramebuffers(1))
                except Exception:
                    gl.glDeleteTextures([texture])
                    raise
                targets.append((framebuffer, texture))
                gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, framebuffer)
                gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                          gl.GL_TEXTURE_2D, texture, 0)
                if gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) != gl.GL_FRAMEBUFFER_COMPLETE:
                    raise RuntimeError("linear floating framebuffer unsupported")
                gl.glClearColor(0, 0, 0, 1)
                gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        except Exception:
            for framebuffer, texture in targets:
                gl.glDeleteFramebuffers(1, [framebuffer])
                gl.glDeleteTextures([texture])
            surface.size = old_size
            raise
        for framebuffer, texture in surface.targets:
            gl.glDeleteFramebuffers(1, [framebuffer])
            gl.glDeleteTextures([texture])
        surface.targets = targets
        surface.acknowledged = None

    def _upload(self, surface: _Surface, local: LocalLayer) -> int:
        gl = self.GL
        decoder = self._decoders[local.layer.assignment_id]
        self._read_sample(decoder)
        if decoder.failure or not decoder.prepared or decoder.sample is None:
            raise RuntimeError("decoded sample unavailable")
        previous = surface.textures.get(local.layer.assignment_id)
        if previous and previous[1:] == (decoder.incarnation, decoder.sample_serial):
            return previous[0]
        sample, variant = decoder.sample, local.layer.variant
        info = self.GstVideo.VideoInfo.new_from_caps(sample.get_caps())
        buffer = sample.get_buffer()
        meta = self.GstVideo.buffer_get_video_meta(buffer)
        stride, offset = (meta.stride[0], meta.offset[0]) if meta else (info.stride[0], info.offset[0])
        mapped, mapping = buffer.map(self.Gst.MapFlags.READ)
        if not mapped:
            raise RuntimeError("cannot map decoded sample")
        try:
            data = packed_rgba(mapping.data, variant.width, variant.height, stride, offset)
            if previous:
                texture = previous[0]
                gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
                gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
                gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, variant.width, variant.height,
                                  gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, data)
            else:
                texture = self._texture(variant.width, variant.height, data=data)
        finally:
            buffer.unmap(mapping)
        surface.textures[local.layer.assignment_id] = (texture, decoder.incarnation,
                                                       decoder.sample_serial)
        return texture

    def _draw_candidate(self, surface: _Surface, draw: _Draw) -> None:
        gl = self.GL
        program = surface.programs[0]
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, surface.targets[1][0])
        gl.glClearColor(0, 0, 0, 1)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        gl.glUseProgram(program)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA)
        fit = _drawn_fit(draw)
        profile = draw.composition.binding.profile
        aspect = profile.width_px / profile.height_px      # the Frame's aspect: Cover's and the photo's
        _set_matrix(gl, program, "output_to_picture", output_to_picture(fit, surface.size, aspect))
        _set_opening(gl, program, fit.corners)
        gl.glUniform1i(gl.glGetUniformLocation(program, "media"), 0)
        # An explicit Trial may render a procedural commissioning canvas when
        # no authorized media exists. It is never a Layer, Run or readiness fact.
        commissioning = bool(draw.applied_calibration and draw.applied_calibration.trial_id
                             and draw.composition.fallback and not draw.composition.layers)
        gl.glUniform1i(gl.glGetUniformLocation(program, "commissioning"), int(commissioning))
        if commissioning:
            gl.glUniform4f(gl.glGetUniformLocation(program, "photo"), 0.0, 0.0, 1.0, 1.0)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        for local in visible_now(draw.composition.layers):
            variant = local.layer.variant
            if variant is not None:
                texture = self._upload(surface, local)
                gl.glActiveTexture(gl.GL_TEXTURE0)
                gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
                rectangle = photo_rect(aspect, variant.width, variant.height, local.layer.crop)
                gl.glUniform4f(gl.glGetUniformLocation(program, "photo"), *rectangle)
            gl.glUniform1i(gl.glGetUniformLocation(program, "video"), int(bool(variant and variant.duration)))
            gl.glUniform1i(gl.glGetUniformLocation(program, "black"), int(variant is None))
            gl.glUniform1f(gl.glGetUniformLocation(program, "opacity"), local.alpha)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        gl.glDisable(gl.GL_BLEND)
        if gl.glGetError() != gl.GL_NO_ERROR:
            raise RuntimeError("composition GL error")

    def _render(self, area, _context, surface: _Surface) -> bool:
        gl = self.GL
        area.make_current()
        if area.get_error() is not None:
            surface.fail("decode")
            return True
        if self.gl_renderer is None:  # reported on the probe channel: the judge flags software rendering
            self.gl_renderer = _gl_text(gl.glGetString(gl.GL_RENDERER))
        gtk_framebuffer = int(gl.glGetIntegerv(gl.GL_FRAMEBUFFER_BINDING))
        width = max(1, area.get_allocated_width()*area.get_scale_factor())
        height = max(1, area.get_allocated_height()*area.get_scale_factor())
        frames = getattr(self, "_frames", None)
        grant = frames.grant(surface.output.output_id) if frames is not None else None
        _observe_grant(surface, grant)
        # The Output's picture operations, read each render like the trial; a new value is
        # drawn by this render's final pass and named in its tag.
        surface.operations = frames.picture(surface.output.output_id) if frames is not None else None
        if frames is not None and (grant is None or not grant.admitted):
            # Before surface admission, show only a fixed synthetic handoff
            # colour. Authored layers remain in Executor under their own grant.
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, gtk_framebuffer)
            gl.glViewport(0, 0, width, height)
            gl.glClearColor(0.12, 0.17, 0.24, 1)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT)
            gl.glFlush()
            if grant is not None and gl.glGetError() == gl.GL_NO_ERROR:
                frames.tag_rendered_buffer(surface.window.get_window(), grant,
                                           "candidate-" + str(grant.grant_id))
            surface.acknowledged = None
            return True
        at = self._frame_time(area)
        draw = surface.pending
        if draw and draw.generations != self._generations(draw.composition):
            surface.pending = draw = None
        if draw is not None and frames is not None:
            pending_grant = frames.grant(surface.output.output_id, draw.composition)
            if pending_grant is None or not pending_grant.admitted:
                surface.pending = draw = None
        ack = surface.acknowledged
        if (draw is None and ack is not None and ack.at is not None
                and self._moved(surface, ack, at)):
            # What it shows moved (a fade, a video's new sample, a trial edit): draw it again at
            # this frame's time, no tick needed.
            self._serial += 1
            draw = _Draw(self._serial, ack.composition, ack.generations,
                         shown=self._shown_now(ack.composition, ack.generations))
        if draw is not None:
            drawn = look(draw.composition, at)
            if self._ready(drawn):
                draw.composition = drawn
            elif draw is not surface.pending:
                draw = None     # a layer seen only at this frame time is not decoded: next frame
            # else: the pending draw as present() prepared it, its look at the tick's time
        logical = draw.composition if draw else (surface.acknowledged.composition if surface.acknowledged else None)
        applied = None
        trial = None
        if logical is not None:
            if frames is not None:
                exact_grant = frames.grant(surface.output.output_id, logical)
                if exact_grant is not None:
                    trial = frames.trial(logical, exact_grant)
            applied = AppliedCalibration(calibration=trial.calibration if trial else logical.calibration,
                trial_id=str(trial.candidate.trial_id) if trial else None,
                trial_generation=trial.candidate.generation if trial else None,
                trial_sequence=trial.candidate.sequence if trial else None,
                candidate_sha256=trial.candidate.candidate_sha256 if trial else None)
            if draw is None and surface.acknowledged.applied_calibration != applied:
                # Trial edits and expiry redraw retained authorized content through the same shader.
                # A cached logical composition is never treated as restored baseline pixels.
                self._serial += 1
                generations = self._generations(logical)
                draw = _Draw(self._serial, logical, generations,
                             shown=self._shown_now(logical, generations))
            if draw is not None:
                draw.applied_calibration = applied
                draw.applied_mat = trial.mat if trial else None
                draw.primitives = trial.primitives if trial else None
        candidate_ok = False
        try:
            self._targets(surface, width, height)
            gl.glBindVertexArray(surface.vao)
            gl.glViewport(0, 0, width, height)
            if draw:
                try:
                    self._draw_candidate(surface, draw)
                    candidate_ok = True
                except Exception:
                    for local in visible_now(draw.composition.layers):
                        decoder = self._decoders.get(local.layer.assignment_id)
                        if decoder:
                            decoder.failure, decoder.prepared = "decode", False
                            decoder.retry_at = time.monotonic()+1
                    surface.pending = None
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, gtk_framebuffer)
            gl.glDisable(gl.GL_BLEND)
            gl.glUseProgram(surface.programs[1])
            gl.glActiveTexture(gl.GL_TEXTURE0)
            gl.glBindTexture(gl.GL_TEXTURE_2D, surface.targets[1 if candidate_ok else 0][1])
            gl.glUniform1i(gl.glGetUniformLocation(surface.programs[1], "picture"), 0)
            shown = draw if candidate_ok else surface.acknowledged
            shadow = (mat_shadow(_drawn_fit(shown), shown.composition.binding.profile, _drawn_mat(shown))
                      if shown else None)
            lut_drawn = self._bind_lut(surface, surface.operations)
            target = None
            if surface.operations is not None and surface.operations.test_pattern in TARGET_PATTERNS:
                modules = self._bind_target(surface, surface.operations, lut_drawn=lut_drawn)
                if shown:
                    target = (modules, board_draw(_drawn_fit(shown), shown.composition.binding.profile, None))
            _final_uniforms(gl, surface.programs[1], surface.operations,
                            _drawn_fit(shown).corners if shown else _NO_OPENING, shadow,
                            lut_drawn=lut_drawn, target=target)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
            gl.glFlush()
            if gl.glGetError() != gl.GL_NO_ERROR:
                raise RuntimeError("presentation GL error")
            if draw and candidate_ok:
                actual = []
                for local in draw.composition.layers:
                    decoder = self._decoders.get(local.layer.assignment_id)
                    if decoder and decoder.local.layer.variant.duration and decoder.sample:
                        position = self._sample_position(decoder.sample)
                        if position is not None:
                            local = replace(local, position=position)
                    actual.append(local)
                draw.composition = replace(draw.composition, layers=tuple(actual))
                draw.completed_at, draw.at = time.monotonic(), at
                surface.targets.reverse()
                surface.acknowledged = draw
                if surface.pending is draw:
                    surface.pending = None
                if self._follows(draw):
                    surface.follower.follow()       # what it shows changes: watch each frame
            if frames is not None and surface.acknowledged is not None:
                actual = surface.acknowledged.composition
                grant = frames.grant(surface.output.output_id, actual)
                if grant is None or not grant.matches(actual):
                    raise RuntimeError("display_frame_revision_mismatch")
                from player.wayland_frames import composition_tag
                # GLArea render is inside the same GDK paint cycle. The tag and
                # wp_presentation feedback precede GDK's commit of this draw;
                # no timer, configure handler or later idle callback tags it.
                metadata = surface.acknowledged.applied_calibration
                tag = ("trial-" + metadata.candidate_sha256 if metadata and metadata.trial_id
                       else composition_tag(actual, visible_now(actual.layers)))
                frames.tag_rendered_buffer(surface.window.get_window(), grant, tag,
                                           surface.acknowledged.primitives,
                                           picture_change=surface._tagged_change(surface.operations, lut_drawn))
        except Exception as error:
            surface.pending = None
            surface.fail(type(error).__name__)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, gtk_framebuffer)
            gl.glClearColor(0, 0, 0, 1)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        finally:
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, gtk_framebuffer)
        return True

    def _bind_lut(self, surface: _Surface, operations: PictureOperations | None) -> bool:
        """Bind the LUT `operations` names on _LUT_UNIT, uploading its bytes when the surface holds
        another; False when none is named or its bytes are not held yet (the first ask starts the
        fetch: player/luts.py). Texture unit 0 stays the active unit."""
        luts = self._luts
        if operations is None or operations.lut is None:
            return False
        table = luts.lut(operations.lut.ref)
        if table is None:
            return False
        gl = self.GL
        gl.glActiveTexture(gl.GL_TEXTURE0 + _LUT_UNIT)
        try:
            if not surface.lut_texture:
                surface.lut_texture, surface.lut_ref = int(gl.glGenTextures(1)), None
            gl.glBindTexture(gl.GL_TEXTURE_3D, surface.lut_texture)
            if surface.lut_ref != operations.lut.ref:
                for parameter, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR),
                                         (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR),
                                         (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                                         (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE),
                                         (gl.GL_TEXTURE_WRAP_R, gl.GL_CLAMP_TO_EDGE)):
                    gl.glTexParameteri(gl.GL_TEXTURE_3D, parameter, value)
                gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)      # a row is 33 * 3 bytes
                # contracts/lut.py's layout as served: red fastest (width), then green, then blue.
                gl.glTexImage3D(gl.GL_TEXTURE_3D, 0, gl.GL_RGB8, LUT_GRID, LUT_GRID, LUT_GRID, 0,
                                gl.GL_RGB, gl.GL_UNSIGNED_BYTE, table)
                surface.lut_ref = operations.lut.ref
        finally:
            gl.glActiveTexture(gl.GL_TEXTURE0)
        return True

    def _bind_target(self, surface: _Surface, operations: PictureOperations, *, lut_drawn: bool) -> int:
        """Bind the screen board's raster on _TARGET_LAYOUT_UNIT, uploaded once per GL context as
        R8 (contracts.calibration_target.board_raster(SCREEN), top row first), and the QR of the
        code naming this Player, `operations`' Output and picture change on _TARGET_QR_UNIT, rebuilt
        (`target_modules`) only when that code changes. The QR's module count; 0 (the square
        draws light, so the reader refuses code-unreadable) with no player id yet, or while a
        post-software target's LUT is not drawn (`lut_drawn` False: its bytes not held), so the
        code never names a picture change whose look is not on screen (decision 0027). Texture
        unit 0 stays the active unit."""
        gl = self.GL
        try:
            gl.glActiveTexture(gl.GL_TEXTURE0 + _TARGET_LAYOUT_UNIT)
            if not surface.target_layout:
                raster = board_raster(Surface.SCREEN)
                surface.target_layout = self._target_texture(
                    gl.GL_R8, gl.GL_RED, raster.width, raster.height, raster.pixels)
            else:
                gl.glBindTexture(gl.GL_TEXTURE_2D, surface.target_layout)
            gl.glActiveTexture(gl.GL_TEXTURE0 + _TARGET_QR_UNIT)
            look_missing = (operations.test_pattern is TestPattern.TARGET_POST_SOFTWARE
                            and operations.lut is not None and not lut_drawn)
            code = (None if self._player_id is None or look_missing
                    else (self._player_id, operations.output_id, operations.picture_change))
            if code is None:
                return 0
            if surface.target_qr and surface.target_code == code:
                gl.glBindTexture(gl.GL_TEXTURE_2D, surface.target_qr)
                return surface.target_qr_modules
            modules = target_modules(TargetCode(*code))
            data = bytes(255 if dark else 0 for row in modules for dark in row)
            if surface.target_qr:
                gl.glDeleteTextures([surface.target_qr])
                surface.target_qr = 0
            surface.target_qr = self._target_texture(gl.GL_R8, gl.GL_RED, len(modules), len(modules), data)
            surface.target_code, surface.target_qr_modules = code, len(modules)
            return surface.target_qr_modules
        finally:
            gl.glActiveTexture(gl.GL_TEXTURE0)

    def _target_texture(self, internal: int, layout: int, width: int, height: int, data: bytes) -> int:
        """A new 2D texture of `data` (rows top first) bound on the active unit, sampled exactly
        (texelFetch; nearest, so it is complete without mipmaps)."""
        gl = self.GL
        texture = int(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
        for parameter, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST),
                                 (gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST),
                                 (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                                 (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
            gl.glTexParameteri(gl.GL_TEXTURE_2D, parameter, value)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, internal, width, height, 0, layout, gl.GL_UNSIGNED_BYTE, data)
        return texture

    def _unrealize(self, area, surface) -> None:
        area.make_current()
        if area.get_error() is not None:
            return
        gl = self.GL
        if surface.lut_texture:
            gl.glDeleteTextures([surface.lut_texture])
        surface.lut_texture, surface.lut_ref = 0, None
        for texture in (surface.target_layout, surface.target_qr):
            if texture:
                gl.glDeleteTextures([texture])
        surface.target_layout = surface.target_qr = surface.target_qr_modules = 0
        surface.target_code = None
        for texture, _, _ in surface.textures.values():
            gl.glDeleteTextures([texture])
        for framebuffer, texture in surface.targets:
            gl.glDeleteFramebuffers(1, [framebuffer])
            gl.glDeleteTextures([texture])
        if surface.programs:
            for program in surface.programs:
                gl.glDeleteProgram(program)
        if surface.vao:
            gl.glDeleteVertexArrays(1, [surface.vao])
        surface.textures.clear()
        surface.targets.clear()
        surface.programs, surface.vao = None, 0
        surface.size = (0, 0)

    def close(self) -> None:
        self._thread()
        if getattr(self, "_frame_timer", None) is not None:
            self._frame_timer.stop()
            self._frame_timer = None
        if getattr(self, "_frames", None) is not None:
            self._frames.close()
            self._frames = None
        for decoder in self._decoders.values():
            self._destroy_decoder(decoder)
        self._decoders.clear()
        for surface in self._surfaces.values():
            surface.follower.stop()
            surface.window.destroy()
        self._closed = True
