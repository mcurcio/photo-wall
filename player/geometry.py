"""Scalar calibration/SDR reference. Production pixels are processed by shaders.

Roadmap 1c (run ledger .claude/runs/display-1c.md, slice R1) adds the Frame fit and the final
pass, each stated once here and mirrored by player/native.py's shaders:
- **One matrix per fit** (`output_to_picture`): output fractions -> the picture's unit square.
  Corners: the inverse perspective onto the corners with the quarter turn folded in. Cover: the
  inverse of rotate, scale evenly and move onto contracts/frame_fit.py's cover rectangle (the
  Frame profile's aspect), worked in output pixels so a non-square output never skews it.
- **Per-Frame mask** (`inside_opening`): a Frame draws nothing outside its own opening (owner
  2026-10-10 q2: outside the Frame is "someone else's territory"), in the position pass.
- **Final pass** (`final_pixel`): contracts/node_frame.py PictureOperations in their stated
  order, then the per-Output mask LAST, so black stays black outside every opening whatever
  the picture settings.
- **Photo fit** (`photo_rect`): each photo's source rectangle, the window `Layer.crop` names, cut
  from the Frame profile's aspect (contracts/photo_crop.py, the one home of the window; no trim,
  owner q3). It is the one crop in either fit: in Cover the drawn rectangle has that same aspect,
  so the photo is never cropped twice or squeezed.

The print look (design /print-look, 2026-10-12) adds a LUT, saturation, the highlight rolloff
(which replaces the clamp after brightness, before white balance), the black floor and the Frame's
mat shadow to the final pass, in one order (`final_pixel`), each skipped at its neutral value.

Display tuning (decision 0024) adds the test patterns (`pattern_signal`): a look pattern (White,
Grey ramp) replaces the photos inside the opening and passes through every step and the mat; the
Black level is the raw signal, through no step and no mat; the per-Output mask stays last.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from contracts.frame_fit import Corners, OutputSize, cover_rectangle
from contracts.kelvin import kelvin_white
from contracts.lut import LUT_GRID, lut_offset
from contracts.models import CornersFit, CoverFit, FrameProfile, Mat, PhotoCrop
from contracts.node_frame import PictureOperations, srgb_to_linear
from contracts.photo_crop import CENTRED, crop_window
from contracts.tuning import TestPattern

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


# -- The print look (design /print-look, 2026-10-12, "The order"): steps 2, 4, 6 and 7 -------------
# Each step is stated once here and mirrored by player/native.py's final pass; a step at its
# neutral value is skipped, never computed, so neutral operations draw exactly the v4 pixels.

LUMA: Final[Linear] = (0.2126, 0.7152, 0.0722)    # Rec. 709 linear luminance, saturation's axis (Haeberli / CSS saturate())
ROLLOFF_FADE_POWER: Final = 2.0                     # fade toward white = g ** 2
LIGHT_ELEVATION_MIN_DEGREES: Final = 1.0            # a soft light's lower edge never reaches the wall plane


def saturated(linear: Linear, saturation: float) -> Linear:
    """Step 2, linear light: each channel L + saturation * (c - L), L = sum(LUMA_i * c_i). No clamp. Pure."""
    luminance = sum(weight * value for weight, value in zip(LUMA, linear, strict=True))
    return tuple(luminance + saturation * (value - luminance) for value in linear)


def peak(operations: PictureOperations) -> float:
    """M, white's level after brightness (design: the largest gain x brightness; the white-balance
    multipliers' largest is 1 and they act after the rolloff): light_factor. Pure."""
    return operations.light_factor


def rolloff_skipped(operations: PictureOperations) -> bool:
    """highlight_knee >= 1 or peak(operations) <= 1 (nothing white passes white): step 4 is then
    v4's clamp to 0..1 after white balance x brightness. The one home of when the rolloff acts:
    Central's picture view reports it (central/displays/views.py) through Display Host's
    operations."""
    return operations.highlight_knee >= 1 or peak(operations) <= 1


