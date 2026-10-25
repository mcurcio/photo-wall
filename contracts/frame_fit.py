"""The Frame fit's shared geometry (render redesign, run ledger .claude/runs/render-planes/; frame
box run, .claude/runs/frame-box/page.md): stdlib only.

A Frame's place on its screen is a box (`FrameBox`); the Player shows the Frame's picture as its
own display plane at `place_box`'s pixels, and Central sizes photos by the same pixels
(contracts/models.py `frame_output`). Display Host never imports this module (pyproject.toml:
"Display Host never computes a Frame's geometry").

Rules this module owns (one home each):
- **The box's pixels** (`FrameBox`, `place_box`): a Frame's box on its screen as it hangs becomes
  whole pixels by rounding each edge half up (floor(v + 0.5)); a box always lies on its screen.
- **The box's physical size** (`box_size_mm`): its share of the Display's picture area.
- **The free axis** (`free_axis`): the one axis the largest window of the Frame's aspect cut from an
  image slides along, and the window's length on it as a share of the image. Everything that asks
  which way a window slides or how long it is reads it here: the crop window
  (contracts/photo_crop.py `crop_window`), the share a crop removes and the placement of a photo's
  window (central/wall_plan/fit.py `crop_share`, `place`) and `window_holds` below.
- **A photo holds the Frame output** (`window_holds`): the largest window of the Frame's aspect
  cut from an image holds the Frame output's pixels on both axes.

Rules it does NOT own: orientation, which is the box's aspect on its screen (owner 2026-10-11:
""landscape" and "portrait" are descriptions of the aspect ratio. whereas the angle of rotation
is a quality of the way frames are hung on the wall"); the screen's turn
(contracts/display_turn.py); the picture operations (the Player, player/geometry.py); where a
Frame's box is stored (Central).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Literal, NamedTuple

OutputSize = tuple[int, int]                            # (width, height) in output pixels, each >= 1
# The Frame output (owner, 2026-10-11: compatibility "measuring against the Frame output and not
# the Display"): the Frame's picture as drawn, its width and height in whole output pixels
# (`place`'s), each >= 1.
PictureSize = tuple[int, int]
# A Display's picture area in whole millimetres as built (never turned), as its EDID's base block
# states it (appliance/display_host/edid.py; frame box run, .claude/runs/frame-box/page.md): each >= 1.
ScreenMm = tuple[int, int]
# How far a FrameBox's right or bottom edge may pass the screen's in floating point (0.7 + 0.3).
BOX_SLACK: Final = 1e-9


class FreeAxis(NamedTuple):
    """The axis a crop window slides along (`along`: "x" across the image's width, "y" down its
    height) and the window's length on it (`length`, a share of the image; 1.0 when the shapes are
    equal and the window cannot move, then `along` is "y")."""

    along: Literal["x", "y"]
    length: float

    def start(self, offset: float) -> float:
        """The window's first edge on the axis at `offset`: offset x (1 - length)."""
        return offset * (1 - self.length)


def free_axis(width: int, height: int, aspect: float) -> FreeAxis:
    """The free axis of a `width` x `height` image for a window of width-to-height `aspect`: an
    image wider than the window slides along "x" with a window aspect / image_aspect of its width;
    a taller one along "y", image_aspect / aspect of its height; equal aspects ("y", 1.0). Never a
    landscape or portrait label: the axis follows from the two ratios. Pure; ValueError for a
    non-positive or non-finite aspect or a non-positive size."""
    if not math.isfinite(aspect) or aspect <= 0:
        raise ValueError("positive finite Frame aspect required")
    if width <= 0 or height <= 0:
        raise ValueError("positive source dimensions required")
    image_aspect = width / height
    if image_aspect > aspect:
        return FreeAxis("x", aspect / image_aspect)
    if image_aspect < aspect:
        return FreeAxis("y", image_aspect / aspect)
    return FreeAxis("y", 1.0)


def window_holds(width: int, height: int, aspect: float, need: PictureSize) -> bool:
    """Whether the largest window of width-to-height `aspect` cut from a `width` x `height` image
    (the window the Player draws: the whole short side along the cut, contracts/photo_crop.py in
    roadmap 5a) holds `need` = (need width, need height) pixels on both axes, within 1e-9 of
    each. The one home of "a photo, or a copy of it, is at least the Frame output" (owner,
    2026-10-09: Immich's downsampled copy "only if it is at least the visible Frame size"): the
    rendition choice (media/library_client.py `covers`) and the photo fit rule's pixel floor
    (roadmap 5a, central/wall_plan/fit.py) both ask here. The window is `free_axis`'s: its length
    times the image's side along the axis, the whole other side. Pure; ValueError("window_size")
    unless every size is >= 1 and `aspect` is finite and above 0."""
    need_width, need_height = need
    if not (all(type(side) is int and side >= 1 for side in (width, height, need_width, need_height))
            and isinstance(aspect, (int, float)) and not isinstance(aspect, bool)
            and math.isfinite(aspect) and aspect > 0):
        raise ValueError("window_size")
    axis = free_axis(width, height, aspect)
    if axis.along == "x":
        window_width, window_height = axis.length * width, float(height)
    else:
        window_width, window_height = float(width), axis.length * height
    return window_width >= need_width - 1e-9 and window_height >= need_height - 1e-9


