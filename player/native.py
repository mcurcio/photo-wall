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
from contracts.models import MAT_LIGHTS_MAX, AppliedCalibration, Mat, OutputBinding
from contracts.node_frame import PictureOperations
from contracts.tuning import TARGET_PATTERNS, TestPattern
from player.calibration_target import target_modules
from player.fitted import FITTED_BYTES, FitKey, prepared, redrawn_by_frames, shown_at_once
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
from player.look import look, next_change
from player.luts import LutSource
from player.mainloop import (
    DisplayEvents,
    FrameFollower,
    MainLoopDispatcher,
    frame_time,
    watch_bus,
)
from player.pacing import FramePacer
from player.rendering import (
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
    return (composition.binding, composition.fallback,
            tuple((local.layer, local.path) for local in composition.layers))


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


FOLLOW_LEAD = 0.1
"""Seconds ahead of a change over time a surface follows its frame clock: three planning ticks,
each of which wakes it (`NativeRenderer._wake`), so the change's first frame is the frame clock's
though a tick runs late. A surface whose next change is further off installs no frame callback."""


def _expires(trial) -> float:
    """When a drawn calibration trial runs out, on time.monotonic's timeline: its expiry is on
    CLOCK_BOOTTIME (player/wayland_frames.py), which runs ahead of it by the time suspended."""
    boottime = getattr(time, "CLOCK_BOOTTIME", None)
    ahead = 0.0 if boottime is None else time.clock_gettime(boottime) - time.monotonic()
    return trial.candidate.expires_boottime_ms / 1000 - ahead


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


@dataclass(frozen=True)
class _Steps:
    """Which of the frame pass's steps a frame computes: each switch is player/geometry.py's skip
    rule, and each set is its own compiled program (`_frame_source`), so a step switched off
    costs a frame nothing (a uniform switch left the GPU running every step's code)."""
    lut_on: bool
    saturation_on: bool
    rolloff_on: bool
    floor_on: bool
    mat_on: bool
    test_pattern: int       # _PATTERN_CODES (0: the photos)


def _steps(operations: PictureOperations | None, shadow: MatShadow | None, *,
           lut_drawn: bool) -> _Steps:
    """The step set for `operations` (neutral when None) and the Frame's mat shadow (None: none).
    `lut_drawn`: the LUT `operations.lut` names is bound on texture unit 1
    (`NativeRenderer._bind_lut`), so step 1 mixes it in; otherwise step 1 is skipped."""
    return _Steps(
        lut_on=lut_drawn and operations is not None and operations.lut is not None,
        saturation_on=operations is not None and operations.saturation != 1.0,
        rolloff_on=operations is not None and not rolloff_skipped(operations),
        floor_on=operations is not None and operations.black_floor > 0,
        mat_on=shadow is not None,
        test_pattern=_PATTERN_CODES[None if operations is None else operations.test_pattern])


def _frame_source(steps: _Steps) -> str:
    """The frame pass's fragment shader for one step set: its switches as constants."""
    return _FINAL.replace(_STEP_SWITCHES, "".join(
        f"const int {name} = {int(getattr(steps, name))};\n" for name in _Steps.__dataclass_fields__))


_NEUTRAL_STEPS = _Steps(False, False, False, False, False, 0)


def _final_uniforms(gl, program: int, operations: PictureOperations | None, opening,
                    shadow: MatShadow | None, steps: _Steps,
                    target: tuple[int, BoardDraw] | None = None) -> None:
    """The frame pass's operations (neutral when None), the Frame's mat shadow (None: none) and the
    per-Output mask's opening, for the program of `steps` (a step switched off sets its neutral
    value). A test pattern (`operations.test_pattern`) needs the opening's unit square, so
    `to_opening` is set whenever a pattern or a shadow is on; with no Frame drawn yet the opening
    is `_NO_OPENING`, off the output, and the pattern stays unseen (a known limit: an Output shows
    a pattern only once a Frame has drawn there). `target`: for a calibration target, the QR's
    module count bound on _TARGET_QR_UNIT (0: no code, every QR square draws light) and the
    opening's BoardDraw (`board_draw`: its columns, aspect and turn; its modules unread, the count
    stands for them); the board's raster is bound on _TARGET_LAYOUT_UNIT
    (`NativeRenderer._bind_target`). None: no columns, so nothing is drawn."""
    def uniform(name: str) -> int:
        return gl.glGetUniformLocation(program, name)

    multipliers, light, contrast, gamma = _NEUTRAL if operations is None else (
        operations.rgb_multipliers, operations.light_factor, operations.contrast_slope,
        operations.gamma_exponent)
    gl.glUniform3f(uniform("rgb_multipliers"), *multipliers)
    gl.glUniform1f(uniform("light_factor"), light)
    gl.glUniform1f(uniform("contrast_slope"), contrast)
    gl.glUniform1f(uniform("gamma_exponent"), gamma)
    gl.glUniform1i(uniform("lut"), _LUT_UNIT)       # never unit 0: two sampler types may not share one
    gl.glUniform1f(uniform("lut_amount"), operations.lut.amount if steps.lut_on else 0.0)
    gl.glUniform1f(uniform("saturation"), operations.saturation if steps.saturation_on else 1.0)
    gl.glUniform1f(uniform("highlight_knee"), operations.highlight_knee if steps.rolloff_on else 1.0)
    gl.glUniform1f(uniform("peak"), peak(operations) if steps.rolloff_on else 1.0)
    gl.glUniform1f(uniform("black_floor"), operations.black_floor if steps.floor_on else 0.0)
    gl.glUniform1i(uniform("target_layout"), _TARGET_LAYOUT_UNIT)   # each sampler on its own unit
    gl.glUniform1i(uniform("target_qr"), _TARGET_QR_UNIT)
    modules, draw = target if target is not None else (0, None)
    gl.glUniform1i(uniform("target_qr_modules"), modules)
    gl.glUniform1i(uniform("target_columns"), draw.columns if draw is not None else 0)
    gl.glUniform1f(uniform("target_aspect"), draw.aspect if draw is not None else 1.0)
    gl.glUniform1i(uniform("target_portrait"), int(draw is not None and draw.portrait))
    if shadow is not None:
        _set_matrix(gl, program, "to_opening", shadow.to_opening)
    elif steps.test_pattern:
        _set_matrix(gl, program, "to_opening", inverse(homography(opening)))
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


def _layer_uniforms(gl, program: int, slots: tuple, matrix: Matrix) -> None:
    """The frame pass's layers, bottom to top (`NativeRenderer._slots`): each slot's kind and
    strength, its fitted picture bound on its unit, and the drawn fit's matrix (a black layer and
    the commissioning grid cover the Frame's picture square). Texture unit 0 stays active."""
    if len(slots) > FRAME_LAYERS:
        raise RuntimeError("frame_layers_exceeded")
    padding = (0,) * (FRAME_LAYERS - len(slots))
    gl.glUniform1i(gl.glGetUniformLocation(program, "layer_count"), len(slots))
    gl.glUniform1iv(gl.glGetUniformLocation(program, "layer_kind"), FRAME_LAYERS,
                    [kind for kind, _, _, _ in slots] + list(padding))
    gl.glUniform1fv(gl.glGetUniformLocation(program, "layer_alpha"), FRAME_LAYERS,
                    [alpha for _, _, _, alpha in slots] + [0.0 for _ in padding])
    for index in range(FRAME_LAYERS):
        gl.glUniform1i(gl.glGetUniformLocation(program, f"layer{index}"), _FITTED_UNIT + index)
    for index, (_, texture, _, _) in enumerate(slots):
        if texture is not None:
            gl.glActiveTexture(gl.GL_TEXTURE0 + _FITTED_UNIT + index)
            gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
    gl.glActiveTexture(gl.GL_TEXTURE0)
    _set_matrix(gl, program, "output_to_picture", matrix)


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
    """Everything but time that changes a surface's pixels (G8 render on change, E-G8-3):
    layers and their timeline, decoder generations, each video's newest sample, the calibration
    trial and the Output's picture operations (1c). How the layers look over time (a fade) is
    drawn on the frame clock (`player/look.py`)."""
    return (_fingerprint(composition), composition.timebase, generations, sample_serials,
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
    # When the calibration trial it draws runs out (monotonic); None: none drawn.
    expires: float | None = None


@dataclass
class _Fitted:
    """A layer's fitted picture (player/fitted.py): a GL_RGBA16F texture of the Output's size and
    the framebuffer that draws it."""
    framebuffer: int
    texture: int
    size: tuple[int, int]
    # The key it was drawn for; None while it holds no finished draw (new, or a draw failed).
    key: FitKey | None = None
    # The GPU's marker after its draw (glFenceSync); None once the GPU passed it.
    fence: Any = None
    # The decode of the last draw the GPU finished (player/fitted.py `prepared`), kept while a
    # newer draw of it (an edit, a resize) is in flight; None: none finished, or a draw failed.
    finished: tuple[int, int] | None = None


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
    # Each layer's fitted picture on this Output (player/fitted.py), by assignment.
    fitted: dict[str, _Fitted] = field(default_factory=dict)
    # What the GLArea's buffer holds (`NativeRenderer._frame_key` of its last frame pass); None:
    # unknown, so the next render runs the pass. GTK keeps the buffer between renders, so a
    # render whose frame equals it (GTK repainting on its own) draws nothing.
    painted: tuple | None = None
    size: tuple[int, int] = (0, 0)
    # The fit pass's program and the frame pass's for neutral steps, compiled with the surface's
    # GL context; None: none yet (not realized, or unrealized).
    programs: tuple[int, int] | None = None
    # The frame pass's program per step set (`_frame_source`), each compiled on first use.
    frame_programs: dict[_Steps, int] = field(default_factory=dict)
    # The step set the last frame pass ran; None before one.
    steps: _Steps | None = None
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
    # Follows the surface's frame clock while its drawn look changes over time, or soon will, or
    # a draw waits for the pacer (`NativeRenderer._frame`); a still picture is not followed.
    follower: FrameFollower | None = None
    # When the next frame may be drawn (player/pacing.py): the compositor's words for this
    # surface's tagged commits.
    pacer: FramePacer = field(default_factory=FramePacer)
    # An event since the last draw changed what the drawn composition shows (a video's new
    # sample, Display Host's edit): the next render draws it again (`NativeRenderer._invalidate`).
    dirty: bool = False
    # A draw is owed but a layer it shows holds no decoded picture yet: frames cannot change that
    # (only prepare() reads a decoder's sample), so the frame clock is not followed until the
    # planning tick or an event wakes the surface (`NativeRenderer._owed`).
    waiting: bool = False

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
# Where an output point falls in the Frame's picture (player/geometry.py `picture_source_uv`):
# the fit's one matrix onto the picture's unit square; a point of the Frame is inside the opening
# (the per-Frame mask) and inside that square.
_PICTURE = """
uniform mat3 output_to_picture;
vec2 picture_point(vec2 point) {
    vec3 q = output_to_picture * vec3(point, 1.0);
    return q.xy/q.z;
}
bool in_frame(vec2 point, vec2 p) {
    return inside_opening(point) && all(greaterThanEqual(p, vec2(0.0))) && all(lessThanEqual(p, vec2(1.0)));
}
"""
# The fit pass (player/fitted.py): one layer's fitted picture, drawn once per FitKey into its own
# linear floating-point texture (cleared to transparent): the per-Frame mask, the fit's matrix, the
# photo's rectangle (Photo fit), the transfer to linear light, premultiplied, at full strength.
_FIT = """#version 300 es
precision highp float;
in vec2 uv;
out vec4 color;
uniform sampler2D media;
uniform vec4 photo;
uniform int video;
""" + _OPENING + _PICTURE + """
vec3 linear_rgb(vec3 value) {
    if (video == 1) {
        return mix(pow((value+0.099)/1.099, vec3(1.0/0.45)), value/4.5,
                   lessThan(value, vec3(0.081)));
    }
    return mix(pow((value+0.055)/1.055, vec3(2.4)), value/12.92,
               lessThanEqual(value, vec3(0.04045)));
}
void main() {
    vec2 p = picture_point(uv);
    if (!in_frame(uv, p)) discard;
    vec4 sample_color = texture(media, mix(photo.xy, photo.zw, p));
    color = vec4(linear_rgb(sample_color.rgb)*sample_color.a, sample_color.a);
}
"""
# The frame pass, the one pass each shown frame draws: the composition of the Frame's layers at
# the frame's strengths (player/geometry.py `compose_pixel`: linear premultiplied source-over,
# bottom to top, from black; per slot a fitted picture, a black layer or Position's commissioning
# grid), then `final_pixel`: contracts/node_frame.py PictureOperations in the print look's order,
# each step switched off (never computed) at its neutral value, then the Frame's mat shadow, then
# the per-Output mask LAST, so black stays black outside the opening whatever the picture settings
# and the shadow (owner 2026-10-10 q2). A test pattern (display tuning, step 0) replaces the
# composed picture here, so Position's grid and every Frame layer are replaced with no change to
# the fit pass; the Black level skips every step and the mat. geometry.py's constants are written
# in (`_FINAL_CONSTANTS`), so each has one home.
_LUT_UNIT = 1      # the LUT's texture unit; unit 0 is the fit pass's photo
_TARGET_LAYOUT_UNIT = 2     # the screen board's raster (contracts.calibration_target.board_raster)
_TARGET_QR_UNIT = 3         # its QR's modules
_FITTED_UNIT = 4            # slot i's fitted picture is on unit _FITTED_UNIT + i
FRAME_LAYERS = 8
"""The most layers one frame composes, one sampler each (ES 3.0 guarantees 16 texture units; the
LUT and the target take three). A frame that sees more fails its surface rather than drop one."""
_SLOT_PICTURE, _SLOT_BLACK, _SLOT_COMMISSIONING = 1, 2, 3
_FINAL_CONSTANTS = (
    f"#define FRAME_LAYERS {FRAME_LAYERS}\n"
    f"#define SLOT_PICTURE {_SLOT_PICTURE}\n"
    f"#define SLOT_BLACK {_SLOT_BLACK}\n"
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
_STEP_SWITCHES = "// The step set: _frame_source writes each switch here as a constant.\n"
_FINAL = """#version 300 es
precision highp float;
precision highp sampler3D;
in vec2 uv;
out vec4 color;
""" + _STEP_SWITCHES + """
""" + _FINAL_CONSTANTS + """
uniform int layer_count;
uniform int layer_kind[FRAME_LAYERS];
uniform float layer_alpha[FRAME_LAYERS];
uniform sampler2D layer0;
uniform sampler2D layer1;
uniform sampler2D layer2;
uniform sampler2D layer3;
uniform sampler2D layer4;
uniform sampler2D layer5;
uniform sampler2D layer6;
uniform sampler2D layer7;
uniform vec3 rgb_multipliers;
uniform float light_factor;
uniform float contrast_slope;
uniform float gamma_exponent;
uniform sampler3D lut;
uniform float lut_amount;
uniform float saturation;
uniform float highlight_knee;
uniform float peak;
uniform float black_floor;
uniform sampler2D target_layout;
uniform sampler2D target_qr;
uniform int target_qr_modules;
uniform int target_columns;
uniform float target_aspect;
uniform int target_portrait;
uniform mat3 to_opening;
uniform vec2 mat_size;
uniform int light_count;
uniform vec2 light_toward[MAT_LIGHTS_MAX];
uniform vec2 light_band[MAT_LIGHTS_MAX];
uniform vec3 light_weight[MAT_LIGHTS_MAX];
""" + _OPENING + _PICTURE + """
// Slot i's fitted picture at st (ES 3.00 indexes a sampler array only by a constant).
vec4 fitted(int i, vec2 st) {
    if (i == 0) return texture(layer0, st);
    if (i == 1) return texture(layer1, st);
    if (i == 2) return texture(layer2, st);
    if (i == 3) return texture(layer3, st);
    if (i == 4) return texture(layer4, st);
    if (i == 5) return texture(layer5, st);
    if (i == 6) return texture(layer6, st);
    return texture(layer7, st);
}
// The Frame's layers composed at `point` (geometry.py `compose_pixel`), in linear light. A fitted
// picture is drawn in GL's lower-left orientation, unlike decoded bytes.
vec3 composed(vec2 point) {
    vec2 st = vec2(point.x, 1.0-point.y);
    vec2 p = picture_point(point);
    bool framed = in_frame(point, p);
    vec3 value = vec3(0.0);
    for (int i = 0; i < FRAME_LAYERS; i++) {
        if (i >= layer_count) break;
        float alpha = layer_alpha[i];
        if (layer_kind[i] == SLOT_PICTURE) {
            vec4 source = fitted(i, st)*alpha;
            value = source.rgb+value*(1.0-source.a);
        } else if (framed) {
            if (layer_kind[i] == SLOT_BLACK) {
                value *= 1.0-alpha;
            } else {
                vec2 grid = fract(p*10.0);
                float line = float(grid.x < 0.035 || grid.y < 0.035);
                value = mix(vec3(0.2, 0.3, 0.45), vec3(0.9), line);
            }
        }
    }
    return value;
}
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
        value = test_pattern == 0 ? composed(uv) : srgb_decoded(pattern_signal(uv));
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
        self._display_events = None
        if os.environ.get("PHOTO_WALL_DISPLAY_HOST") == "1":
            from player.wayland_frames import WaylandFrames
            display = self.Gdk.Display.get_default()
            if "Wayland" not in type(display).__name__:
                raise RuntimeError("node_display_requires_wayland")
            self._frames = WaylandFrames(display)
            self._display_events = DisplayEvents(self.GLib, self._frames.fd, self._arrived)
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
            area.connect("resize", self._resized, surface)
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

    def _due(self, surface: _Surface) -> float | None:
        """When the surface next owes a draw, on the monotonic clock: now (-inf) while a draw is
        pending or an event marked the drawn one; else when its drawn look next changes over time
        (`player/look.py` `next_change`: a fade, the drawn trial running out); None: nothing."""
        drawn = surface.acknowledged
        if surface.waiting:
            return None
        if surface.pending is not None or (surface.dirty and drawn is not None):
            return -math.inf
        if drawn is None or drawn.at is None:
            return None
        return next_change(drawn.composition, drawn.at, drawn.expires)

    def _frame(self, surface: _Surface, at: float) -> bool:
        """The surface's per-frame hook (FrameFollower) at frame time `at`: when a draw is owed by
        `at`, queue the render if the pacer lets a frame be drawn now; keep following while one
        is owed within FOLLOW_LEAD (a draw the pacer holds included)."""
        due = self._due(surface)        # close() stops every follower first
        if due is not None and due <= at:
            self._request(surface, at)
        return due is not None and due <= at + FOLLOW_LEAD

    def _request(self, surface: _Surface, at: float) -> None:
        """A draw is owed by `at`: queue the render when the pacer lets a frame be drawn at `at`
        (player/pacing.py: the last frame was answered, its slot reached), else follow the frame
        clock, whose frames ask again (`_frame`) and read the compositor's word. The one place a
        draw is asked for: nothing draws while nothing is owed."""
        self._heard(surface, at)
        if surface.pacer.ready(at):
            surface.area.queue_render()
        else:
            surface.follower.follow()

    def _heard(self, surface: _Surface, at: float) -> None:
        """Give the pacer the compositor's newest word for the surface's commits, while one it
        waits for may have come (read at `at`)."""
        frames = getattr(self, "_frames", None)
        if frames is None or not surface.pacer.in_flight:
            return
        word = frames.presentation(surface.window.get_window(), at)
        if word is not None:
            surface.pacer.answered(word, at)

    def _wake(self, surface: _Surface, now: float) -> None:
        """Follow the frame clock when the surface owes a draw within FOLLOW_LEAD of `now`: after
        each render (a fade drawn, a draw not decoded yet) and on each planning tick's present()
        (the wake for a change far ahead, such as a still photo's fade-out)."""
        due = self._due(surface)
        if due is not None and due <= now + FOLLOW_LEAD:
            surface.follower.follow()

    def _invalidate(self, surface: _Surface) -> None:
        """An event changed what the surface's drawn composition shows: the next frame the pacer
        allows draws it again (GTK paints a queued render on the next frame of its clock)."""
        surface.dirty, surface.waiting = True, False
        self._request(surface, time.monotonic())

    def _arrived(self) -> None:
        """Display Host's events arrived (`DisplayEvents`): a surface whose drawn composition
        would now draw otherwise (`_shown`: the Output's picture operations, a trial edit) is
        drawn again on the next frame; an event that changes nothing costs no draw."""
        for surface in self._surfaces.values():
            drawn = surface.acknowledged
            if (not self._closed and drawn is not None and not surface.dirty
                    and self._current(drawn.generations)
                    and self._shown_now(drawn.composition, drawn.generations) != drawn.shown):
                self._invalidate(surface)

    def _sample_arrived(self, key: str, incarnation: int) -> None:
        """A decoder brought a new sample (its streaming thread hops here once per sample): the
        surface drawing it draws it on the next frame; one not drawing it does nothing."""
        decoder = self._decoders.get(key)
        if self._closed or decoder is None or decoder.incarnation != incarnation:
            return
        surface = self._surfaces.get(decoder.local.layer.output_id)
        drawn = surface.acknowledged if surface else None
        if drawn is not None and any(drawn_key == key for drawn_key, _, _ in drawn.generations):
            self._invalidate(surface)

    def _owed(self, surface: _Surface, at: float) -> _Draw | None:
        """The one redraw decision, for the frame at `at`: the pending draw (a new composition
        from present()), else the drawn one again when an event marked it (`dirty`) or its look
        changed by `at` (`_due`), in either case as it looks at `at`, with the calibration trial
        Display Host holds now. None: what is drawn stands, also while a layer seen at `at` cannot
        be drawn yet (`_ready`: not decoded, or its first fitted picture not finished): the draw
        stays owed and the surface `waiting`, unfollowed until the planning tick (whose prepare()
        reads the decoder and draws the fitted picture) wakes it."""
        draw, drawn = surface.pending, surface.acknowledged
        if draw is not None and draw.generations != self._generations(draw.composition):
            surface.pending = draw = None
        if draw is None:
            due = self._due(surface)
            if drawn is None or due is None or due > at:
                return None
            if not self._current(drawn.generations):
                surface.dirty = False           # a decoder was sought: present() owes the draw
                return None
            self._serial += 1
            draw = _Draw(self._serial, drawn.composition, drawn.generations)
        frames = getattr(self, "_frames", None)
        if frames is not None:
            grant = frames.grant(surface.output.output_id, draw.composition)
            if grant is None or not grant.admitted:
                surface.pending, surface.dirty = None, False
                return None
        seen = look(draw.composition, at)
        self._apply_trial(draw)
        if not self._ready(surface, seen, _drawn_fit(draw)):
            surface.waiting = True
            return None
        draw.composition, surface.dirty = seen, False
        return draw

    def _apply_trial(self, draw: _Draw) -> None:
        """Record on `draw` what Display Host's calibration trial for its composition draws now: the
        fit's calibration, the mat, the primitives and when it runs out (no trial: the
        composition's calibration and the binding's mat). The one read of the drawn fit, for a
        frame and for a fitted picture prepare() draws ahead of its frame."""
        frames = getattr(self, "_frames", None)
        trial = None
        if frames is not None:
            grant = frames.grant(draw.composition.binding.output_id, draw.composition)
            if grant is not None:
                trial = frames.trial(draw.composition, grant)
        draw.applied_calibration = AppliedCalibration(
            calibration=trial.calibration if trial else draw.composition.calibration,
            trial_id=str(trial.candidate.trial_id) if trial else None,
            trial_generation=trial.candidate.generation if trial else None,
            trial_sequence=trial.candidate.sequence if trial else None,
            candidate_sha256=trial.candidate.candidate_sha256 if trial else None)
        draw.applied_mat = trial.mat if trial else None
        draw.primitives = trial.primitives if trial else None
        draw.expires = _expires(trial) if trial else None

    def _key(self, surface: _Surface, local: LocalLayer, binding: OutputBinding, fit,
             decoder: _Decoder) -> FitKey:
        """The FitKey `local` is drawn for now on `surface`, with the drawn `fit`."""
        profile = binding.profile
        return FitKey(decode=(decoder.incarnation, decoder.generation),
                      sample=decoder.sample_serial, fit=fit,
                      aspect=profile.width_px / profile.height_px, crop=local.layer.crop,
                      size=surface.size)

    def _done(self, fitted: _Fitted | None) -> bool:
        """Whether `fitted` holds a finished draw: drawn, and the GPU past its fence (asked
        without waiting), which records its decode as `finished`. The surface's GL context is
        current."""
        if fitted is None or fitted.key is None:
            return False
        if fitted.fence is not None:
            gl = self.GL
            if gl.glClientWaitSync(fitted.fence, 0, 0) not in (gl.GL_ALREADY_SIGNALED,
                                                               gl.GL_CONDITION_SATISFIED):
                return False
            gl.glDeleteSync(fitted.fence)
            fitted.fence = None
        fitted.finished = fitted.key.decode
        return True

    @staticmethod
    def _on_screen(surface: _Surface) -> set[str]:
        """The pictures the surface's last completed draw shows."""
        drawn = surface.acknowledged
        return set() if drawn is None else {
            local.layer.assignment_id for local in visible_now(drawn.composition.layers)
            if local.layer.variant is not None}

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

    def _ready(self, surface: _Surface, composition: OutputComposition, fit) -> bool:
        """Every layer seen in `composition` can be drawn by this frame: decoded, and either on
        screen already (the frame redraws a fitted picture an edit or a newer video sample made
        stale, as it draws any edit) or with its fitted picture finished for this picture
        (player/fitted.py `shown_at_once`). A frame never draws a layer's first fitted picture,
        so a fade's first frame costs one frame pass. The surface's GL context is current."""
        on_screen = self._on_screen(surface)
        for local in visible_now(composition.layers):
            if local.layer.variant is None:
                continue
            key = local.layer.assignment_id
            decoder = self._decoders.get(key)
            if decoder is None or not decoder.prepared or decoder.failure:
                return False
            fitted = surface.fitted.get(key)
            drawn = fitted.key if fitted else None
            now = self._key(surface, local, composition.binding, fit, decoder)
            if redrawn_by_frames(drawn, now, key in on_screen):
                continue
            if not shown_at_once(drawn, now, self._done(fitted)):
                return False
        return True

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
        owner, renderer = weakref.ref(decoder), weakref.ref(self)
        key, incarnation = local.layer.assignment_id, decoder.incarnation
        # One main-loop hop per sample (a hop still queued carries the next sample too).
        hop = MainLoopDispatcher(self.GLib, 1)

        def arrived():
            if (current := renderer()) is not None:
                current._sample_arrived(key, incarnation)

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
                hop(arrived)
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
        """Resident pictures (decoders and those `compositions` add) and their texture bytes."""
        pictures = {key: (d.local.layer.output_id, d.local.layer.variant)
                    for key, d in self._decoders.items()}
        for composition in compositions:
            for local in composition.layers:
                if local.layer.variant is not None:
                    pictures[local.layer.assignment_id] = (local.layer.output_id, local.layer.variant)
        return len(pictures), sum(self._picture_bytes(output_id, variant)
                                  for output_id, variant in pictures.values())

    def _picture_bytes(self, output_id: str, variant) -> int:
        """One picture's texture bytes: its decoded RGBA8 texture, kept to draw its fitted picture
        again after an edit, and its fitted picture at its Output's size (or the size it was
        configured with, before the first render)."""
        surface = self._surfaces.get(output_id)
        pixels = 0 if surface is None else max(surface.output.width*surface.output.height,
                                               surface.size[0]*surface.size[1])
        return variant.width*variant.height*4 + pixels*FITTED_BYTES

    def capacity(self, compositions: tuple[OutputComposition, ...]) -> CapacityResult:
        self._thread()
        count, size = self._estimate(compositions)
        surfaces_ok = all(surface.programs is not None and not surface.failed()
                          for surface in self._surfaces.values())
        return CapacityResult(surfaces_ok and count <= self.decoder_limit
                              and size <= self.texture_budget, qualified=False)

    def prepare(self, local: LocalLayer, binding: OutputBinding) -> PrepareResult:
        """`local`, on `binding`'s Frame: prepared once decoded and its fitted picture drawn and
        finished (player/fitted.py `prepared`), drawn here on the planning tick while its Output
        is still (not following its frame clock), so a photo's first frame (a fade's first) only
        mixes it."""
        self._thread()
        return self._prepare(local, OutputComposition(binding), seen=False)

    def _prepare(self, local: LocalLayer, composition: OutputComposition, *,
                 seen: bool) -> PrepareResult:
        """prepare() for a layer of `composition` (whose calibration and binding a frame draws it
        with), and for `seen` (present()'s layers seen now) its fitted picture drawn whatever the
        Output shows: it is late, and its frames wait for it."""
        decoded = self._decode(local)
        if decoded.status != "prepared" or local.layer.variant is None:
            return decoded
        return self._prepare_fitted(local, composition, seen=seen)

    def _prepare_fitted(self, local: LocalLayer, composition: OutputComposition, *,
                        seen: bool) -> PrepareResult:
        """Draw `local`'s fitted picture when it is not on screen and has none for its picture now
        (missing, or stale after an edit), if `seen` or its Output is still, and no draw of it is
        in flight (the GPU has not passed its fence: drawn once, never again before that); a
        picture on screen is its frames' to redraw. Prepared by player/fitted.py `prepared`."""
        surface = self._surfaces[local.layer.output_id]
        key = local.layer.assignment_id
        decoder = self._decoders[key]
        binding = composition.binding
        probe = _Draw(0, composition, ())
        try:
            self._apply_trial(probe)
        except Exception:
            return PrepareResult("pending")         # Display Host unreadable now: the next tick
        surface.area.make_current()
        if surface.area.get_error() is not None or surface.size == (0, 0):
            return PrepareResult("pending")
        now = self._key(surface, local, binding, _drawn_fit(probe), decoder)
        fitted = surface.fitted.get(key)
        drawn = fitted.key if fitted else None
        in_flight = drawn is not None and not self._done(fitted)
        if (not in_flight and not now.same_picture(drawn)
                and not redrawn_by_frames(drawn, now, key in self._on_screen(surface))
                and (seen or not surface.follower.following)):
            try:
                fitted = self._fit(surface, local, now)
            except Exception:
                decoder.failure, decoder.prepared = "decode", False
                decoder.retry_at = time.monotonic()+1
                return PrepareResult("failed", "decode")
            finally:
                # Outside GTK's paint: the window's own framebuffer is bound again, as GTK left it.
                self.GL.glBindFramebuffer(self.GL.GL_FRAMEBUFFER, 0)
            self._done(fitted)
        return PrepareResult("prepared" if prepared(fitted.finished if fitted else None, now)
                             else "pending")

    def _decode(self, local: LocalLayer) -> PrepareResult:
        """`local`'s decoder: made, read, sought to its position; prepared once it holds a sample
        and the surface's programs exist."""
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
            if (count >= self.decoder_limit
                    or size+self._picture_bytes(local.layer.output_id, variant) > self.texture_budget):
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
        # The layers seen now (the tick's look): each is decoded, and its fitted picture drawn if
        # the planning tick could not draw it ahead (late), since its frames wait for it.
        visible = visible_now(look(composition, time.monotonic()).layers)
        for local in visible:
            result = self._prepare(local, composition, seen=True)
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
        # Render on change: a new draw only when what it shows changed (layers, their timeline,
        # decoder generations, a new video sample, a trial edit). A still photo is never redrawn
        # (nothing renews it: the shell's lease holds on the app's answers to its pings); a look
        # changing over time is drawn on the frame clock, which this tick wakes when its change
        # comes near.
        shown = self._shown_now(composition, generations)
        # The newest draw is the pending one, else the acknowledged one: whatever _render
        # dropped or drew, present() reads it from those two and nothing else.
        ack = surface.acknowledged
        newest = surface.pending or ack
        now = time.monotonic()
        surface.waiting = False                     # this tick's prepare() read the decoders
        self._wake(surface, now)
        if newest is None or newest.shown != shown:
            self._serial += 1
            surface.pending = _Draw(self._serial, composition, generations, shown=shown)
            self._request(surface, now)
        # Presented is a state: the newest draw stands until superseded, its grant withdrawn or
        # its decoders replaced (each clears or replaces `acknowledged`). It stays keyed on
        # layers and generations only: a playing video's newer sample or a frame of a crossfade
        # never makes the composition already drawn read as pending.
        if (ack and _fingerprint(ack.composition) == _fingerprint(composition)
                and ack.generations == generations):
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
                self._free_fitted(surface, assignment_id)

    def diagnostics(self) -> dict:
        self._thread()
        return {"qualified": False, "resident_decoders": len(self._decoders),
                "estimated_texture_bytes": self._estimate(())[1],
                "outputs": {key: {"size": surface.size, "failure": surface.failed(),
                                  "fitted": sorted(surface.fitted),
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

    def _setup(self, surface: _Surface, width: int, height: int) -> None:
        """The surface's programs (compiled once per GL context) and its drawn size. A new size is
        an edit like any other (player/fitted.py): every fitted picture stays (its key names the
        old size, so each is drawn again at the new one before it is shown, and stays prepared),
        what is on screen is drawn again by this render (`dirty`), and the GLArea's buffer, which
        GTK made anew, holds nothing painted."""
        gl = self.GL
        if surface.programs is None:
            fit_program = self._program(_FIT)
            try:
                final_program = self._program(_frame_source(_NEUTRAL_STEPS))
            except Exception:
                gl.glDeleteProgram(fit_program)
                raise
            surface.programs = fit_program, final_program
            surface.frame_programs = {_NEUTRAL_STEPS: final_program}
            surface.vao = int(gl.glGenVertexArrays(1))
        if surface.size == (width, height):
            return
        old_size = surface.size
        surface.size = (width, height)
        if self._estimate(())[1] > self.texture_budget:
            surface.size = old_size
            raise RuntimeError("resized Output exceeds texture budget")
        surface.painted = None
        surface.dirty, surface.waiting = True, False

    def _new_fitted(self, surface: _Surface) -> _Fitted:
        """A fitted picture's texture (GL_RGBA16F, the surface's size) and framebuffer."""
        gl = self.GL
        texture = self._texture(*surface.size, floating=True)
        try:
            framebuffer = int(gl.glGenFramebuffers(1))
        except Exception:
            gl.glDeleteTextures([texture])
            raise
        fitted = _Fitted(framebuffer, texture, surface.size)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, framebuffer)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                  gl.GL_TEXTURE_2D, texture, 0)
        if gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) != gl.GL_FRAMEBUFFER_COMPLETE:
            self._delete_fitted(fitted)
            raise RuntimeError("linear floating framebuffer unsupported")
        return fitted

    def _delete_fitted(self, fitted: _Fitted) -> None:
        gl = self.GL
        if fitted.fence is not None:
            gl.glDeleteSync(fitted.fence)
            fitted.fence = None
        gl.glDeleteFramebuffers(1, [fitted.framebuffer])
        gl.glDeleteTextures([fitted.texture])

    def _free_fitted(self, surface: _Surface, key: str) -> None:
        """Delete `key`'s fitted picture on `surface` (its GL context current), if it has one."""
        fitted = surface.fitted.pop(key, None)
        if fitted is None:
            return
        self._delete_fitted(fitted)
        if surface.painted is not None and any(texture == fitted.texture
                                               for _, texture, _, _ in surface.painted[1]):
            surface.painted = None          # a later texture may take its name

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

    def _fit(self, surface: _Surface, local: LocalLayer, key: FitKey) -> _Fitted:
        """Draw `local`'s fitted picture for `key` (the fit pass) into its texture, made at the
        surface's size when it has none or one of another size (a resize), and mark the GPU's
        progress with a fence; flushed, so the GPU draws it now. Its key is None until the draw is
        issued without a GL error, so a failed draw is never shown, and a failed draw forgets the
        finished one too (its texture was cleared). The surface's GL context is current; the
        framebuffer is left bound."""
        gl = self.GL
        assignment = local.layer.assignment_id
        fitted = surface.fitted.get(assignment)
        if fitted is None or fitted.size != surface.size:
            resized = self._new_fitted(surface)
            if fitted is not None:
                resized.finished = fitted.finished      # still this decode's, being redrawn
                self._free_fitted(surface, assignment)
            fitted = surface.fitted[assignment] = resized
        fitted.key = None
        if fitted.fence is not None:
            gl.glDeleteSync(fitted.fence)
            fitted.fence = None
        try:
            self._fit_pass(surface, local, key, fitted)
        except Exception:
            fitted.finished = None
            raise
        decoder = self._decoders[assignment]
        fitted.key = replace(key, sample=decoder.sample_serial)    # the sample uploaded
        return fitted

    def _fit_pass(self, surface: _Surface, local: LocalLayer, key: FitKey, fitted: _Fitted) -> None:
        """The fit pass's GL work for `_fit`: `local`'s decoded picture through `key`'s fit into
        `fitted`'s framebuffer, then its fence. Raises on a GL error."""
        gl = self.GL
        texture = self._upload(surface, local)
        program, variant = surface.programs[0], local.layer.variant
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fitted.framebuffer)
        gl.glBindVertexArray(surface.vao)
        gl.glViewport(0, 0, *fitted.size)
        gl.glDisable(gl.GL_BLEND)
        gl.glClearColor(0, 0, 0, 0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        gl.glUseProgram(program)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
        gl.glUniform1i(gl.glGetUniformLocation(program, "media"), 0)
        _set_matrix(gl, program, "output_to_picture", output_to_picture(key.fit, fitted.size, key.aspect))
        _set_opening(gl, program, key.fit.corners)
        gl.glUniform4f(gl.glGetUniformLocation(program, "photo"),
                       *photo_rect(key.aspect, variant.width, variant.height, local.layer.crop))
        gl.glUniform1i(gl.glGetUniformLocation(program, "video"), int(bool(variant.duration)))
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        fitted.fence = gl.glFenceSync(gl.GL_SYNC_GPU_COMMANDS_COMPLETE, 0)
        gl.glFlush()
        if gl.glGetError() != gl.GL_NO_ERROR:
            raise RuntimeError("fit GL error")

    def _redraw_fitted(self, surface: _Surface, draw: _Draw) -> bool:
        """Draw again the fitted picture of each picture `draw` shows whose key changed (`_ready`
        lets only a picture on screen, or a video's newer sample, reach here): an edit or a new
        sample, drawn on this frame as any edit is. False when one fails: that decoder fails (a
        new decoder after a second, as a failed decode) and the draw is dropped, so the screen
        keeps what it shows."""
        fit = _drawn_fit(draw)
        for local in visible_now(draw.composition.layers):
            if local.layer.variant is None:
                continue
            decoder = self._decoders[local.layer.assignment_id]
            self._read_sample(decoder)          # a playing video's newest picture
            key = self._key(surface, local, draw.composition.binding, fit, decoder)
            fitted = surface.fitted.get(local.layer.assignment_id)
            if fitted is not None and fitted.key == key:
                continue
            try:
                self._fit(surface, local, key)
            except Exception:
                decoder.failure, decoder.prepared = "decode", False
                decoder.retry_at = time.monotonic()+1
                surface.pending = None
                return False
        return True

    @staticmethod
    def _slots(surface: _Surface, shown: _Draw | None) -> tuple | None:
        """The frame pass's layers for `shown`, bottom to top: (kind, fitted texture, its key,
        strength) per layer seen; the commissioning grid alone while an explicit trial draws a
        fallback with no layers (it is never a Layer, Run or readiness fact). None: a picture it
        shows has no fitted picture to mix (its last draw failed), so no frame can draw it."""
        if shown is None:
            return ()
        composition = shown.composition
        if (shown.applied_calibration and shown.applied_calibration.trial_id
                and composition.fallback and not composition.layers):
            return ((_SLOT_COMMISSIONING, None, None, 1.0),)
        slots = []
        for local in visible_now(composition.layers):
            if local.layer.variant is None:
                slots.append((_SLOT_BLACK, None, None, local.alpha))
                continue
            fitted = surface.fitted.get(local.layer.assignment_id)
            if fitted is None or fitted.key is None:
                return None
            slots.append((_SLOT_PICTURE, fitted.texture, fitted.key, local.alpha))
        return tuple(slots)

    def _frame_program(self, surface: _Surface, steps: _Steps) -> int:
        """The frame pass's program for `steps`, compiled on its first use with this GL context
        (a picture settings edit that switches a step: one compile, then cached)."""
        program = surface.frame_programs.get(steps)
        if program is None:
            program = surface.frame_programs[steps] = self._program(_frame_source(steps))
        surface.steps = steps
        return program

    def _frame_pass(self, surface: _Surface, shown: _Draw | None, slots: tuple, lut_drawn: bool,
                    target: tuple[int, BoardDraw] | None) -> None:
        """The one pass a shown frame draws, into GTK's framebuffer (bound): the fitted pictures
        and black layers mixed at their strengths, then the Display's picture settings, the mat
        shadow and the per-Output mask (`_FINAL`), with the program of its step set."""
        gl = self.GL
        fit = _drawn_fit(shown) if shown else None
        profile = shown.composition.binding.profile if shown else None
        shadow = mat_shadow(fit, profile, _drawn_mat(shown)) if shown else None
        steps = _steps(surface.operations, shadow, lut_drawn=lut_drawn)
        program = self._frame_program(surface, steps)
        gl.glBindVertexArray(surface.vao)
        gl.glViewport(0, 0, *surface.size)
        gl.glDisable(gl.GL_BLEND)
        gl.glUseProgram(program)
        matrix = (output_to_picture(fit, surface.size, profile.width_px / profile.height_px)
                  if shown else (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0))
        _layer_uniforms(gl, program, slots, matrix)
        _final_uniforms(gl, program, surface.operations, fit.corners if shown else _NO_OPENING,
                        shadow, steps, target=target)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        gl.glFlush()
        if gl.glGetError() != gl.GL_NO_ERROR:
            raise RuntimeError("presentation GL error")

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
        # drawn by this render's frame pass and named in its tag.
        surface.operations = frames.picture(surface.output.output_id) if frames is not None else None
        try:
            # Programs and drawn size on every render, the candidate's too: capacity() needs
            # them, so the first present() after admission can ask for its draw (no later render
            # is owed to set them up: nothing renews a frame).
            self._setup(surface, width, height)
        except Exception as error:
            surface.pending = surface.painted = None
            surface.fail(type(error).__name__)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, gtk_framebuffer)
            gl.glClearColor(0, 0, 0, 1)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT)
            return True
        if frames is not None and (grant is None or not grant.admitted):
            # Before surface admission, show only a fixed synthetic handoff
            # colour. Authored layers remain in Executor under their own grant.
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, gtk_framebuffer)
            gl.glViewport(0, 0, width, height)
            gl.glClearColor(0.12, 0.17, 0.24, 1)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT)
            gl.glFlush()
            if grant is not None and gl.glGetError() == gl.GL_NO_ERROR:
                self._submitted(surface, frames.tag_rendered_buffer(
                    surface.window.get_window(), grant, "candidate-" + str(grant.grant_id)),
                    self._frame_time(area))
            surface.acknowledged = surface.painted = None
            return True
        at = self._frame_time(area)
        try:
            draw = self._owed(surface, at)
            if draw is not None and not self._redraw_fitted(surface, draw):
                draw = None                     # a failed redraw: the draw is dropped
            shown = draw or surface.acknowledged
            slots = self._slots(surface, shown)
            if slots is None and surface.painted is None:
                # What is on screen lost its fitted picture (a failed redraw) and the GLArea's
                # buffer holds nothing known (GTK made it anew): nothing of it can be shown.
                shown = surface.acknowledged = None
                slots = ()
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, gtk_framebuffer)
            lut_drawn = self._bind_lut(surface, surface.operations)
            target = None
            if surface.operations is not None and surface.operations.test_pattern in TARGET_PATTERNS:
                modules = self._bind_target(surface, surface.operations, lut_drawn=lut_drawn)
                if shown:
                    target = (modules, board_draw(_drawn_fit(shown), shown.composition.binding.profile, None))
            # Everything the frame pass reads: GTK keeps the GLArea's buffer between renders, so a
            # frame equal to the one painted (GTK's own repaint, an edit that changed nothing) draws nothing,
            # and a frame that cannot be drawn (`slots` None) keeps what the buffer shows. The LUT
            # bound is the one `operations` names once `lut_drawn` (`_bind_lut`).
            painted = None if slots is None else (
                surface.size, slots, surface.operations, lut_drawn,
                (target, surface.target_code) if target else None,
                _drawn_fit(shown) if shown else None,
                shown.composition.binding.profile if shown else None,
                _drawn_mat(shown) if shown else None)
            if painted is not None and painted != surface.painted:
                surface.painted = None
                self._frame_pass(surface, shown, slots, lut_drawn, target)
                surface.painted = painted
            if draw:
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
                # What it shows now, its decoders' samples read: an event compares against it.
                draw.shown = self._shown_now(draw.composition, draw.generations)
                surface.acknowledged = draw
                if surface.pending is draw:
                    surface.pending = None
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
                self._submitted(surface, frames.tag_rendered_buffer(
                    surface.window.get_window(), grant, tag, surface.acknowledged.primitives,
                    picture_change=surface._tagged_change(surface.operations, lut_drawn)), at)
        except Exception as error:
            surface.pending = surface.painted = None
            surface.fail(type(error).__name__)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, gtk_framebuffer)
            gl.glClearColor(0, 0, 0, 1)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        finally:
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, gtk_framebuffer)
        self._wake(surface, at)         # a fade drawn, or a draw still owed: the next frame
        return True

    @staticmethod
    def _submitted(surface: _Surface, sequence: int | None, at: float) -> None:
        """A tagged commit for the frame at `at`: the pacer holds the next frame until the
        compositor answers it. A tag with no sequence (no feedback source) holds nothing."""
        if isinstance(sequence, int) and sequence > 0:
            surface.pacer.submitted(sequence, at)

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

    @staticmethod
    def _resized(_area, _width, _height, surface: _Surface) -> None:
        """GtkGLArea's "resize": GTK made the GLArea's buffer anew (on every size allocation, the
        size changed or not), so its contents are undefined and the render it emits next must
        paint the frame again, never take the buffer for what was painted."""
        surface.painted = None

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
        for fitted in surface.fitted.values():
            self._delete_fitted(fitted)
        if surface.programs:
            gl.glDeleteProgram(surface.programs[0])
        for program in surface.frame_programs.values():
            gl.glDeleteProgram(program)
        surface.frame_programs, surface.steps = {}, None
        if surface.vao:
            gl.glDeleteVertexArrays(1, [surface.vao])
        surface.textures.clear()
        surface.fitted.clear()
        surface.painted = None
        surface.programs, surface.vao = None, 0
        surface.size = (0, 0)

    def close(self) -> None:
        self._thread()
        if getattr(self, "_display_events", None) is not None:
            self._display_events.stop()
            self._display_events = None
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