def rolled_off(linear: Linear, knee: float, peak: float) -> Linear:
    """Step 4 when not skipped, on brightness-scaled channels (max(0, c * light_factor)), before
    white balance. x = max channel; x <= knee: unchanged. Else t = (min(x, peak)-k)/(1-k),
    w = (peak-k)/(1-k), g = t*(1 + t/w**2)/(1 + t), y = k + (1-k)*g, fade = g ** ROLLOFF_FADE_POWER,
    each channel c*y/x*(1-fade) + y*fade. peak -> exactly 1, slope 1 at the knee.

    Extended Reinhard on the brightest channel with `peak` as its white, every channel scaled
    alike (hue kept) and faded toward white as it nears the top (the path to white of darktable's
    filmic and AgX). A channel saturation pushed past `peak` is held there (x capped), so it ends
    at white too, never above it. Pure.

    Worked through the shortfall 1 - g = (w - t)(w + t) / (w**2 (1 + t)) (the same g, rearranged),
    so at x >= peak, where t == w, it is exactly 0 and the pixel exactly white. White balance comes
    after, so that white is the Display's white (the colour temperature's tint kept)."""
    x = max(linear)
    if x <= knee:
        return linear
    t = (min(x, peak) - knee) / (1 - knee)
    w = (peak - knee) / (1 - knee)
    short = (w - t) * (w + t) / (w * w * (1 + t))
    g = 1 - short
    y = 1 - (1 - knee) * short
    fade = g ** ROLLOFF_FADE_POWER
    return tuple(value * y / x * (1 - fade) + y * fade for value in linear)


def floor_lifted(light: float, share: float) -> float:
    """Step 6, in the panel's light: share + (1 - share) * light (black point compensation: black
    becomes `share` of white light, white stays white). Pure."""
    return share + (1 - share) * light


def light_colour(kelvin: int) -> Linear:
    """A mat light's colour at `kelvin`, as light of luminance 1 (sum of LUMA_c * c = 1): the
    kelvin table's white (contracts/kelvin.py `kelvin_white`, whose largest channel is 1 for the
    Display's white balance) scaled so a light's strength alone says how bright it is and its
    kelvin only its colour. Pure."""
    white = kelvin_white(kelvin)
    luminance = sum(weight * channel for weight, channel in zip(LUMA, white, strict=True))
    return tuple(channel / luminance for channel in white)


@dataclass(frozen=True)
class MatLight:
    """One light of a Frame's mat, as the final pass uses it."""
    toward: tuple[float, float]   # unit vector from the opening toward the light (output axes, x right, y down)
    dark_until_mm: float          # s0 = depth * cot(min(90, steep + softness/2)): nearer the edge than this, fully shadowed
    lit_from_mm: float            # s1 = depth * cot(max(LIGHT_ELEVATION_MIN_DEGREES, steep - softness/2)): fully lit from here
    weight: Linear                # strength_tenths/10 * light_colour(colour_kelvin)


@dataclass(frozen=True)
class MatShadow:
    """A Frame's mat shadow (step 7): its opening in mat space and the lights that shade it."""
    to_opening: Matrix            # output fractions -> the opening's unit square: inverse(homography(fit.corners))
    size_mm: tuple[float, float]  # the square's physical extent along u, v
    lights: tuple[MatLight, ...]  # only lights with strength_tenths > 0; 1..MAT_LIGHTS_MAX


def _cot(degrees: float) -> float:
    """cot of an angle in degrees; cot(90) is exactly 0."""
    return 0.0 if degrees == 90 else 1 / math.tan(math.radians(degrees))


def mat_shadow(fit: CornersFit | CoverFit, profile: FrameProfile, mat: Mat) -> MatShadow | None:
    """None when not mat.shades (step 7 skipped). quarter = the nearest quarter turn of fit.rotation
    (round(rotation / 90) * 90). size: diag_mm = diagonal_inches*25.4, a = width_px/height_px,
    W = diag_mm*a/hypot(a,1), H = diag_mm/hypot(a,1); size_mm = (W, H) when quarter is 0 or 180,
    else (H, W). toward: alpha = radians(from_degrees + quarter), (sin alpha, -cos alpha). cot(90)
    is exactly 0.

    Everything is in mat space, the opening's own rectangle at its physical size, whose axes
    follow the opening's edges: so a skewed Corners opening still gets an even band, and the
    light's direction turns by the same quarter turn as the rectangle's size. The rest of a Cover
    rotation is the opening's own tilt on the output, already in its edges: an opening drawn level
    on the wall is lit from the wall's up whatever its tilt on the panel. Pure."""
    if not mat.shades:
        return None
    diagonal = profile.diagonal_inches * 25.4
    aspect = profile.width_px / profile.height_px
    width, height = diagonal * aspect / math.hypot(aspect, 1), diagonal / math.hypot(aspect, 1)
    quarter = round(fit.rotation / 90) * 90
    size = (width, height) if quarter % 180 == 0 else (height, width)
    lights = []
    for light in mat.lights:
        if light.strength_tenths <= 0:
            continue
        turn = math.radians(light.from_degrees + quarter)
        half = light.softness_degrees / 2
        strength = light.strength_tenths / 10
        lights.append(MatLight(
            toward=(math.sin(turn), -math.cos(turn)),
            dark_until_mm=mat.depth_mm * _cot(min(90, light.steep_degrees + half)),
            lit_from_mm=mat.depth_mm * _cot(max(LIGHT_ELEVATION_MIN_DEGREES, light.steep_degrees - half)),
            weight=tuple(strength * channel for channel in light_colour(light.colour_kelvin))))
    return MatShadow(inverse(homography(fit.corners)), size, tuple(lights))


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


