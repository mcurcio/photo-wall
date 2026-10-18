"""Scalar calibration/SDR reference. Production pixels are processed by shaders.

The Frame fit and the final pass are each stated once here and mirrored by player/native.py's shaders:
- **One matrix per fit** (`output_to_picture`): output fractions -> the picture's unit square.
  Corners: the inverse perspective onto the corners with the quarter turn folded in. Cover: the
  inverse of rotate, scale evenly and move onto contracts/frame_fit.py's cover rectangle (the
  Frame profile's aspect), worked in output pixels so a non-square output never skews it.
- **Per-Frame mask** (`inside_opening`): a Frame draws nothing outside its own opening (outside the
  Frame is someone else's territory), in the position pass.
- **Final pass** (`final_pixel`): contracts/node_frame.py PictureOperations in their stated
  order, then the per-Output mask LAST, so black stays black outside every opening whatever
  the picture settings.
- **Photo fit** (`photo_rect`): each photo's source rectangle, the window `Layer.crop` names, cut
  from the Frame profile's aspect (contracts/photo_crop.py, the one home of the window; no trim). It is the one crop in either fit: in Cover the drawn rectangle has that same aspect,
  so the photo is never cropped twice or squeezed.

Room light is one physics path in real units, every Frame always through it: the Frame's paper (`paper_reflectance`, its profile's LUT first) under E(x), the room's light
summed over the Frame's lights (`frame_lights`, `irradiance`: a wash, a far light's mat shadow, a
close light's inverse square, cone and shadow), as a signal fitted between the panel's black and
its white at the Display's matched lux (`paper_signal`), the one fixed highlight rolloff
(`highlight_rolled`), then the Display's white balance, contrast and gamma, the dither
(`dithered`) and the per-Output mask (`final_pixel`).

The test patterns (`pattern_signal`): a look pattern (White, Grey ramp) replaces the photos inside the opening and goes through white
balance, contrast and gamma only, never the room's light, the paper or the mat (White is the TV's
own white for the paper match); the Black level is the raw signal, through no step; the per-Output
mask stays last.

The calibration targets v2 (decision 0027) draw the screen board (contracts/calibration_target.py
"v2", OpenCV's ChArUco board drawn at build time as `board_raster`): SCREEN_ROWS squares across the
opening's short side, as many columns as fit (`screen_columns`), centred along its long side,
turned a quarter clockwise in a portrait opening (`board_draw`, `board_point`); each point the
raster's pixel, or a QR copy's module (`board_signal`; player/calibration_target.py
`target_modules`). The opening's size has one home, `opening_size_mm`, which the physics uses
too. Pre-software is the board's signal raw everywhere in the opening; post-software runs it
through the Display's white balance, contrast and gamma, as the look patterns.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable, Sequence
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
from contracts.frame_fit import Corners, OutputSize, cover_rectangle
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
from contracts.models import CornersFit, CoverFit, FrameProfile, PhotoCrop
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


def _solve(rows: list[list[float]]) -> tuple[float, ...]:
    for column in range(len(rows)):
        pivot = max(range(column, len(rows)), key=lambda i: abs(rows[i][column]))
        rows[column], rows[pivot] = rows[pivot], rows[column]
        scale = rows[column][column]
        if abs(scale) < 1e-12:
            raise ValueError("singular homography")
        rows[column] = [v / scale for v in rows[column]]
        for index, row in enumerate(rows):
            if index != column:
                factor = row[column]
                rows[index] = [a - factor * b for a, b in zip(row, rows[column], strict=True)]
    return tuple(row[-1] for row in rows)


def homography(corners: Sequence[tuple[float, float]]) -> Matrix:
    """Unit square -> TL, TR, BR, BL, both in top-left screen coordinates."""
    if len(corners) != 4 or not all(math.isfinite(v) for p in corners for v in p):
        raise ValueError("four finite corners required")
    rows = []
    for (x, y), (u, v) in zip(((0, 0), (1, 0), (1, 1), (0, 1)), corners, strict=True):
        rows.extend(([x, y, 1, 0, 0, 0, -u*x, -u*y, u],
                     [0, 0, 0, x, y, 1, -v*x, -v*y, v]))
    return (*_solve(rows), 1.0)


def inverse(matrix: Matrix) -> Matrix:
    a, b, c, d, e, f, g, h, i = matrix
    cofactors = (e*i-f*h, c*h-b*i, b*f-c*e,
                 f*g-d*i, a*i-c*g, c*d-a*f,
                 d*h-e*g, b*g-a*h, a*e-b*d)
    determinant = a*cofactors[0] + b*cofactors[3] + c*cofactors[6]
    if not math.isfinite(determinant) or abs(determinant) < 1e-12:
        raise ValueError("singular homography")
    return tuple(v / determinant for v in cofactors)


def transform(matrix: Matrix, x: float, y: float) -> tuple[float, float]:
    divisor = matrix[6]*x + matrix[7]*y + matrix[8]
    if not math.isfinite(divisor) or abs(divisor) < 1e-12:
        raise ValueError("point maps to infinity")
    return ((matrix[0]*x + matrix[1]*y + matrix[2]) / divisor,
            (matrix[3]*x + matrix[4]*y + matrix[5]) / divisor)


# Undo a clockwise quarter turn of the picture, as a matrix on the unit square: the one home of
# the quarter-turn rule (Corners folds it into its matrix).
UNDO_QUARTER_TURN: dict[int, Matrix] = {
    0: (1, 0, 0, 0, 1, 0, 0, 0, 1),
    90: (0, 1, 0, -1, 0, 1, 0, 0, 1),         # (u, v) -> (v, 1-u)
    180: (-1, 0, 1, 0, -1, 1, 0, 0, 1),       # (u, v) -> (1-u, 1-v)
    270: (0, -1, 1, 1, 0, 0, 0, 0, 1),        # (u, v) -> (1-v, u)
}


def multiply(left: Matrix, right: Matrix) -> Matrix:
    """The 3x3 product left x right (row-major): apply `right`, then `left`."""
    return tuple(sum(left[3*row + k]*right[3*k + column] for k in range(3))
                 for row in range(3) for column in range(3))


def decode_channel(value: float, video: bool = False) -> float:
    if not video:
        return srgb_to_linear(value)
    value = min(1.0, max(0.0, value))
    return value / 4.5 if value < .081 else ((value + .099) / 1.099) ** (1/.45)


def encode_srgb(value: float) -> float:
    value = min(1.0, max(0.0, value))
    return 12.92*value if value <= .0031308 else 1.055*value**(1/2.4)-.055


def compose_pixel(samples: Iterable[tuple[tuple[float, float, float, float], float, bool]]) -> tuple[float, float, float, float]:
    """Bottom-to-top straight encoded RGBA/effective alpha/video -> opaque sRGB, with neutral
    picture operations (`final_pixel` is the final pass).

    Empty output is opaque black. Source alpha and Layer alpha multiply in linear
    premultiplied source-over.
    """
    rgb = [0.0, 0.0, 0.0]
    for rgba, opacity, video in samples:
        if not all(math.isfinite(v) and 0 <= v <= 1 for v in (*rgba, opacity)):
            raise ValueError("finite normalized samples required")
        alpha = rgba[3]*opacity
        rgb = [decode_channel(rgba[i], video)*alpha + rgb[i]*(1-alpha) for i in range(3)]
    return (*(encode_srgb(v) for v in rgb), 1.0)


def _cover_matrix(fit: CoverFit, output: OutputSize, aspect: float) -> Matrix:
    """Output fractions -> picture unit square for Cover: the inverse of "rotate `rotation`
    clockwise, scale to the cover rectangle's width and height, move to its centre", all in
    output pixels. With the picture axes u = (cos r, sin r), v = (-sin r, cos r) (y down) and the
    pixel point p = (x W, y H): u = (p - centre).u_axis / width + 1/2, likewise v with height."""
    rectangle = cover_rectangle(fit.corners, fit.rotation, output, aspect)
    width_px, height_px = output
    turn = math.radians(rectangle.rotation)
    cos, sin = math.cos(turn), math.sin(turn)
    cx, cy = rectangle.centre_x, rectangle.centre_y
    w, h = rectangle.width, rectangle.height
    return (cos*width_px/w, sin*height_px/w, .5 - (cx*cos + cy*sin)/w,
            -sin*width_px/h, cos*height_px/h, .5 + (cx*sin - cy*cos)/h,
            0.0, 0.0, 1.0)


def output_to_picture(fit: CornersFit | CoverFit, output: OutputSize, aspect: float) -> Matrix:
    """The one matrix of a fit: output fractions (x, y) -> the picture's unit square (u, v), origin
    top-left, for a Frame whose profile has width-to-height `aspect` (Cover's rectangle has it;
    Corners stretches the picture onto the corners whatever it is). Pure. ValueError("singular
    homography") for a degenerate Corners fit; contracts/frame_fit.py's for a bad Cover one."""
    if isinstance(fit, CoverFit):
        return _cover_matrix(fit, output, aspect)
    return multiply(UNDO_QUARTER_TURN[fit.rotation], inverse(homography(fit.corners)))


def inside_opening(corners: Corners, x: float, y: float) -> bool:
    """Whether output point (x, y) lies in the convex opening (edges included): the per-Frame mask,
    by half-planes. Pure. The corners turn clockwise in y-down coordinates (convex_clockwise), so
    a point is inside when it lies on the inner (non-negative cross product) side of every edge."""
    for index, (ax, ay) in enumerate(corners):
        bx, by = corners[(index + 1) % 4]
        if (bx - ax)*(y - ay) - (by - ay)*(x - ax) < 0:
            return False
    return True


def photo_rect(frame_aspect: float, source_width: int, source_height: int,
               crop: PhotoCrop) -> tuple[float, float, float, float]:
    """The photo's source UV rectangle (left, top, right, bottom): the window `crop` names for
    `frame_aspect` (contracts/photo_crop.py `crop_window`). Pure; ValueError for a non-positive
    or non-finite aspect or size."""
    return crop_window(crop, source_width, source_height, frame_aspect)


def picture_source_uv(x: float, y: float, fit: CornersFit | CoverFit, output: OutputSize, profile_aspect: float,
                      source_width: int, source_height: int,
                      crop: PhotoCrop) -> tuple[float, float] | None:
    """The source UV one Frame samples at output point (x, y); None outside its opening or its
    picture. The per-Frame mask (the opening) comes first, so a Frame never
    draws over its neighbour, then the picture's unit square. The photo is cropped to the Frame
    profile's aspect (`profile_aspect`), in either fit, at the window `crop` names (`photo_rect`)."""
    if not inside_opening(fit.corners, x, y):
        return None
    u, v = transform(output_to_picture(fit, output, profile_aspect), x, y)
    if not (0 <= u <= 1 and 0 <= v <= 1):
        return None
    left, top, right, bottom = photo_rect(profile_aspect, source_width, source_height, crop)
    return left + u*(right-left), top + v*(bottom-top)


# -- The physics' shared rules: luminance, a light's colour, the rolloff's curve, the mat's
# edge and the paper profile's LUT. Each is stated once here and mirrored by player/native.py's final pass.

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
    """The pattern's ENCODED signal (0..1 per channel, equal channels) at (u, v) in the Frame's
    opening: u, v = transform(inverse(homography(fit.corners)), x, y), the opening's own unit square
    (the mat's `to_opening`), so the pattern turns with the Frame; u and v are clamped to 0..1.
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
    opening's physical long side over its short side (>= 1; `opening_size_mm`); `portrait`: the
    opening is taller than wide, so the board is turned a quarter clockwise (its top at the
    opening's right); `modules`: the QR naming the port's picture change
    (player/calibration_target.py `target_modules`), None while there is no code (no player id
    yet, or a post-software look not on screen): every QR square then draws light."""
    aspect: float
    portrait: bool
    modules: QrModules | None

    @property
    def columns(self) -> int:
        """The board's columns shown: contracts.calibration_target.screen_columns(aspect)."""
        return screen_columns(self.aspect)


def board_draw(fit: CornersFit | CoverFit, profile: FrameProfile, modules: QrModules | None) -> BoardDraw:
    """The BoardDraw of a Frame's opening: (W, H) = `opening_size_mm`(fit, profile); aspect
    max(W, H) / min(W, H); portrait when W < H. Pure."""
    width, height = opening_size_mm(fit, profile)
    return BoardDraw(max(width, height) / min(width, height), width < height, modules)


def board_point(u: float, v: float, draw: BoardDraw) -> tuple[float, float]:
    """The screen board's point (column, row), in squares, at the opening's unit-square (u, v).
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
    (floor(column * SQUARE_PX), floor(row * SQUARE_PX)) over 255. Pure; the shader's
    `board_signal` mirrors it (tests/native_renderer_pixel_harness.py)."""
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


def opening_size_mm(fit: CornersFit | CoverFit, profile: FrameProfile) -> tuple[float, float]:
    """The opening's physical extent along its unit square's u and v, in mm: diag_mm =
    diagonal_inches*25.4, a = width_px/height_px, W = diag_mm*a/hypot(a,1), H = diag_mm/hypot(a,1);
    (W, H) when the nearest quarter turn of fit.rotation (round(rotation / 90) * 90) is 0 or 180,
    else (H, W). The one home of this rule: `mat_shadow`'s size_mm is it. Pure."""
    diagonal = profile.diagonal_inches * 25.4
    aspect = profile.width_px / profile.height_px
    width, height = diagonal * aspect / math.hypot(aspect, 1), diagonal / math.hypot(aspect, 1)
    quarter = round(fit.rotation / 90) * 90
    return (width, height) if quarter % 180 == 0 else (height, width)


def final_pixel(photo: Linear, operations: PictureOperations, *, inside: bool, lights: FrameLights | None,
                x: float, y: float, lut: bytes | None = None, opening_uv: tuple[float, float] | None = None,
                board: BoardDraw | None = None,
                dither_at: tuple[int, int] | None = None) -> tuple[float, float, float, float]:
    """One output pixel's final pass, at output point (x, y) (output fractions) of a
    Frame lit by `lights` (`frame_lights`; required when `inside`: ValueError("lights")):
    the paper profile's LUT (`lut_applied`, the bytes of the Frame's `lighting.paper.profile`,
    fully, when held) -> `paper_reflectance` -> `irradiance` -> `paper_signal` (the Display's light:
    operations.light) -> `highlight_rolled` at
    lights.peak -> x rgb_multipliers -> sRGB encode -> contrast about 0.5 -> gamma exponent -> clamp
    -> `dithered` at `dither_at` (an output pixel; None: no dither, for an oracle) -> the per-Output
    mask LAST: (0, 0, 0, 1) when not `inside` any Frame's opening.

    Test patterns: WHITE and GREY_RAMP (`pattern_signal` at `opening_uv`, the opening's unit square)
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


def output_pixel(x: float, y: float,
                 frames: Sequence[tuple[CornersFit | CoverFit, float, Linear, FrameLights | None]],
                 operations: PictureOperations, output: OutputSize) -> tuple[float, float, float, float]:
    """The reference for one Output carrying several Frames, each a fit, its profile's aspect, one
    flat linear colour and its lights (`frame_lights`): the colour of the last Frame whose opening
    holds (x, y), black where none does, through `final_pixel` with `inside` = some opening holds it
    and that covering Frame's lights. Pure; the two-Frames test's oracle, for operations without a
    test pattern (`final_pixel` needs the opening's uv for one). Each Frame's coverage is where it
    samples its picture (`picture_source_uv`), so the per-Frame mask decides what a Frame paints;
    the per-Output mask is the union of the openings."""
    colour: Linear = (0.0, 0.0, 0.0)
    lights: FrameLights | None = None
    for fit, aspect, flat, frame_lit in frames:
        if picture_source_uv(x, y, fit, output, aspect, 1, 1, CENTRED) is not None:
            colour, lights = flat, frame_lit
    inside = any(inside_opening(fit.corners, x, y) for fit, _, _, _ in frames)
    return final_pixel(colour, operations, inside=inside, lights=lights, x=x, y=y)


# ---- Room light: one physics path, the scalar reference player/native.py mirrors ----

HIGHLIGHT_KNEE: Final = 0.8         # the fixed rolloff's knee, in signal (a share of the TV's white)
NO_LIGHT: Final[Linear] = (0.0, 0.0, 0.0)
# The 4 x 4 ordered dither (Bayer): the threshold rank at row y % 4, column x % 4. The shader's copy
# is written in from here (player/native.py `_FINAL_CONSTANTS`).
BAYER_4: Final = (0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5)
# The shader's light kind codes (player/native.py writes them in).
LIGHT_KINDS: Final = ("wash", "far", "close")
LightKind = Literal["wash", "far", "close"]


@dataclass(frozen=True)
class LitLight:
    """One Frame light as the final pass draws it (the shader's uniform arrays), in mat space: the
    opening's own rectangle at its physical size, mm, x along its unit square's u, y along v (down
    for an opening drawn level)."""
    kind: LightKind
    rgb: Linear                                 # `light_on_frame`: rgb lux on the Frame (at its centre, close)
    toward: tuple[float, float] = (0.0, 0.0)    # far: unit vector toward the light (mat axes)
    band_mm: tuple[float, float] = (0.0, 0.0)   # far: (dark until, lit from), the mat shadow's band
    position_mm: tuple[float, float, float] = (0.0, 0.0, 1.0)   # close: (x, y) in mat space, out from the wall
    beam: tuple[float, float] = (0.0, 0.0)      # close: half the inner and outer beam angles, radians


@dataclass(frozen=True)
class FrameLights:
    """One Frame's lights resolved against one room-lights document, as the final pass uses them
    (the shader's uniforms): `to_opening` (output fractions -> the opening's unit square,
    inverse(homography(fit.corners))), the opening's physical `size_mm` (`opening_size_mm`), the
    mat's `depth_mm`, the `paper`, each light that adds light (`lights`, at most FRAME_LIGHTS_MAX;
    one adding none is left out) and the Frame's `peak`: its paper white's brightest signal over the
    opening (`_brightest_white`), the same for every pixel of the Frame, so the rolloff keeps the
    cone's shape."""
    to_opening: Matrix
    size_mm: tuple[float, float]
    depth_mm: float
    paper: Paper
    lights: tuple[LitLight, ...]
    peak: float


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


def _to_mat(across: float, up: float, quarter: int) -> tuple[float, float]:
    """A wall offset (across: + right, up: + up) as a mat-space offset: the wall's up is mat space's
    -y for an opening at quarter 0, turned clockwise by the Frame's quarter turn (as a far light's
    `toward`)."""
    turn = math.radians(quarter)
    x, y = across, -up
    return (x * math.cos(turn) - y * math.sin(turn), x * math.sin(turn) + y * math.cos(turn))


def _lit(light: WashLight | FarLight | CloseLight, rgb: Linear, depth: float, quarter: int,
         size: tuple[float, float]) -> LitLight:
    """`light` in mat space."""
    if isinstance(light, WashLight):
        return LitLight("wash", rgb)
    if isinstance(light, FarLight):
        turn = math.radians(light.from_degrees + quarter)
        half = light.softness_degrees / 2
        return LitLight("far", rgb, toward=(math.sin(turn), -math.cos(turn)), band_mm=(
            depth * _cot(min(90, light.steep_degrees + half)),
            depth * _cot(max(LIGHT_ELEVATION_MIN_DEGREES, light.steep_degrees - half))))
    dx, dy = _to_mat(light.across_mm, light.up_mm, quarter)
    return LitLight("close", rgb, position_mm=(size[0] / 2 + dx, size[1] / 2 + dy, light.out_mm),
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


def frame_lights(lighting: FrameLighting, document: RoomLightsDocument | None,
                 fit: CornersFit | CoverFit, profile: FrameProfile, display: DisplayLight) -> FrameLights:
    """`lighting`'s lights against `document` (None: none received yet, so every light adds none),
    in the opening's mat space at its physical size (`opening_size_mm`), with the Frame's peak: the
    paper white's brightest signal over the opening (`_brightest_white`). quarter = the nearest quarter
    turn of fit.rotation; a far light's `toward` = (sin a, -cos a), a = from_degrees + quarter, its
    band (depth cot(min(90, steep + softness/2)), depth cot(max(LIGHT_ELEVATION_MIN_DEGREES, steep -
    softness/2))); a close light's place `_to_mat` from the opening's centre. Pure."""
    size = opening_size_mm(fit, profile)
    quarter = round(fit.rotation / 90) * 90
    lights = []
    for light in lighting.lights:
        entry = None if document is None else document.lights.get(light.light)
        rgb = light_on_frame(light, entry)
        if max(rgb) > 0:
            lights.append(_lit(light, rgb, lighting.mat_depth_mm, quarter, size))
    lit = tuple(lights)
    return FrameLights(inverse(homography(fit.corners)), size, lighting.mat_depth_mm, lighting.paper, lit,
                       _brightest_white(lit, size, lighting.mat_depth_mm, lighting.paper, display))


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
    """E at output point (x, y), rgb lux: the sum over the Frame's lights of light_on_frame x shape x
    reaches, at m = transform(to_opening, x, y) x size_mm. Shape: 1 for wash and far; for close,
    `_close_shape`. Reaches: `_reached` (the mat shadow). No floor: the brightness follows the room
    down. Pure."""
    u, v = transform(lights.to_opening, x, y)
    return _irradiance_at(lights.lights, (u * lights.size_mm[0], v * lights.size_mm[1]), lights.size_mm,
                          lights.depth_mm)


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
