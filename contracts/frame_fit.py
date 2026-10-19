"""The Frame fit's shared geometry (render redesign, run ledger .claude/runs/render-planes/): stdlib only.

A Frame's place on its screen is an upright rectangle (`FrameRectangle`); the Player shows the
Frame's picture as its own display plane at `place`'s pixels, and Central sizes photos by the
same pixels (contracts/models.py `frame_output`). Display Host never imports this module
(pyproject.toml: "Display Host never computes a Frame's geometry").

Rules this module owns (one home each):
- **The rectangle's pixels** (`FrameRectangle`, `place`): a Frame's upright rectangle on its
  screen as it hangs becomes whole pixels by rounding half up (floor(v + 0.5)): its width from
  the screen's width, its height from that width and the Frame profile's aspect, its corner from
  its centre; a rectangle that does not lie wholly on its screen is refused, never clipped.
- **A photo holds the Frame output** (`window_holds`): the largest window of the Frame's aspect
  cut from an image holds the Frame output's pixels on both axes.

Rules it does NOT own: orientation (setting #96), which is the Frame profile's aspect alone (owner
2026-10-11: ""landscape" and "portrait" are descriptions of the aspect ratio. whereas the angle
of rotation is a quality of the way frames are hung on the wall"); the screen's turn
(contracts/display_turn.py); the picture operations (the Player, player/geometry.py); where a
Frame's rectangle and profile are stored (Central).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

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
    s1-page.md): an upright rectangle at the Frame profile's aspect (the
    profile stays the one source of the aspect), its centre as fractions of the screen's width and
    height as it hangs (contracts/display_turn.py `hanging`), its width as a fraction of that
    width; its height follows from the aspect. The screen's turn is the only turn. Whether it lies inside its screen needs the screen and the aspect: `place` checks it.

    __post_init__: ValueError("frame_rectangle") unless each field is a finite int or float (never
    a bool), 0 <= centre_x <= 1, 0 <= centre_y <= 1 and 0 < width <= 1."""
    centre_x: float
    centre_y: float
    width: float

    def __post_init__(self) -> None:
        if not (all(_finite(value) for value in (self.centre_x, self.centre_y, self.width))
                and 0 <= self.centre_x <= 1 and 0 <= self.centre_y <= 1 and 0 < self.width <= 1):
            raise ValueError("frame_rectangle")


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
        ...


@dataclass(frozen=True)
class Placement:
    """A Frame's rectangle on its screen in whole pixels as the screen hangs: the top-left corner
    (x, y) and the size, which the Frame's picture fills on screen: finished no larger than this
    (player/planes.py `finish_size`), the display controller enlarges it or shows it 1:1, never
    shrinks it (bench T2: a shrunk 4K picture glitches at four). Built by `place`."""
    x: int
    y: int
    width: int
    height: int

    @property
    def aspect(self) -> float:
        """width / height: the Frame's aspect (frame box run). The one home of a Frame's shape as a
        number: Central's fit rule (`FitTarget.aspect` asks its Frame output, these sides) and the
        Player's crop (player/planes.py) both read it. Pure."""
        ...


def place(rectangle: FrameRectangle, screen: OutputSize, aspect: float) -> Placement:
    """`rectangle` in whole pixels on a screen of `screen` = (W, H) pixels as it hangs, for a Frame
    profile of width-to-height `aspect`; each value rounds half up (floor(v + 0.5)):
    width = max(1, round(rectangle.width * W)), height = max(1, round(width / aspect)),
    x = round(centre_x * W - width / 2), y = round(centre_y * H - height / 2).
    The one home of how a rectangle becomes pixels: the Player's planes and the Frame output
    Central sizes photos by (contracts/models.py `frame_output`) both ask here. Pure.

    ValueError("placement_screen") unless both screen sides are ints (never bools) >= 1;
    ValueError("placement_aspect") unless `aspect` is finite and above 0;
    ValueError("placement_outside") unless 0 <= x, x + width <= W, 0 <= y and y + height <= H."""
    screen_width, screen_height = screen
    if not (type(screen_width) is int and type(screen_height) is int
            and screen_width >= 1 and screen_height >= 1):
        raise ValueError("placement_screen")
    if not (_finite(aspect) and aspect > 0):
        raise ValueError("placement_aspect")
    width = max(1, _half_up(rectangle.width * screen_width))
    height = max(1, _half_up(width / aspect))
    x = _half_up(rectangle.centre_x * screen_width - width / 2)
    y = _half_up(rectangle.centre_y * screen_height - height / 2)
    if not (0 <= x and x + width <= screen_width and 0 <= y and y + height <= screen_height):
        raise ValueError("placement_outside")
    return Placement(x=x, y=y, width=width, height=height)


def place_box(box: FrameBox, screen: OutputSize) -> Placement:
    """`box` in whole pixels on a screen of `screen` = (W, H) pixels as it hangs, by its edges, each
    rounding half up (floor(v + 0.5)), so two boxes that share an edge share its pixel column:
    x = min(W - 1, round(left * W)), right = max(x + 1, min(W, round((left + width) * W))),
    width = right - x; y and height likewise down with H. Total: every FrameBox on every screen
    has a placement of at least 1 x 1 lying on that screen. The one home of how a Frame's box
    becomes pixels: the Player's planes, Central's Frame output (contracts/models.py
    `frame_output`) and the console's mirror (central/console/src/placement.js `placeBox`) ask here.
    Pure. ValueError("placement_screen") unless both screen sides are ints (never bools) >= 1."""
    ...


def box_size_mm(box: FrameBox, screen_mm: ScreenMm) -> tuple[float, float]:
    """The box's physical (width, height) in millimetres on a Display whose picture area is
    `screen_mm` as it hangs (already turned): (box.width * screen_mm[0], box.height * screen_mm[1]).
    The one home of a Frame's physical size (the Player's lights and mat, the wall plan, the
    Planner's large-Frame video rule); contracts/models.py `frame_size_mm` turns the Display first.
    Pure. ValueError("screen_mm") unless both sides are ints (never bools) >= 1."""
    ...


def _finite(value: object) -> bool:
    """Whether `value` is a finite int or float, never a bool."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _half_up(value: float) -> int:
    """`value` rounded half up, floor(value + 0.5): never Python's half-to-even."""
    return math.floor(value + 0.5)


__all__ = ["BOX_SLACK", "FrameBox", "FrameRectangle", "OutputSize", "PictureSize", "Placement", "ScreenMm", "box_size_mm",
           "place", "place_box", "window_holds"]
