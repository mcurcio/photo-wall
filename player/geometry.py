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
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

from contracts.frame_fit import Corners, OutputSize, cover_rectangle
from contracts.models import CornersFit, CoverFit, PhotoCrop
from contracts.node_frame import PictureOperations, srgb_to_linear
from contracts.photo_crop import CENTRED, crop_window

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


def output_pixel(x: float, y: float, frames: Sequence[tuple[CornersFit | CoverFit, float, Linear]],
                 operations: PictureOperations, output: OutputSize) -> tuple[float, float, float, float]:
    """The reference for one Output carrying several Frames, each a fit, its profile's aspect and
    one flat linear colour:
    the colour of the last Frame whose opening holds (x, y), black where none does, through
    `final_pixel` with `inside` = some opening holds it. Pure; the two-Frames test's oracle. Each
    Frame's coverage is where it samples its picture (`picture_source_uv`), so the per-Frame mask
    decides what a Frame paints; the per-Output mask is the union of the openings."""
    colour: Linear = (0.0, 0.0, 0.0)
    for fit, aspect, flat in frames:
        if picture_source_uv(x, y, fit, output, aspect, 1, 1, CENTRED) is not None:
            colour = flat
    inside = any(inside_opening(fit.corners, x, y) for fit, _, _ in frames)
    return final_pixel(colour, operations, inside=inside)