def mat_shade(shadow: MatShadow, x: float, y: float) -> Linear:
    """At output point (x, y): m = transform(to_opening) * size_mm; per light s = the distance along `toward`
    from m to the square's boundary (min over the two axes it heads to; >= 0); v = 1 if s >= s1, 0 if s < s0,
    else smoothstep (t*t*(3-2t)), with s0 == s1 a hard step (s >= s0 -> 1);
    shade_c = (1 + sum_j weight_jc * v_j) / (1 + sum_j weight_jc). Fully lit is exactly (1, 1, 1)."""
    u, v = transform(shadow.to_opening, x, y)
    point = (u * shadow.size_mm[0], v * shadow.size_mm[1])
    lit, total = [1.0, 1.0, 1.0], [1.0, 1.0, 1.0]
    for light in shadow.lights:
        s = _edge_distance(point, light.toward, shadow.size_mm)
        if s >= light.lit_from_mm:
            reached = 1.0
        elif s < light.dark_until_mm:
            reached = 0.0
        else:
            t = (s - light.dark_until_mm) / (light.lit_from_mm - light.dark_until_mm)
            reached = t * t * (3 - 2 * t)
        for channel in range(3):
            lit[channel] += light.weight[channel] * reached
            total[channel] += light.weight[channel]
    return tuple(lit[channel] / total[channel] for channel in range(3))


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


def lut_applied(linear: Linear, table: bytes, amount: float) -> Linear:
    """Step 1: encode for the screen, look the colour up (`lut_sampled`), mix the table's colour in
    by `amount` (e (1 - amount) + s amount, as GLSL's mix), decode back to linear light. Pure."""
    encoded = tuple(encode_srgb(value) for value in linear)
    sampled = lut_sampled(table, encoded)
    return tuple(decode_channel(e * (1 - amount) + s * amount)
                 for e, s in zip(encoded, sampled, strict=True))


# -- Display tuning's test patterns (decision 0024): what a Frame shows while its Display is tuned --

GREY_RAMP_STEPS: Final = 11       # 0 %, 10 %, ..., 100 % of the signal
BLACK_LEVEL_STEPS: Final = 20     # raw steps 1..20 beside black 0
SIGNAL_LEVELS: Final = 255        # an 8-bit output's top code (the test Pi: output_bpc 8): raw step k is k / SIGNAL_LEVELS
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
    else:
        value = 0.0
        band = (v - 1/3) * 3
        if 0 <= band < 1:
            position = u * BLACK_LEVEL_STEPS
            cell = min(int(position), BLACK_LEVEL_STEPS - 1)
            margin = (1 - BLACK_LEVEL_PATCH) / 2
            if margin <= position - cell < 1 - margin and margin <= band < 1 - margin:
                value = (cell + 1) / SIGNAL_LEVELS
    return (value, value, value)


