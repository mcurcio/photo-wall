"""Scalar calibration/SDR reference. Production pixels are processed by shaders.

Roadmap 1c (run ledger .claude/runs/display-1c.md, slice R1) adds the Frame fit and the final
pass, each stated once here and mirrored by player/native.py's shaders:
- **One matrix per fit** (`output_to_picture`): output fractions -> the picture's unit square.
  Corners: the inverse perspective onto the corners with the quarter turn folded in. Cover: the
  inverse of rotate, scale evenly and move onto contracts/frame_fit.py's cover rectangle, worked
  in output pixels so a non-square output never skews it.
- **Per-Frame mask** (`inside_opening`): a Frame draws nothing outside its own opening (owner
  2026-10-10 q2: outside the Frame is "someone else's territory"), in the position pass.
- **Final pass** (`final_pixel`): contracts/node_frame.py PictureOperations in their stated
  order, then the per-Output mask LAST, so black stays black outside every opening whatever
  the picture settings.
- **Photo fit** (`photo_rect`): each photo's source rectangle, centred cover of the Frame's
  aspect (today's `cover_rect`, renamed for Photo fit #48/#102; trim removed, owner q3).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

from contracts.frame_fit import Corners, OutputSize, cover_rectangle
from contracts.models import Calibration, CornersFit, CoverFit
from contracts.node_frame import PictureOperations

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
# the quarter-turn rule (Corners folds it into its matrix; `inverse_rotation` reads it).
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


def inverse_rotation(x: float, y: float, rotation: int) -> tuple[float, float]:
    """Undo a clockwise image rotation in screen coordinates."""
    if rotation not in UNDO_QUARTER_TURN:
        raise ValueError("rotation must be a quarter-turn")
    return transform(UNDO_QUARTER_TURN[rotation], x, y)


def cover_rect(crop: tuple[float, float, float, float], frame_aspect: float,
               source_width: int, source_height: int) -> tuple[float, float, float, float]:
    """Source UV rectangle used by centered cover, with crop applied exactly once."""
    if not math.isfinite(frame_aspect) or frame_aspect <= 0:
        raise ValueError("positive finite Frame aspect required")
    if source_width <= 0 or source_height <= 0:
        raise ValueError("positive source dimensions required")
    left, top, right, bottom = crop
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ValueError("invalid crop")
    width, height = right-left, bottom-top
    aspect = source_width*width / (source_height*height)
    if aspect > frame_aspect:
        width *= frame_aspect / aspect
    else:
        height *= aspect / frame_aspect
    return ((left+right-width)/2, (top+bottom-height)/2,
            (left+right+width)/2, (top+bottom+height)/2)


def source_uv(x: float, y: float, calibration: Calibration, frame_aspect: float,
              source_width: int, source_height: int) -> tuple[float, float] | None:
    u, v = transform(inverse(homography(calibration.corners)), x, y)
    if not (0 <= u <= 1 and 0 <= v <= 1):
        return None
    u, v = inverse_rotation(u, v, calibration.rotation)
    left, top, right, bottom = cover_rect(
        calibration.crop, frame_aspect, source_width, source_height
    )
    return left + u*(right-left), top + v*(bottom-top)


def decode_channel(value: float, video: bool = False) -> float:
    value = min(1.0, max(0.0, value))
    if video:
        return value / 4.5 if value < .081 else ((value + .099) / 1.099) ** (1/.45)
    return value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4


def encode_srgb(value: float) -> float:
    value = min(1.0, max(0.0, value))
    return 12.92*value if value <= .0031308 else 1.055*value**(1/2.4)-.055


def compose_pixel(samples: Iterable[tuple[tuple[float, float, float, float], float, bool]],
                  gain: float = 1) -> tuple[float, float, float, float]:
    """Bottom-to-top straight encoded RGBA/effective alpha/video -> opaque sRGB.

    Empty output is opaque black. Source alpha and Layer alpha multiply in linear
    premultiplied source-over; gain applies once after the complete composition.
    """
    if not math.isfinite(gain) or not 0 <= gain <= 2:
        raise ValueError("gain must be finite in [0, 2]")
    rgb = [0.0, 0.0, 0.0]
    for rgba, opacity, video in samples:
        if not all(math.isfinite(v) and 0 <= v <= 1 for v in (*rgba, opacity)):
            raise ValueError("finite normalized samples required")
        alpha = rgba[3]*opacity
        rgb = [decode_channel(rgba[i], video)*alpha + rgb[i]*(1-alpha) for i in range(3)]
    return (*(encode_srgb(v*gain) for v in rgb), 1.0)


# -- roadmap 1c, slice R1 ------------------------------------------------------------------------

def fit_of(calibration: Calibration) -> CornersFit | CoverFit:
    """The fit a Position draws: `calibration.fit`, or until INTEGRATE, a record without one read
    as CornersFit(corners, rotation) (exactly today's picture). INTEGRATE deletes the second arm."""
    if calibration.fit is not None:
        return calibration.fit
    return CornersFit(corners=calibration.corners, rotation=calibration.rotation)


def _cover_matrix(fit: CoverFit, output: OutputSize) -> Matrix:
    """Output fractions -> picture unit square for Cover: the inverse of "rotate `rotation`
    clockwise, scale to the cover rectangle's width and height, move to its centre", all in
    output pixels. With the picture axes u = (cos r, sin r), v = (-sin r, cos r) (y down) and the
    pixel point p = (x W, y H): u = (p - centre).u_axis / width + 1/2, likewise v with height."""
    rectangle = cover_rectangle(fit.corners, fit.rotation, output)
    if not (rectangle.width > 0 and rectangle.height > 0):
        raise ValueError("singular homography")
    width_px, height_px = output
    turn = math.radians(rectangle.rotation)
    cos, sin = math.cos(turn), math.sin(turn)
    cx, cy = rectangle.centre_x, rectangle.centre_y
    w, h = rectangle.width, rectangle.height
    return (cos*width_px/w, sin*height_px/w, .5 - (cx*cos + cy*sin)/w,
            -sin*width_px/h, cos*height_px/h, .5 + (cx*sin - cy*cos)/h,
            0.0, 0.0, 1.0)


def output_to_picture(fit: CornersFit | CoverFit, output: OutputSize) -> Matrix:
    """The one matrix of a fit: output fractions (x, y) -> the picture's unit square (u, v), origin
    top-left. Pure. ValueError("singular homography") for a degenerate fit."""
    if isinstance(fit, CoverFit):
        return _cover_matrix(fit, output)
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


def photo_rect(frame_aspect: float, source_width: int, source_height: int) -> tuple[float, float, float, float]:
    """The photo's source UV rectangle (left, top, right, bottom) for centred cover of
    `frame_aspect` (Photo fit). Pure; ValueError as `cover_rect` for bad sizes."""
    if not math.isfinite(frame_aspect) or frame_aspect <= 0:
        raise ValueError("positive finite Frame aspect required")
    if source_width <= 0 or source_height <= 0:
        raise ValueError("positive source dimensions required")
    width = height = 1.0
    aspect = source_width / source_height
    if aspect > frame_aspect:
        width = frame_aspect / aspect
    else:
        height = aspect / frame_aspect
    return ((1-width)/2, (1-height)/2, (1+width)/2, (1+height)/2)


def picture_source_uv(x: float, y: float, fit: CornersFit | CoverFit, output: OutputSize, frame_aspect: float,
                      source_width: int, source_height: int) -> tuple[float, float] | None:
    """The source UV one Frame samples at output point (x, y); None outside its opening or its
    picture. Replaces `source_uv`. The per-Frame mask (the opening) comes first, so a Frame never
    draws over its neighbour, then the picture's unit square."""
    if not inside_opening(fit.corners, x, y):
        return None
    u, v = transform(output_to_picture(fit, output), x, y)
    if not (0 <= u <= 1 and 0 <= v <= 1):
        return None
    left, top, right, bottom = photo_rect(frame_aspect, source_width, source_height)
    return left + u*(right-left), top + v*(bottom-top)


def final_pixel(linear: Linear, operations: PictureOperations, *, inside: bool) -> tuple[float, float, float, float]:
    """One output pixel's final pass: white balance, light factor (linear light), clamp, sRGB
    encode, contrast about 0.5, gamma exponent, clamp, then the per-Output mask (opaque black when
    not `inside` any Frame's opening). Pure. A contrast result below 0 meets the exponent as 0
    (a power of a negative number is undefined, in GLSL too); with the clamp after, this equals
    clamping before the exponent."""
    pixel = []
    for value, multiplier in zip(linear, operations.rgb_multipliers, strict=True):
        value = min(1.0, max(0.0, value*multiplier*operations.light_factor))
        value = (encode_srgb(value) - .5)*operations.contrast_slope + .5
        pixel.append(min(1.0, max(0.0, value) ** operations.gamma_exponent))
    if not inside:
        return (0.0, 0.0, 0.0, 1.0)
    return (*pixel, 1.0)


def output_pixel(x: float, y: float, frames: Sequence[tuple[CornersFit | CoverFit, Linear]],
                 operations: PictureOperations, output: OutputSize) -> tuple[float, float, float, float]:
    """The reference for one Output carrying several Frames, each drawn as one flat linear colour:
    the colour of the last Frame whose opening holds (x, y), black where none does, through
    `final_pixel` with `inside` = some opening holds it. Pure; the two-Frames test's oracle. Each
    Frame's coverage is where it samples its picture (`picture_source_uv`), so the per-Frame mask
    decides what a Frame paints; the per-Output mask is the union of the openings."""
    colour: Linear = (0.0, 0.0, 0.0)
    for fit, flat in frames:
        if picture_source_uv(x, y, fit, output, 1.0, 1, 1) is not None:
            colour = flat
    inside = any(inside_opening(fit.corners, x, y) for fit, _ in frames)
    return final_pixel(colour, operations, inside=inside)
