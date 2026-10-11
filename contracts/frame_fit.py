"""The Frame fit's shared geometry (roadmap 1c; run ledger .claude/runs/display-1c.md): stdlib only.

The Player draws a Frame's picture through this one module (player/geometry.py builds its one
matrix on `cover_rectangle`, and the scalar reference is that same code). Display Host never
imports it (pyproject.toml: "Display Host never computes a Frame's geometry"): the host never
implements a second transform.

Rules this module owns (one home each):
- **Corners are convex and clockwise** (`convex_clockwise`): the four corners, top-left,
  top-right, bottom-right, bottom-left in fractions of the output (origin top-left), turn the
  same way at every corner with a cross product above `CONVEX_EPSILON`, so a folded or
  degenerate quad is refused. Both fits check it; contracts/models.py's `Calibration` too.
- **The cover rectangle** (`cover_rectangle`): in Cover, the Frame's picture has the Frame
  profile's aspect ratio (width_px : height_px; the profile is the one source of the Frame's
  aspect), is turned `rotation` degrees clockwise, and is the smallest such rectangle that holds
  every corner. Worked in output PIXELS, so a non-square output never skews it: the picture's
  axes are u = (cos r, sin r) and v = (-sin r, cos r) in y-down pixels; with the corners'
  projected spans du on u and dv on v and the aspect a = width / height, the height is
  max(du / a, dv) and the width a x height; the centre is the midpoint of both spans (never the
  corners' average).

Rules it does NOT own: orientation (setting #96), which is the Frame profile's aspect alone (the
angle never changes it; owner 2026-10-11: ""landscape" and "portrait" are descriptions of the
aspect ratio. whereas the angle of rotation is a quality of the way frames are hung on the
wall"); the per-Frame and per-Output masks and the final pass (the Player, player/geometry.py and
player/native.py); which fit a Frame uses and where its profile is stored (Central).
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

Point = tuple[float, float]                             # (x, y), fractions of the output, origin top-left
Corners = tuple[Point, Point, Point, Point]             # top-left, top-right, bottom-right, bottom-left
OutputSize = tuple[int, int]                            # (width, height) in output pixels, each >= 1

FULL_SCREEN: Final[Corners] = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
CONVEX_EPSILON: Final = 1e-6                            # the smallest cross product of a usable corner


def convex_clockwise(corners: Sequence[Sequence[float]]) -> bool:
    """Whether the four corners turn clockwise (in y-down coordinates) at every corner with a cross
    product above CONVEX_EPSILON. Pure; the one copy of the rule (moved verbatim from
    contracts.models.Calibration's validator)."""
    cross = []
    for i, a in enumerate(corners):
        b, c = corners[(i + 1) % 4], corners[(i + 2) % 4]
        cross.append((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]))
    return min(cross) > CONVEX_EPSILON


@dataclass(frozen=True)
class CoverRectangle:
    """The Frame's picture in Cover, in output pixels: its centre, its width along the picture's u
    axis, its height along v, and the clockwise turn `rotation` in degrees, 0 <= rotation < 360."""
    centre_x: float
    centre_y: float
    width: float
    height: float
    rotation: float


def cover_rectangle(corners: Corners, rotation: float, output: OutputSize, aspect: float) -> CoverRectangle:
    """The smallest rectangle of width-to-height `aspect` (the Frame profile's width_px / height_px)
    turned `rotation` degrees clockwise that holds every corner, worked in output pixels (corners
    scaled by `output`). Pure. ValueError("cover_rotation") unless 0 <= rotation < 360 and finite;
    ValueError("cover_output") unless both sizes >= 1; ValueError("cover_aspect") unless `aspect`
    is finite and above 0; ValueError("cover_corners") unless `convex_clockwise(corners)`."""
    if not (isinstance(rotation, (int, float)) and not isinstance(rotation, bool)
            and math.isfinite(rotation) and 0 <= rotation < 360):
        raise ValueError("cover_rotation")
    width, height = output
    if not (type(width) is int and type(height) is int and width >= 1 and height >= 1):
        raise ValueError("cover_output")
    if not (isinstance(aspect, (int, float)) and not isinstance(aspect, bool)
            and math.isfinite(aspect) and aspect > 0):
        raise ValueError("cover_aspect")
    if not convex_clockwise(corners):
        raise ValueError("cover_corners")
    turn = math.radians(rotation)
    cos, sin = math.cos(turn), math.sin(turn)
    pixels = [(x * width, y * height) for x, y in corners]
    along_u = [x * cos + y * sin for x, y in pixels]
    along_v = [-x * sin + y * cos for x, y in pixels]
    mid_u = (min(along_u) + max(along_u)) / 2
    mid_v = (min(along_v) + max(along_v)) / 2
    tall = max((max(along_u) - min(along_u)) / aspect, max(along_v) - min(along_v))
    return CoverRectangle(centre_x=mid_u * cos - mid_v * sin, centre_y=mid_u * sin + mid_v * cos,
                          width=aspect * tall, height=tall, rotation=float(rotation))


__all__ = ["CONVEX_EPSILON", "FULL_SCREEN", "Corners", "CoverRectangle", "OutputSize", "Point",
           "convex_clockwise", "cover_rectangle"]