def final_pixel(linear: Linear, operations: PictureOperations, *, inside: bool,
                shade: Linear | None = None, lut: bytes | None = None,
                opening_uv: tuple[float, float] | None = None) -> tuple[float, float, float, float]:
    """One output pixel's final pass, in the print look's order (design "The order"):
    step 0 (display tuning): operations.test_pattern WHITE or GREY_RAMP replaces `linear` with
    srgb_to_linear of `pattern_signal` at `opening_uv` (the opening's unit square), and every step
    below runs as for a photo; BLACK_LEVEL is `pattern_signal` itself, every step below and the
    shade skipped. A pattern without `opening_uv` is ValueError("opening_uv") ->
    step 1 (`lut_applied`, the LUT `operations.lut` names mixed in by its amount) unless
    operations.lut is None or its bytes (`lut`) are not held -> step 2 (`saturated`) unless
    saturation == 1 -> steps 3-4: when `rolloff_skipped`, v4's white balance x light_factor
    clamped to 0..1; else max(0, c * light_factor), `rolled_off`, then x white balance (the
    rolloff's white is the Display's white) -> sRGB encode, contrast about 0.5, gamma exponent,
    clamp (v4) -> steps 6-7 in the panel's light, unless black_floor == 0 and `shade` is None:
    decode (sRGB, the panel the encode is for), `floor_lifted` unless black_floor == 0, x shade_c
    unless `shade` is None, encode -> the per-Output mask LAST: (0, 0, 0, 1) when not `inside`
    any Frame's opening, whatever the floor and the shadow. Pure.

    A contrast result below 0 meets the exponent as 0 (a power of a negative number is undefined,
    in GLSL too); with the clamp after, this equals clamping before the exponent."""
    pattern = operations.test_pattern
    if pattern is not None:
        if opening_uv is None:
            raise ValueError("opening_uv")
        signal = pattern_signal(pattern, *opening_uv)
        if pattern is TestPattern.BLACK_LEVEL:
            return (*signal, 1.0) if inside else (0.0, 0.0, 0.0, 1.0)
        linear = tuple(srgb_to_linear(value) for value in signal)
    if operations.lut is not None and lut is not None:
        linear = lut_applied(linear, lut, operations.lut.amount)
    if operations.saturation != 1.0:
        linear = saturated(linear, operations.saturation)
    if rolloff_skipped(operations):
        gained = tuple(min(1.0, max(0.0, value*multiplier*operations.light_factor))
                       for value, multiplier in zip(linear, operations.rgb_multipliers, strict=True))
    else:
        rolled = rolled_off(tuple(max(0.0, value*operations.light_factor) for value in linear),
                            operations.highlight_knee, peak(operations))
        gained = tuple(value*multiplier for value, multiplier in zip(rolled, operations.rgb_multipliers, strict=True))
    pixel = []
    for value in gained:
        value = (encode_srgb(value) - .5)*operations.contrast_slope + .5
        pixel.append(min(1.0, max(0.0, value) ** operations.gamma_exponent))
    if operations.black_floor != 0 or shade is not None:
        light = [srgb_to_linear(value) for value in pixel]
        if operations.black_floor != 0:
            light = [floor_lifted(value, operations.black_floor) for value in light]
        if shade is not None:
            light = [value * factor for value, factor in zip(light, shade, strict=True)]
        pixel = [encode_srgb(value) for value in light]
    if not inside:
        return (0.0, 0.0, 0.0, 1.0)
    return (*pixel, 1.0)


def output_pixel(x: float, y: float,
                 frames: Sequence[tuple[CornersFit | CoverFit, float, Linear, MatShadow | None]],
                 operations: PictureOperations, output: OutputSize) -> tuple[float, float, float, float]:
    """The reference for one Output carrying several Frames, each a fit, its profile's aspect, one
    flat linear colour and its mat shadow (None: no shadow): the colour of the last Frame whose
    opening holds (x, y), black where none does, through `final_pixel` with `inside` = some
    opening holds it and the shade of that covering Frame's shadow. Pure; the two-Frames test's
    oracle, for operations without a test pattern (`final_pixel` needs the opening's uv for one). Each Frame's coverage is where it samples its picture (`picture_source_uv`), so the
    per-Frame mask decides what a Frame paints; the per-Output mask is the union of the openings."""
    colour: Linear = (0.0, 0.0, 0.0)
    shadow: MatShadow | None = None
    for fit, aspect, flat, frame_shadow in frames:
        if picture_source_uv(x, y, fit, output, aspect, 1, 1, CENTRED) is not None:
            colour, shadow = flat, frame_shadow
    inside = any(inside_opening(fit.corners, x, y) for fit, _, _, _ in frames)
    shade = None if shadow is None else mat_shade(shadow, x, y)
    return final_pixel(colour, operations, inside=inside, shade=shade)