@dataclass(frozen=True)
class FrameBox:
    """A Frame's box on its screen (frame box run, .claude/runs/frame-box/page.md; owner,
    2026-10-19: "I need to be able to set width and height independently, along with the position
    of that box on the display"): its left and top edges, its width and its height, each a fraction
    of the screen's width or height as it hangs (contracts/display_turn.py `hanging`). The one
    source of the Frame's shape: its pixels are `place_box` on the screen's mode, its aspect their
    ratio, its physical size `box_size_mm`. Always on its screen: no check needs the screen.

    __post_init__: ValueError("frame_box") unless each field is a finite int or float (never a
    bool), 0 <= left, 0 <= top, 0 < width, 0 < height, left + width <= 1 + BOX_SLACK and
    top + height <= 1 + BOX_SLACK."""
    left: float
    top: float
    width: float
    height: float

    def __post_init__(self) -> None:
        """See the class docstring."""
        if not (all(_finite(value) for value in (self.left, self.top, self.width, self.height))
                and 0 <= self.left and 0 <= self.top and 0 < self.width and 0 < self.height
                and self.left + self.width <= 1 + BOX_SLACK
                and self.top + self.height <= 1 + BOX_SLACK):
            raise ValueError("frame_box")


@dataclass(frozen=True)
class Placement:
    """A Frame's rectangle on its screen in whole pixels as the screen hangs: the top-left corner
    (x, y) and the size, which the Frame's picture fills on screen: finished no larger than this
    (player/planes.py `finish_size`), the display controller enlarges it or shows it 1:1, never
    shrinks it (bench T2: a shrunk 4K picture glitches at four). Built by `place_box`."""
    x: int
    y: int
    width: int
    height: int

    @property
    def aspect(self) -> float:
        """width / height: the Frame's aspect (frame box run). The one home of a Frame's shape as a
        number: Central's fit rule (`FitTarget.aspect` asks its Frame output, these sides) and the
        Player's crop (player/planes.py) both read it. Pure."""
        return self.width / self.height


def place_box(box: FrameBox, screen: OutputSize) -> Placement:
    """`box` in whole pixels on a screen of `screen` = (W, H) pixels as it hangs, by its edges, each
    rounding half up (floor(v + 0.5)), so two boxes that share an edge share its pixel column:
    x = min(W - 1, round(left * W)), right = max(x + 1, min(W, round((left + width) * W))),
    width = right - x; y and height likewise down with H. Total: every FrameBox on every screen
    has a placement of at least 1 x 1 lying on that screen. The one home of how a Frame's box
    becomes pixels: the Player's planes, Central's Frame output (contracts/models.py
    `frame_output`) and the console's mirror (central/console/src/placement.js `placeBox`) ask here.
    Pure. ValueError("placement_screen") unless both screen sides are ints (never bools) >= 1."""
    screen_width, screen_height = screen
    if not (type(screen_width) is int and type(screen_height) is int
            and screen_width >= 1 and screen_height >= 1):
        raise ValueError("placement_screen")
    x, width = _edges(box.left, box.width, screen_width)
    y, height = _edges(box.top, box.height, screen_height)
    return Placement(x=x, y=y, width=width, height=height)


def box_size_mm(box: FrameBox, screen_mm: ScreenMm) -> tuple[float, float]:
    """The box's physical (width, height) in millimetres on a Display whose picture area is
    `screen_mm` as it hangs (already turned): (box.width * screen_mm[0], box.height * screen_mm[1]).
    The one home of a Frame's physical size (the Player's lights and mat, the wall plan, the
    Planner's large-Frame video rule); contracts/models.py `frame_size_mm` turns the Display first.
    Pure. ValueError("screen_mm") unless both sides are ints (never bools) >= 1."""
    screen_width, screen_height = screen_mm
    if not (type(screen_width) is int and type(screen_height) is int
            and screen_width >= 1 and screen_height >= 1):
        raise ValueError("screen_mm")
    return (box.width * screen_width, box.height * screen_height)


def _edges(start: float, length: float, side: int) -> tuple[int, int]:
    """One axis of `place_box`: the first pixel and the pixel count of the span `start` to
    `start + length` (fractions) on `side` pixels, each edge rounding half up on its own and the
    span kept on the screen and at least one pixel."""
    first = min(side - 1, _half_up(start * side))
    end = max(first + 1, min(side, _half_up((start + length) * side)))
    return first, end - first


def _finite(value: object) -> bool:
    """Whether `value` is a finite int or float, never a bool."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _half_up(value: float) -> int:
    """`value` rounded half up, floor(value + 0.5): never Python's half-to-even."""
    return math.floor(value + 0.5)


__all__ = ["BOX_SLACK", "FrameBox", "OutputSize", "PictureSize", "Placement", "ScreenMm", "box_size_mm", "place_box",
           "window_holds"]
