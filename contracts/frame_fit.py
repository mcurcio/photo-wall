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
from typing import Final, Literal

Point = tuple[float, float]                             # (x, y), fractions of the output, origin top-left
Corners = tuple[Point, Point, Point, Point]             # top-left, top-right, bottom-right, bottom-left
OutputSize = tuple[int, int]                            # (width, height) in output pixels, each >= 1
# The Frame output (owner, 2026-10-11: compatibility "measuring against the Frame output and not
# the Display"): the Frame's picture as drawn, its width and height in output pixels along the
# picture's own axes (u, v), each >= 1. Rounded up, so a photo holding it is never upscaled.
PictureSize = tuple[int, int]

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


def picture_size(mode: Literal["cover", "corners"], corners: Corners, rotation: float,
                 output: OutputSize, aspect: float) -> PictureSize:
    """The Frame output: how many output pixels the Frame's picture spans along its own width
    and height (follow-ups 2026-10-11, slice A1; run ledger .claude/runs/photo-4.md). Pure.

    - "cover": `cover_rectangle(corners, rotation, output, aspect)`'s width and height. It is
      smaller than the Output when the corners are inset (a mat opening) and larger than the
      opening when the picture is turned off-axis (the rectangle holds every corner).
    - "corners": the picture is stretched onto the quad, so each axis needs its longer pair edge,
      in output pixels: width = max(|TR - TL|, |BR - BL|), height = max(|BL - TL|, |BR - TR|);
      `rotation` (0, 90, 180 or 270 only) picks which edge is the picture's top, so 90 and 270
      swap the two.
    Each side is rounded up after subtracting 1e-6 (a float just over an integer is that
    integer), and is at least 1.

    ValueError as `cover_rectangle` ("cover_rotation", "cover_output", "cover_aspect",
    "cover_corners") for either mode; ValueError("corners_rotation") for "corners" with a
    rotation other than a quarter turn."""
    rect = cover_rectangle(corners, rotation, output, aspect)   # the one validation of the inputs
    if mode == "cover":
        return _pixels_up(rect.width), _pixels_up(rect.height)
    if rotation not in (0, 90, 180, 270):
        raise ValueError("corners_rotation")
    width, height = output
    tl, tr, br, bl = ((x * width, y * height) for x, y in corners)
    across = max(math.dist(tl, tr), math.dist(bl, br))
    down = max(math.dist(tl, bl), math.dist(tr, br))
    if rotation in (90, 270):                # a quarter turn: the picture's top is a side edge
        across, down = down, across
    return _pixels_up(across), _pixels_up(down)


def _pixels_up(length: float) -> int:
    """`length` output pixels rounded up (a float within 1e-6 above an integer is that integer),
    at least 1."""
    return max(1, math.ceil(length - 1e-6))


def window_holds(width: int, height: int, aspect: float, need: PictureSize) -> bool:
    """Whether the largest window of width-to-height `aspect` cut from a `width` x `height` image
    (the window the Player draws: the whole short side along the cut, contracts/photo_crop.py in
    roadmap 5a) holds `need` = (need width, need height) pixels on both axes, within 1e-9 of
    each. The one home of "a photo, or a copy of it, is at least the Frame output" (owner,
    2026-10-09: Immich's downsampled copy "only if it is at least the visible Frame size"): the
    rendition choice (media/library_client.py `covers`) and the photo fit rule's pixel floor
    (roadmap 5a, central/wall_plan/fit.py) both ask here. Pure; ValueError("window_size") unless
    every size is >= 1 and `aspect` is finite and above 0."""
    need_width, need_height = need
    if not (all(type(side) is int and side >= 1 for side in (width, height, need_width, need_height))
            and isinstance(aspect, (int, float)) and not isinstance(aspect, bool)
            and math.isfinite(aspect) and aspect > 0):
        raise ValueError("window_size")
    if width / height > aspect:              # wider than the window: it spans the whole height
        window_width, window_height = aspect * height, float(height)
    else:                                    # taller, or the same shape: the whole width
        window_width, window_height = float(width), width / aspect
    return window_width >= need_width - 1e-9 and window_height >= need_height - 1e-9


@dataclass(frozen=True)
class FrameRectangle:
    """A Frame's place on its screen (render redesign, run ledger .claude/runs/render-planes/
    s1-page.md; the design's FrameFit): an upright rectangle at the Frame profile's aspect (the
    profile stays the one source of the aspect), its centre as fractions of the screen's width and
    height as it hangs (contracts/display_turn.py `hanging`), its width as a fraction of that
    width; its height follows from the aspect. No corners, no angle: the screen's turn is the
    only turn. Whether it lies inside its screen needs the screen and the aspect: `place` checks it.

    __post_init__: ValueError("frame_rectangle") unless each field is a finite int or float (never
    a bool), 0 <= centre_x <= 1, 0 <= centre_y <= 1 and 0 < width <= 1."""
    centre_x: float
    centre_y: float
    width: float

    def __post_init__(self) -> None:
        raise NotImplementedError("render-planes S1 bead B1")


@dataclass(frozen=True)
class Placement:
    """A Frame's rectangle on its screen in whole pixels as the screen hangs: the top-left corner
    (x, y) and the size; the Frame's picture is exactly width x height pixels, so the display
    controller never scales it (bench T2: a scaled 4K picture glitches at four). Built by `place`."""
    x: int
    y: int
    width: int
    height: int


def place(rectangle: FrameRectangle, screen: OutputSize, aspect: float) -> Placement:
    """`rectangle` in whole pixels on a screen of `screen` = (W, H) pixels as it hangs, for a Frame
    profile of width-to-height `aspect`; each value rounds half up (floor(v + 0.5)):
    width = max(1, round(rectangle.width * W)), height = max(1, round(width / aspect)),
    x = round(centre_x * W - width / 2), y = round(centre_y * H - height / 2).
    The one home of how a rectangle becomes pixels: the Player's planes and, from S2, the Frame
    output Central sizes photos by (`picture_size`'s successor) both ask here. Pure.

    ValueError("placement_screen") unless both screen sides are ints (never bools) >= 1;
    ValueError("placement_aspect") unless `aspect` is finite and above 0;
    ValueError("placement_outside") unless 0 <= x, x + width <= W, 0 <= y and y + height <= H."""
    raise NotImplementedError("render-planes S1 bead B1")


__all__ = ["CONVEX_EPSILON", "FULL_SCREEN", "Corners", "CoverRectangle", "FrameRectangle", "OutputSize",
           "PictureSize", "Placement", "Point", "convex_clockwise", "cover_rectangle", "picture_size", "place",
           "window_holds"]
