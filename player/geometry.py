"""Scalar calibration/SDR reference. Production pixels are processed by shaders.

Each rule is stated once here; the look is mirrored by player/look_shader.py, the photo fit by the
fit pass (player/fit_pass.py), the patterns by player/pattern_pass.py. A Frame is an upright
rectangle on its screen (its Position's `Placement`), and every rule below works on the picture's
own point (u, v), each 0..1 across and down the picture as it hangs:
- **The picture's point** (`buffer_to_picture`, `placement_uv`): a point of the fit pass's target,
  turned for its screen (contracts/display_turn.py), or of the screen's ground inside the Frame's
  Placement, as the picture's (u, v). The one home of the quarter turn is `UNDO_QUARTER_TURN`.
- **Photo fit** (`photo_rect`, `fit_source_uv`): each photo's source rectangle, the window
  `Layer.crop` names, cut from the Frame's aspect (contracts/photo_crop.py, the one home of
  the window; no trim), stretched over the picture, so the photo is never cropped twice or
  squeezed.
- **Final pass** (`final_pixel`): contracts/node_frame.py PictureOperations in their stated order,
  then the mask LAST, so black stays black outside the Frame whatever the picture settings.

The picture's physical size is the caller's `size_mm` (contracts/models.py `frame_size_mm` of the
drawn box, the binding's `display_mm` and the screen's turn; a screen's turn turns the screen, not
the wall); None when the Display states no size, and then only wash lights add light.

Room light is one physics path in real units, every Frame always through it: the Frame's paper
(`paper_reflectance`, its profile's LUT first) under E(u, v), the room's light summed over the
Frame's lights (`picture_lights`, `irradiance`: a wash, a far light's mat shadow, a close light's
inverse square, cone and shadow), as a signal fitted between the panel's black and its white at the
Display's matched lux (`paper_signal`), the one fixed highlight rolloff (`highlight_rolled`), then
the Display's white balance, contrast and gamma, the dither (`dithered`) and the mask
(`final_pixel`).

The test patterns (`pattern_signal`): a look pattern (White, Grey ramp) replaces the photos in the
picture and goes through white balance, contrast and gamma only, never the room's light, the paper
or the mat (White is the TV's own white for the paper match); the Black level is the raw signal,
through no step; the mask stays last.

The calibration targets v2 (decision 0027) draw the screen board (contracts/calibration_target.py
"v2", OpenCV's ChArUco board drawn at build time as `board_raster`): SCREEN_ROWS squares across the
picture's short side, as many columns as fit (`screen_columns`), centred along its long side,
turned a quarter clockwise in a portrait picture (`picture_board`, `board_point`); each point the
raster's pixel, or a QR copy's module (`board_signal`; player/calibration_target.py
`target_modules`); the board's aspect is the placement's pixels' (`picture_board`).
Pre-software is the board's signal raw everywhere in the picture; post-software runs it through the
Display's white balance, contrast and gamma, as the look patterns.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Final, Literal

from contracts.calibration_target import (
    QR_QUIET_MODULES,
    QR_SQUARES,
    SCREEN_ROWS,
    SQUARE_PX,
    QrModules,
    Surface,
    board_raster,
    qr_copies,
    screen_columns,
)
from contracts.colour import srgb_to_linear
from contracts.display_turn import TURNS, DisplayTurn, wayland_transform
from contracts.frame_fit import Placement
from contracts.frame_lighting import (
    SHEET_WHITENESS,
    AtFull,
    AtReading,
    CloseLight,
    FarLight,
    FrameLighting,
    Paper,
    WashLight,
)
from contracts.kelvin import kelvin_white
from contracts.lut import LUT_GRID, lut_offset
from contracts.models import PhotoCrop
from contracts.node_frame import PictureOperations
from contracts.node_picture import DisplayLight
from contracts.photo_crop import CENTRED, crop_window
from contracts.room_light import (
    ColourDimmer,
    ColourEntry,
    Dimmer,
    FixedEntry,
    Measured,
    MeasuredEntry,
    RoomLightEntry,
    RoomLightsDocument,
    WhiteEntry,
)
from contracts.tuning import SIGNAL_LEVELS, TARGET_PATTERNS, TestPattern

Matrix = tuple[float, float, float, float, float, float, float, float, float]
Linear = tuple[float, float, float]     # linear-light RGB, each 0..1


def transform(matrix: Matrix, x: float, y: float) -> tuple[float, float]:
    divisor = matrix[6]*x + matrix[7]*y + matrix[8]
    if not math.isfinite(divisor) or abs(divisor) < 1e-12:
        raise ValueError("point maps to infinity")
    return ((matrix[0]*x + matrix[1]*y + matrix[2]) / divisor,
            (matrix[3]*x + matrix[4]*y + matrix[5]) / divisor)


# Undo a clockwise quarter turn of the picture, as a matrix on the unit square: the one home of
# the quarter-turn rule (the fit pass's target is the picture turned the other way,
# `buffer_to_picture`).
UNDO_QUARTER_TURN: dict[int, Matrix] = {
    0: (1, 0, 0, 0, 1, 0, 0, 0, 1),
    90: (0, 1, 0, -1, 0, 1, 0, 0, 1),         # (u, v) -> (v, 1-u)
    180: (-1, 0, 1, 0, -1, 1, 0, 0, 1),       # (u, v) -> (1-u, 1-v)
    270: (0, -1, 1, 1, 0, 0, 0, 0, 1),        # (u, v) -> (1-v, u)
}


def decode_channel(value: float, video: bool = False) -> float:
    if not video:
        return srgb_to_linear(value)
    value = min(1.0, max(0.0, value))
    return value / 4.5 if value < .081 else ((value + .099) / 1.099) ** (1/.45)


def encode_srgb(value: float) -> float:
    value = min(1.0, max(0.0, value))
    return 12.92*value if value <= .0031308 else 1.055*value**(1/2.4)-.055


def photo_rect(frame_aspect: float, source_width: int, source_height: int,
               crop: PhotoCrop) -> tuple[float, float, float, float]:
    """The photo's source UV rectangle (left, top, right, bottom): the window `crop` names for
    `frame_aspect` (contracts/photo_crop.py `crop_window`). Pure; ValueError for a non-positive
    or non-finite aspect or size."""
    return crop_window(crop, source_width, source_height, frame_aspect)


# -- The physics' shared rules: luminance, a light's colour, the rolloff's curve, the mat's
# edge and the paper profile's LUT. Each is stated once here and mirrored by player/look_shader.py `LOOK_GLSL`.

LUMA: Final[Linear] = (0.2126, 0.7152, 0.0722)    # Rec. 709 linear luminance: a light's colour at luminance 1
LIGHT_ELEVATION_MIN_DEGREES: Final = 1.0            # a soft far light's lower edge never reaches the wall plane


def rolled_off(value: float, knee: float, peak: float) -> float:
    """The highlight rolloff's curve on one channel (knee < 1 < peak): value <= knee unchanged. Else
    t = (min(value, peak) - k)/(1 - k), w = (peak - k)/(1 - k), g = t (1 + t/w**2)/(1 + t), and the
    result k + (1 - k) g: extended Reinhard with `peak` as its white, so peak -> exactly 1, slope 1
    at the knee, and at peak -> 1 the identity (w = 1 gives g = t). Worked through the shortfall
    1 - g = (w - t)(w + t)/(w**2 (1 + t)) (the same g, rearranged), so at value >= peak, where
    t == w, it is exactly 0 and the result exactly 1. Pure."""
    if value <= knee:
        return value
    t = (min(value, peak) - knee) / (1 - knee)
    w = (peak - knee) / (1 - knee)
    return 1 - (1 - knee) * (w - t) * (w + t) / (w * w * (1 + t))


def light_colour(kelvin: int) -> Linear:
    """A light's colour at `kelvin`, as light of luminance 1 (sum of LUMA_c * c = 1): the kelvin
    table's white (contracts/kelvin.py `kelvin_white`, whose largest channel is 1 for the Display's
    white balance) scaled so a light's lux alone says how bright it is and its kelvin only its
    colour. Pure."""
    white = kelvin_white(kelvin)
    luminance = sum(weight * channel for weight, channel in zip(LUMA, white, strict=True))
    return tuple(channel / luminance for channel in white)


def _cot(degrees: float) -> float:
    """cot of an angle in degrees; cot(90) is exactly 0."""
    return 0.0 if degrees == 90 else 1 / math.tan(math.radians(degrees))


def _edge_distance(point: tuple[float, float], toward: tuple[float, float],
                   size: tuple[float, float]) -> float:
    """How far `point` (mat space, mm) is from the opening's edge along `toward`: the nearer of the
    two edges the direction heads to; never below 0."""
    reach = math.inf
    for position, direction, extent in zip(point, toward, size, strict=True):
        if direction > 0:
            reach = min(reach, (extent - position) / direction)
        elif direction < 0:
            reach = min(reach, position / -direction)
    return max(reach, 0.0)


def lut_sampled(table: bytes, encoded: Linear) -> Linear:
    """Trilinear at texel centres, as GL samples a GL_LINEAR 3D texture: position p = e*(LUT_GRID-1) per axis,
    blend the 8 grid points around it (contracts/lut.py lut_offset), each byte / 255. Pure.

    `encoded` is screen-encoded (sRGB), each channel clamped to 0..1, as is the result."""
    lower, fraction = [], []
    for value in encoded:
        position = min(1.0, max(0.0, value)) * (LUT_GRID - 1)
        index = min(int(position), LUT_GRID - 2)
        lower.append(index)
        fraction.append(position - index)
    result = [0.0, 0.0, 0.0]
    for dr, dg, db in itertools.product((0, 1), repeat=3):
        weight = ((fraction[0] if dr else 1 - fraction[0]) * (fraction[1] if dg else 1 - fraction[1])
                  * (fraction[2] if db else 1 - fraction[2]))
        offset = lut_offset(lower[0] + dr, lower[1] + dg, lower[2] + db)
        for channel in range(3):
            result[channel] += weight * table[offset + channel] / 255
    return tuple(result)


def lut_applied(linear: Linear, table: bytes) -> Linear:
    """The paper profile's LUT, fully: encode for the screen, look the colour up (`lut_sampled`),
    decode back to linear light. Pure."""
    return tuple(decode_channel(value) for value in lut_sampled(table, tuple(encode_srgb(value) for value in linear)))


# -- Display tuning's test patterns (decision 0024): what a Frame shows while its Display is tuned --

GREY_RAMP_STEPS: Final = 11       # 0 %, 10 %, ..., 100 % of the signal
BLACK_LEVEL_STEPS: Final = 20     # raw steps 1..20 beside black 0
BLACK_LEVEL_PATCH: Final = 0.6    # the lit share of a Black level cell, in u and in the band's v, centred


def pattern_signal(pattern: TestPattern, u: float, v: float) -> Linear:
    """The pattern's ENCODED signal (0..1 per channel, equal channels) at the Frame's picture point
    (u, v), 0..1 across and down the picture as it hangs, so the pattern hangs with the Frame
    whatever its screen's turn; u and v are clamped to 0..1.
    WHITE: 1. GREY_RAMP: v < 0.5, GREY_RAMP_STEPS equal steps across u, step i = i /
    (GREY_RAMP_STEPS - 1); v >= 0.5, a smooth ramp, u. BLACK_LEVEL: 0, except in the middle third
    (1/3 <= v < 2/3) where u splits into BLACK_LEVEL_STEPS equal cells and the middle
    BLACK_LEVEL_PATCH (in u and in that band's v) of cell k (k = 1..20 from u = 0) is k /
    SIGNAL_LEVELS. Pure.

    Named pattern_signal, never test_*: a test module importing a test_* name would be collected."""
    u, v = min(1.0, max(0.0, u)), min(1.0, max(0.0, v))
    if pattern is TestPattern.WHITE:
        value = 1.0
    elif pattern is TestPattern.GREY_RAMP:
        if v < .5:
            value = min(int(u * GREY_RAMP_STEPS), GREY_RAMP_STEPS - 1) / (GREY_RAMP_STEPS - 1)
        else:
            value = u
    elif pattern is TestPattern.BLACK_LEVEL:
        value = 0.0
        band = (v - 1/3) * 3
        if 0 <= band < 1:
            position = u * BLACK_LEVEL_STEPS
            cell = min(int(position), BLACK_LEVEL_STEPS - 1)
            margin = (1 - BLACK_LEVEL_PATCH) / 2
            if margin <= position - cell < 1 - margin and margin <= band < 1 - margin:
                value = (cell + 1) / SIGNAL_LEVELS
    else:
        raise ValueError("pattern")     # a calibration target is `board_signal`'s
    return (value, value, value)


@dataclass(frozen=True)
class BoardDraw:
    """What one Frame needs to draw a calibration target v2 (decision 0027). `aspect`: its
    picture's long side over its short side in pixels (>= 1; `picture_board`); `portrait`: the
    picture is taller than wide, so the board is turned a quarter clockwise (its top at the
    picture's right); `modules`: the QR naming the port's picture change
    (player/calibration_target.py `target_modules`), None while there is no code (no player id
    yet, or a post-software look not on screen): every QR square then draws light."""
    aspect: float
    portrait: bool
    modules: QrModules | None

    @property
    def columns(self) -> int:
        """The board's columns shown: contracts.calibration_target.screen_columns(aspect)."""
        return screen_columns(self.aspect)


def board_point(u: float, v: float, draw: BoardDraw) -> tuple[float, float]:
    """The screen board's point (column, row), in squares, at the picture's point (u, v).
    With n = draw.aspect * SCREEN_ROWS squares along the long side and the board's draw.columns
    centred on it (offset (n - columns) / 2): landscape, (u * n - offset, v * SCREEN_ROWS);
    portrait, turned a quarter clockwise, (v * n - offset, (1 - u) * SCREEN_ROWS). Points outside
    [0, columns) x [0, SCREEN_ROWS) are the surround. Pure."""
    along = draw.aspect * SCREEN_ROWS
    offset = (along - draw.columns) / 2
    if draw.portrait:
        return (v * along - offset, (1 - u) * SCREEN_ROWS)
    return (u * along - offset, v * SCREEN_ROWS)


def board_signal(column: float, row: float, draw: BoardDraw) -> float:
    """The screen board's ENCODED signal (0..1, one value for every channel) at (column, row) in
    squares, first match: 0.0 outside [0, draw.columns) x [0, SCREEN_ROWS); inside the QR squares
    of a copy of qr_copies(draw.columns), the module there (dark 0.0; light and the
    QR_QUIET_MODULES quiet zone 1.0; all 1.0 when draw.modules is None), the modules and their
    quiet zone scaled to fill QR_SQUARES; else board_raster(SCREEN)'s pixel at
    (floor(column * SQUARE_PX), floor(row * SQUARE_PX)) over 255. Pure; player/pattern_pass.py's
    `board_signal` mirrors it (tests/native_fit_pass_harness.py)."""
    if not (0 <= column < draw.columns and 0 <= row < SCREEN_ROWS):
        return 0.0
    for copy in qr_copies(draw.columns):
        if copy.column <= column < copy.column + QR_SQUARES and copy.row <= row < copy.row + QR_SQUARES:
            if draw.modules is None:
                return 1.0
            side = len(draw.modules) + 2 * QR_QUIET_MODULES
            module_column = min(side - 1, int((column - copy.column) / QR_SQUARES * side)) - QR_QUIET_MODULES
            module_row = min(side - 1, int((row - copy.row) / QR_SQUARES * side)) - QR_QUIET_MODULES
            inside = 0 <= module_row < len(draw.modules) and 0 <= module_column < len(draw.modules)
            return 0.0 if inside and draw.modules[module_row][module_column] else 1.0
    raster = board_raster(Surface.SCREEN)
    return raster.pixels[math.floor(row * SQUARE_PX) * raster.width + math.floor(column * SQUARE_PX)] / 255


def final_pixel(photo: Linear, operations: PictureOperations, *, inside: bool, lights: FrameLights | None,
                x: float, y: float, lut: bytes | None = None, opening_uv: tuple[float, float] | None = None,
                board: BoardDraw | None = None,
                dither_at: tuple[int, int] | None = None) -> tuple[float, float, float, float]:
    """One pixel's final pass, at the Frame's picture point (x, y) (0..1 across and down the
    picture as it hangs) of a Frame lit by `lights` (`picture_lights`; required when `inside`:
    ValueError("lights")):
    the paper profile's LUT (`lut_applied`, the bytes of the Frame's `lighting.paper.profile`,
    fully, when held) -> `paper_reflectance` -> `irradiance` -> `paper_signal` (the Display's light:
    operations.light) -> `highlight_rolled` at
    lights.peak -> x rgb_multipliers -> sRGB encode -> contrast about 0.5 -> gamma exponent -> clamp
    -> `dithered` at `dither_at` (an output pixel; None: no dither, for an oracle) -> the mask
    LAST: (0, 0, 0, 1) when not `inside` the Frame.

    Test patterns: WHITE and GREY_RAMP (`pattern_signal` at `opening_uv`, the picture's point)
    and TARGET_POST_SOFTWARE (s = board_signal(*board_point(*opening_uv, board), board)) replace
    the photo's signal with srgb_to_linear of theirs and go through white balance, contrast, gamma,
    the clamp and the dither only, never the room's light, the paper or the mat. BLACK_LEVEL and
    TARGET_PRE_SOFTWARE are the raw signal, through no step. A pattern without `opening_uv` is
    ValueError("opening_uv"); a calibration target without `board` ValueError("board").

    A contrast result below 0 meets the exponent as 0 (a power of a negative number is undefined,
    in GLSL too); with the clamp after, this equals clamping before the exponent. Pure."""
    if inside and lights is None:
        raise ValueError("lights")
    pattern = operations.test_pattern
    if pattern in TARGET_PATTERNS:
        if opening_uv is None:
            raise ValueError("opening_uv")
        if board is None:
            raise ValueError("board")
        value = board_signal(*board_point(*opening_uv, board), board)
        if pattern is TestPattern.TARGET_PRE_SOFTWARE:
            return (value, value, value, 1.0) if inside else (0.0, 0.0, 0.0, 1.0)
        signal = (srgb_to_linear(value),) * 3
    elif pattern is not None:
        if opening_uv is None:
            raise ValueError("opening_uv")
        encoded = pattern_signal(pattern, *opening_uv)
        if pattern is TestPattern.BLACK_LEVEL:
            return (*encoded, 1.0) if inside else (0.0, 0.0, 0.0, 1.0)
        signal = tuple(srgb_to_linear(value) for value in encoded)
    elif lights is None:
        return (0.0, 0.0, 0.0, 1.0)     # not inside (checked above): the mask's black
    else:
        if lut is not None:
            photo = lut_applied(photo, lut)
        reflectance = paper_reflectance(photo, lights.paper)
        signal = highlight_rolled(paper_signal(reflectance, irradiance(lights, x, y), operations.light), lights.peak)
    pixel = display_steps(signal, operations)
    if dither_at is not None:
        pixel = tuple(min(1.0, max(0.0, dithered(value, *dither_at))) for value in pixel)
    if not inside:
        return (0.0, 0.0, 0.0, 1.0)
    return (*pixel, 1.0)


def display_steps(signal: Linear, operations: PictureOperations) -> Linear:
    """The Display's own steps on a linear signal (a share of the TV's white): x rgb_multipliers,
    sRGB encode, contrast about 0.5, gamma exponent, clamp; the encoded result. The one home of the
    tail every pixel and every look pattern shares (`final_pixel`); the calibration reader predicts
    a post-software target with it. Pure."""
    pixel = []
    for value, multiplier in zip(signal, operations.rgb_multipliers, strict=True):
        value = (encode_srgb(value * multiplier) - .5) * operations.contrast_slope + .5
        pixel.append(min(1.0, max(0.0, value) ** operations.gamma_exponent))
    return tuple(pixel)


# ---- Room light: one physics path, the scalar reference player/look_shader.py mirrors ----

HIGHLIGHT_KNEE: Final = 0.8         # the fixed rolloff's knee, in signal (a share of the TV's white)
NO_LIGHT: Final[Linear] = (0.0, 0.0, 0.0)
# The 4 x 4 ordered dither (Bayer): the threshold rank at row y % 4, column x % 4. The shader's copy
# is written in from here (player/look_shader.py).
BAYER_4: Final = (0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5)
# The shader's light kind codes (player/look_shader.py writes them in).
LIGHT_KINDS: Final = ("wash", "far", "close")
LightKind = Literal["wash", "far", "close"]


@dataclass(frozen=True)
class LitLight:
    """One Frame light as the final pass draws it (the shader's uniform arrays), in mat space: the
    picture's own rectangle at its physical size, mm, x along its u, y along its v (down the wall)."""
    kind: LightKind
    rgb: Linear                                 # `light_on_frame`: rgb lux on the Frame (at its centre, close)
    toward: tuple[float, float] = (0.0, 0.0)    # far: unit vector toward the light (mat axes)
    band_mm: tuple[float, float] = (0.0, 0.0)   # far: (dark until, lit from), the mat shadow's band
    position_mm: tuple[float, float, float] = (0.0, 0.0, 1.0)   # close: (x, y) in mat space, out from the wall
    beam: tuple[float, float] = (0.0, 0.0)      # close: half the inner and outer beam angles, radians


# The mat space of a Frame whose physical size is not known (FrameLights.mat_size): a unit square,
# where only wash lights are drawn, so no point of it depends on a size.
UNSIZED_MAT: Final = (1.0, 1.0)


@dataclass(frozen=True)
class FrameLights:
    """One Frame's lights resolved against one room-lights document, as the final pass uses them
    (the shader's uniforms): the picture's physical `size_mm` (`picture_lights`; None when the
    Display states no size, and then every light in `lights` is a wash: the others need
    millimetres), the mat's `depth_mm`, the `paper`, each light that adds light (`lights`, at most
    FRAME_LIGHTS_MAX; one adding none is left out) and the Frame's `peak`: its paper white's
    brightest signal over the opening (`_brightest_white`), the same for every pixel of the Frame,
    so the rolloff keeps the cone's shape. ValueError("frame_lights") when `size_mm` is None and a
    light is not a wash."""
    size_mm: tuple[float, float] | None
    depth_mm: float
    paper: Paper
    lights: tuple[LitLight, ...]
    peak: float

    def __post_init__(self) -> None:
        """See the class docstring."""
        if self.size_mm is None and any(lit.kind != "wash" for lit in self.lights):
            raise ValueError("frame_lights")

    @property
    def mat_size(self) -> tuple[float, float]:
        """The mat space's extent the physics and the shader's `mat_size` work in: `size_mm`, or
        UNSIZED_MAT when no size is known (only washes then, which are the same everywhere)."""
        return UNSIZED_MAT if self.size_mm is None else self.size_mm


def light_on_frame(light: WashLight | FarLight | CloseLight, entry: RoomLightEntry | None) -> Linear:
    """The rgb lux `light` puts on the Frame (at its centre, for a close light), its shape aside.
    No light (0, 0, 0) for an absent entry, NotReported, Unavailable, an off dimmer, or a reference
    that does not fit the entry (AtFull on a measured light, AtReading on any other): a total rule.
    A fixed light: lux, coloured by its kelvin at luminance 1 (`light_colour`). A dimmer on: lux x
    brightness, coloured the same way. A colour dimmer on: drive_c = rgb_c x brightness and
    E_c = lux x drive_c x share_c / LUMA_c, so luminance is lux x sum_c drive_c x share_c. A
    measured light: lux x reading / the reference's reading, coloured as a fixed light. Pure."""
    if entry is None:
        return NO_LIGHT
    reference = light.reference
    if isinstance(entry, MeasuredEntry):
        if not (isinstance(entry.state, Measured) and isinstance(reference, AtReading)):
            return NO_LIGHT
        return _white(light.lux * entry.state.lux / reference.lux, entry.colour.kelvin)
    if not isinstance(reference, AtFull):
        return NO_LIGHT
    if isinstance(entry, FixedEntry):
        return _white(light.lux, entry.colour.kelvin)
    if isinstance(entry, WhiteEntry):
        state = entry.state
        return _white(light.lux * state.brightness, entry.colour.kelvin) if isinstance(state, Dimmer) and state.on \
            else NO_LIGHT
    assert isinstance(entry, ColourEntry)
    state = entry.state
    if not (isinstance(state, ColourDimmer) and state.on):
        return NO_LIGHT
    shares = entry.colour.channel_shares
    return tuple(light.lux * state.rgb[index] * state.brightness * shares[index] / LUMA[index] for index in range(3))


def light_lux_on_frame(lighting: FrameLighting, document: RoomLightsDocument | None) -> float | None:
    """The light on the Frame at its centre, lux: the luminance (LUMA weights) of the sum of
    `light_on_frame` over `lighting`'s lights against `document` (a close light at the centre, the
    mat's shadow aside). None for no document yet (nothing drawn). What the Player reports as a
    Frame's `light_lux` (contracts/node_state.py `FrameReport`). Pure."""
    if document is None:
        return None
    total = 0.0
    for light in lighting.lights:
        rgb = light_on_frame(light, document.lights.get(light.light))
        total += sum(weight * channel for weight, channel in zip(LUMA, rgb, strict=True))
    return total


def _white(lux: float, kelvin: int) -> Linear:
    """`lux` of a white light of `kelvin`, at luminance 1 per lux."""
    return tuple(lux * channel for channel in light_colour(kelvin))


def _lit(light: WashLight | FarLight | CloseLight, rgb: Linear, depth: float, size: tuple[float, float]) -> LitLight:
    """`light` in mat space: a far light's `toward` = (sin a, -cos a), a = from_degrees, its band
    (depth cot(min(90, steep + softness/2)), depth cot(max(LIGHT_ELEVATION_MIN_DEGREES, steep -
    softness/2))); a close light's place from the picture's centre, the wall's up mat space's -y."""
    if isinstance(light, WashLight):
        return LitLight("wash", rgb)
    if isinstance(light, FarLight):
        turn = math.radians(light.from_degrees)
        half = light.softness_degrees / 2
        return LitLight("far", rgb, toward=(math.sin(turn), -math.cos(turn)), band_mm=(
            depth * _cot(min(90, light.steep_degrees + half)),
            depth * _cot(max(LIGHT_ELEVATION_MIN_DEGREES, light.steep_degrees - half))))
    return LitLight("close", rgb, position_mm=(size[0] / 2 + light.across_mm, size[1] / 2 - light.up_mm, light.out_mm),
                    beam=(math.radians(light.inner_beam_degrees) / 2, math.radians(light.outer_beam_degrees) / 2))


PEAK_GRID: Final = 33     # the peak's samples a side of the opening (`_brightest_white`)


def _brightest_white(lights: tuple[LitLight, ...], size: tuple[float, float], depth: float,
                     paper: Paper, display: DisplayLight) -> float:
    """The largest channel of the paper white's `paper_signal` over the opening: `_irradiance_at`
    (every light with its shape, its cone and the mat's shadow) on a PEAK_GRID x PEAK_GRID lattice of
    the opening's unit square, edges included, and at each close light's foot on the wall when that
    lies inside the opening. A wash alone is exact (the light is the same everywhere); between
    samples a brighter point may lie, and `rolled_off` takes it to the TV's white. Pure."""
    points = [(size[0] * i / (PEAK_GRID - 1), size[1] * j / (PEAK_GRID - 1))
              for i in range(PEAK_GRID) for j in range(PEAK_GRID)]
    points += [(lit.position_mm[0], lit.position_mm[1]) for lit in lights if lit.kind == "close"
               and 0 <= lit.position_mm[0] <= size[0] and 0 <= lit.position_mm[1] <= size[1]]
    white = (paper.whiteness,) * 3
    return max((max(paper_signal(white, _irradiance_at(lights, point, size, depth), display)) for point in points),
               default=0.0)


def _smoothstep(t: float) -> float:
    return t * t * (3 - 2 * t)


def _reached(lit: LitLight, point: tuple[float, float], size: tuple[float, float], depth: float) -> float:
    """The share of `lit` the mat lets reach `point` (mat space): wash 1; far, the band rule (s the
    distance along `toward` to the opening's edge: 1 from lit-from, 0 nearer than dark-until,
    smoothstep between, a hard step when they are equal); close, a point light's shadow: along the
    wall toward the light's foot, s the distance to the opening's edge, lit when s >= depth x h /
    out (h the distance along the wall to the light's foot; h == 0 is always lit)."""
    if lit.kind == "wash":
        return 1.0
    if lit.kind == "far":
        s = _edge_distance(point, lit.toward, size)
        dark, full = lit.band_mm
        if s >= full:
            return 1.0
        if s < dark:
            return 0.0
        return _smoothstep((s - dark) / (full - dark))
    lx, ly, out = lit.position_mm
    h = math.hypot(lx - point[0], ly - point[1])
    if h == 0:
        return 1.0
    s = _edge_distance(point, ((lx - point[0]) / h, (ly - point[1]) / h), size)
    return 1.0 if s >= depth * h / out else 0.0


def _close_shape(lit: LitLight, point: tuple[float, float], size: tuple[float, float]) -> float:
    """A close light's shape at `point` (mat space), relative to the Frame's centre: the inverse
    square with the angle onto the wall, (d0 / d)**3, times the cone: phi the angle between the
    light's aim (the Frame's centre) and the point, 1 within half the inner beam, 0 from half the
    outer beam, smoothstep of (outer - phi) / (outer - inner) between."""
    lx, ly, out = lit.position_mm
    aim = (size[0] / 2 - lx, size[1] / 2 - ly, -out)
    ray = (point[0] - lx, point[1] - ly, -out)
    d0, d = math.hypot(*aim), math.hypot(*ray)
    phi = math.acos(min(1.0, max(-1.0, sum(a * b for a, b in zip(aim, ray, strict=True)) / (d0 * d))))
    inner, outer = lit.beam
    if phi <= inner:
        cone = 1.0
    elif phi >= outer:
        cone = 0.0
    else:
        cone = _smoothstep((outer - phi) / (outer - inner))
    return (d0 / d) ** 3 * cone


def irradiance(lights: FrameLights, x: float, y: float) -> Linear:
    """E at the picture's point (x, y) (0..1 across and down the picture as it hangs), rgb lux: the
    sum over the Frame's lights of light_on_frame x shape x reaches, at m = (x, y) x size_mm. Shape:
    1 for wash and far; for close, `_close_shape`. Reaches: `_reached` (the mat shadow). No floor:
    the brightness follows the room down. Pure."""
    size = lights.mat_size
    return _irradiance_at(lights.lights, (x * size[0], y * size[1]), size, lights.depth_mm)


def _irradiance_at(lights: tuple[LitLight, ...], point: tuple[float, float], size: tuple[float, float],
                   depth: float) -> Linear:
    """`irradiance` at a mat-space `point` (mm). Pure."""
    total = [0.0, 0.0, 0.0]
    for lit in lights:
        shape = _close_shape(lit, point, size) if lit.kind == "close" else 1.0
        if shape == 0:
            continue
        share = shape * _reached(lit, point, size, depth)
        for channel in range(3):
            total[channel] += lit.rgb[channel] * share
    return tuple(total)


def paper_reflectance(photo: Linear, paper: Paper) -> Linear:
    """rho = whiteness x (10^-ink + (1 - 10^-ink) x photo_c), the photo already through the paper
    profile's LUT when it has one (the only source of saturation). Pure."""
    black = 10 ** -paper.ink_density
    return tuple(paper.whiteness * (black + (1 - black) * value) for value in photo)


def paper_signal(reflectance: Linear, light: Linear, display: DisplayLight) -> Linear:
    """The panel's linear signal for paper of `reflectance` under `light` (rgb lux): (rho_c x E_c /
    (SHEET_WHITENESS x matched_lux) - black) / (1 - black), at least 0 (below the panel's glow it
    shows the glow). May pass 1: `highlight_rolled` brings it back. Pure."""
    white = SHEET_WHITENESS * display.matched_lux
    return tuple(max(0.0, (rho * lux / white - display.black) / (1 - display.black))
                 for rho, lux in zip(reflectance, light, strict=True))


def highlight_rolled(signal: Linear, peak: float) -> Linear:
    """The one fixed rolloff above the TV's white, keeping each pixel's hue: identity when
    peak <= 1; otherwise a pixel whose largest channel m is at or below HIGHLIGHT_KNEE is unchanged,
    and one above it is scaled as a whole by rolled_off(m, HIGHLIGHT_KNEE, peak) / m, so
    [HIGHLIGHT_KNEE, peak] lands on [HIGHLIGHT_KNEE, 1] and the channels keep their ratios (a print
    under one light has one colour at every tone; rolling each channel apart would turn a warm
    light's highlights yellow-white). Monotone and continuous in signal and peak. Pure."""
    largest = max(signal)
    if peak <= 1 or largest <= HIGHLIGHT_KNEE:
        return signal
    scale = rolled_off(largest, HIGHLIGHT_KNEE, peak) / largest
    return tuple(value * scale for value in signal)


def dithered(encoded: float, x: int, y: int) -> float:
    """`encoded` (0..1, screen-encoded) plus an ordered dither of at most half an 8-bit step at
    output pixel (x, y), before quantisation (required: the room may dim the picture to a few dozen
    steps): ((BAYER_4[(y % 4) * 4 + x % 4] + 0.5) / 16 - 0.5) / 255. Its mean over any 4 x 4 tile is
    `encoded`; the shader uses the same pattern. Pure."""
    return encoded + ((BAYER_4[(y % 4) * 4 + x % 4] + .5) / 16 - .5) / 255


# The fit pass's scalar reference (render redesign, run ledger .claude/runs/render-planes/s1-page.md):
# one picture finished at its finish size (player/planes.py), as it hangs and turned for its screen, the look
# applied once (player/fit_pass.py holds its shader to these within one level).

def buffer_to_picture(x: float, y: float, size: tuple[int, int], turn: DisplayTurn) -> tuple[float, float]:
    """A point (x, y) of a fit pass target laid out `size` = (width, height) pixels (pixel centres at
    +0.5, y down) as the picture's own (u, v), each 0..1 across the picture as it hangs, upright: the
    target is the picture turned for a screen hung `turn` degrees clockwise (contracts/display_turn.py),
    so the screen's matching buffer transform shows it upright. ValueError("display_turn") for any
    other turn. Pure.

    The target holds the picture turned `turn` degrees counter-clockwise (a screen hung turned 90
    degrees clockwise has its top edge on the viewer's right), so this undoes a clockwise quarter
    turn of 360 - `turn` (`UNDO_QUARTER_TURN`) on the target's fractions (x / width, y / height)."""
    width, height = size
    back = TURNS[-wayland_transform(turn) % len(TURNS)]
    return transform(UNDO_QUARTER_TURN[back], x / width, y / height)


def fit_source_uv(u: float, v: float, aspect: float, source: tuple[int, int],
                  crop: PhotoCrop | None) -> tuple[float, float]:
    """The picture point (u, v) as the source's texture coordinates (0..1, origin top-left): the
    window `crop_window(crop or CENTRED, *source, aspect)` (contracts/photo_crop.py) stretched over the
    picture. Pure."""
    left, top, right, bottom = photo_rect(aspect, *source, crop or CENTRED)
    return left + u*(right-left), top + v*(bottom-top)


def picture_lights(lighting: FrameLighting, document: RoomLightsDocument | None,
                   size_mm: tuple[float, float] | None, display: DisplayLight) -> FrameLights:
    """`lighting`'s lights against `document` (None: none received yet, so every light adds none), in
    the picture's mat space at its physical size `size_mm` (contracts/models.py `frame_size_mm` of
    the drawn box, the binding's `display_mm` and the screen's turn; a screen's turn turns the
    screen, not the wall), each light that adds light (`_lit`), with the Frame's peak: the paper
    white's brightest signal over the picture (`_brightest_white`). With `size_mm` None (the
    Display states no size) only wash lights add light: far and close lights and the mat's shadow
    need millimetres. Pure."""
    mat = UNSIZED_MAT if size_mm is None else size_mm
    lights = []
    for light in lighting.lights:
        if size_mm is None and not isinstance(light, WashLight):
            continue
        entry = None if document is None else document.lights.get(light.light)
        rgb = light_on_frame(light, entry)
        if max(rgb) > 0:
            lights.append(_lit(light, rgb, lighting.mat_depth_mm, mat))
    lit = tuple(lights)
    return FrameLights(size_mm, lighting.mat_depth_mm, lighting.paper, lit,
                       _brightest_white(lit, mat, lighting.mat_depth_mm, lighting.paper, display))


# The pattern pass's scalar reference (render redesign S3, run ledger .claude/runs/render-planes/
# s3-page.md, bead B2): a Frame's pattern drawn into its screen's ground, in the Frame's picture as it
# hangs (player/pattern_pass.py holds its shader to these within one level).

def placement_uv(x: float, y: float, placement: Placement, screen: tuple[int, int]) -> tuple[float, float] | None:
    """The screen point (x, y), fractions 0..1 of a screen `screen` = (width, height) pixels as it
    hangs, as the Frame's picture point (u, v) when it lies in `placement` (left and top edges in,
    right and bottom out): u = (x * width - placement.x) / placement.width, v likewise down; None
    outside. Pure."""
    width, height = screen
    u = (x * width - placement.x) / placement.width
    v = (y * height - placement.y) / placement.height
    return (u, v) if 0 <= u < 1 and 0 <= v < 1 else None


GRID_SLATE: Final[Linear] = (0.2, 0.3, 0.45)     # the commissioning grid's ground, linear light
GRID_LINE: Final[Linear] = (0.9, 0.9, 0.9)       # its lines
GRID_LINES: Final = 10                           # lines across the picture, each way
GRID_LINE_WIDTH: Final = 0.035                   # a line's share of the space between two lines


def commissioning_grid(u: float, v: float) -> Linear:
    """Position's commissioning grid at the Frame's picture point (u, v), in linear light: slate
    GRID_SLATE (0.2, 0.3, 0.45) with light lines GRID_LINE (0.9, 0.9, 0.9) where fract(GRID_LINES u)
    or fract(GRID_LINES v) < GRID_LINE_WIDTH (10 and 0.035, in the picture's unit square). The photo a commissioning ground passes through
    `final_pixel`. Pure; player/pattern_pass.py's shader is written from these constants."""
    def fract(value: float) -> float:
        return value - math.floor(value)
    if fract(u * GRID_LINES) < GRID_LINE_WIDTH or fract(v * GRID_LINES) < GRID_LINE_WIDTH:
        return GRID_LINE
    return GRID_SLATE


def picture_board(placement: Placement, modules: QrModules | None) -> BoardDraw:
    """The BoardDraw of a Frame's picture placed at `placement` (its pixels as it hangs): aspect
    max(w, h) / min(w, h), portrait when w < h (frame box run decision 11: the board's squares are
    the screen's pixels, so a mode with non-square pixels draws them non-square). Pure."""
    width, height = placement.width, placement.height
    return BoardDraw(max(width, height) / min(width, height), width < height, modules)
