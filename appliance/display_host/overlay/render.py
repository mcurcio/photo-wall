"""What the overlay client draws, as a draw-list of plain ops. PURE: no cairo, no Wayland.

`render_slate` is the base page the private client shows under a released Output (and, translucent,
over a starting candidate: `testing`); `render_trial` is the live-calibration overlay; `render_health`
is a tint-on health page (the health layer, above everything, app included). `paint.paint`
executes a draw-list with cairo; tests read the draw-list directly. Text, colours and geometry are
the retired C client's (native/diagnostic-client.c), so the slate on a whole Output is
pixel-identical.

The slate and the health card are one debug overlay (issue #124), laid out in the explain area
(overlay/area.py, its one rule): inside it the page as before, scaled to the area; outside it opaque
black, drawn last so nothing of the page spills past the area's edge.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from .area import ExplainArea, area_rect

if TYPE_CHECKING:
    from .health import HealthPage

RGBA = tuple[float, float, float, float]
Point = tuple[float, float]

SLATE_RGB = (0.04, 0.06, 0.09)
OPAQUE_ALPHA = 1.0
# A starting candidate is covered translucently, so the compositor can present (and report) the
# candidate's real frames underneath (shell.c curtain 0.96 and its `testing` configure).
TESTING_ALPHA = 0.96
TRIAL_ALPHA = 0.0
TEXT_RGBA: RGBA = (0.94, 0.96, 1.0, 1.0)
TRIAL_RGBA: RGBA = (1.0, 1.0, 0.0, 1.0)
# The shell's reasons (native/shell.c `invalidate`, `hold_lost`) as the slate words them. No line
# says "restarting": nothing restarts the Player on its own (issue #125).
REASON_TEXT: Mapping[str, str] = MappingProxyType({
    "starting_new": "Starting Player - waiting for authorized handoff",
    "output_mode_changed": "Display mode changed - checking Player output",
    "surface_lease_or_process_lost": "Player stopped - recover with Hardware > Reboot",
    "authorized_withdrawal": "Player output withdrawn",
    "hold_lost": "Waiting for a picture",
})
REASON_DEFAULT = "Player output unavailable"
MAX_PRIMITIVES = 512          # the trial event's primitives text, as the shell bounds it
HEALTH_TINT_RGBA: RGBA = (0.0, 0.0, 0.0, 0.45)
CARD_RGBA: RGBA = (*SLATE_RGB, 1.0)
OUTSIDE_RGBA: RGBA = (0.0, 0.0, 0.0, 1.0)     # the Output outside the explain area


@dataclass(frozen=True, slots=True)
class Paint:
    """Fill the whole surface; `source` replaces the pixels (alpha included) instead of blending."""
    rgba: RGBA
    source: bool = False


@dataclass(frozen=True, slots=True)
class Rect:
    """A filled rectangle."""
    x: float
    y: float
    width: float
    height: float
    rgba: RGBA


@dataclass(frozen=True, slots=True)
class Line:
    """A stroked polyline through `points`, closed back to the first when `closed`."""
    points: tuple[Point, ...]
    rgba: RGBA
    width: float
    closed: bool = False


@dataclass(frozen=True, slots=True)
class Arc:
    """A stroked full circle."""
    x: float
    y: float
    radius: float
    rgba: RGBA
    width: float


@dataclass(frozen=True, slots=True)
class Text:
    """`text` with its baseline origin at (x, y); `face` None keeps cairo's default face."""
    x: float
    y: float
    size: float
    text: str
    rgba: RGBA
    face: str | None = None


Op = Paint | Rect | Line | Arc | Text
DrawList = tuple[Op, ...]


def reason_text(reason: str) -> str:
    return REASON_TEXT.get(reason, REASON_DEFAULT)


def outside(rect: tuple[int, int, int, int], width: int, height: int) -> DrawList:
    """Opaque black over the width x height Output outside `rect` (x, y, w, h): up to four bands
    (above, below, left, right), none for a rect that is the whole Output."""
    x, y, w, h = rect
    bands = ((0, 0, width, y), (0, y + h, width, height - y - h),
             (0, y, x, h), (x + w, y, width - x - w, h))
    return tuple(Rect(*band, OUTSIDE_RGBA) for band in bands if band[2] > 0 and band[3] > 0)


def render_slate(name: str, width: int, height: int, reason: str, testing: bool,
                 brightness: str = "", *, area: ExplainArea) -> DrawList:
    """The base page in the explain area `area` of a width x height Output: background, then up to
    six lines laid out on a 1280-wide page scaled to the area's width (never below 0.35) from its
    corner, then opaque black outside the area. The sixth line is `brightness`, the display
    controller's line (where brightness acts and its value, frame protocol v4 `brightness`), or
    nothing while it is empty."""
    alpha = TESTING_ALPHA if testing else OPAQUE_ALPHA
    rect = area_rect(area, (width, height))
    left, top, area_width, _ = rect
    scale = max(area_width / 1280.0, 0.35)

    def line(x: float, y: float, size: float, text: str) -> Text:
        return Text(left + x * scale, top + y * scale, size * scale, text, TEXT_RGBA, "sans")

    return (
        Paint((*SLATE_RGB, alpha), source=True),
        line(48, 90, 36, "Photo Wall"),
        line(48, 150, 24, reason_text(reason)),
        line(48, 215, 20, f"Output {name}  |  {width} x {height} pixels"),
        line(48, 260, 20, "Frame binding unconfirmed"),
        line(48, 305, 18, "Base display service - app content is independently supervised"),
        *((line(48, 345, 18, brightness),) if brightness else ()),
        *outside(rect, width, height),
    )


def render_trial(width: int, height: int, points: tuple[Point, Point, Point, Point]) -> DrawList:
    """The live-calibration overlay: a transparent page, the four corners (output-relative 0..1)
    joined into a closed quad, a numbered ring at each, and a title."""
    corners = tuple((x * width, y * height) for x, y in points)
    marks: list[Op] = []
    for number, (x, y) in enumerate(corners, start=1):
        marks.append(Arc(x, y, 9.0, TRIAL_RGBA, 3.0))
        marks.append(Text(x + 12, y + 18, 20, str(number), TRIAL_RGBA))
    return (
        Paint((*SLATE_RGB, TRIAL_ALPHA), source=True),
        Line(corners, TRIAL_RGBA, 3.0, closed=True),
        *marks,
        Text(24, 32, 20, "Live calibration - output-space overlay", TRIAL_RGBA),
    )


def card_rect(rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """The health card's (x, y, w, h) inside `rect` (x, y, w, h: the explain area): 60 % of its
    width, centred, its bottom 5 % of its height above its bottom edge, at least 48 px tall."""
    left, top, width, height = rect
    w = round(0.6 * width)
    h = max(48, round(0.15 * height))
    return left + (width - w) // 2, top + height - h - round(0.05 * height), w, h


def render_health(width: int, height: int, page: HealthPage, *, area: ExplainArea) -> DrawList:
    """A health page (overlay.health.HealthPage) on a width x height Output with the explain area
    `area`. Tint on: the area darkened, opaque black outside it, then the opaque card with the
    page's two lines inside the area. Tint off: fully transparent (the layer unmapped)."""
    if not page.tint:
        return (Paint((0.0, 0.0, 0.0, 0.0), source=True),)
    rect = area_rect(area, (width, height))
    x, y, w, h = card_rect(rect)
    pad = max(8, round(0.04 * w))
    first, second = page.lines
    return (
        Paint(HEALTH_TINT_RGBA, source=True),
        *outside(rect, width, height),
        Rect(x, y, w, h, CARD_RGBA),
        Text(x + pad, y + round(0.45 * h), max(12, round(0.26 * h)), first, TEXT_RGBA, "sans"),
        Text(x + pad, y + round(0.8 * h), max(10, round(0.18 * h)), second, TEXT_RGBA, "sans"),
    )


def parse_trial_points(primitives: str) -> tuple[Point, Point, Point, Point] | None:
    """The trial event's primitives: a JSON array of four [x, y] pairs, or None (ignored, as the C
    client did). A missing or non-numeric coordinate reads as 0.0 (jansson's json_number_value)."""
    if len(primitives) > MAX_PRIMITIVES:
        return None

    def unique(pairs: list[tuple[str, object]]) -> dict:
        if len({key for key, _ in pairs}) != len(pairs):
            raise ValueError("duplicate_key")
        return dict(pairs)

    def constant(_name: str) -> object:
        raise ValueError("not_json")

    def integer(text: str) -> int:   # jansson refuses an integer outside json_int_t
        value = int(text)
        if not -(2**63) <= value < 2**63:
            raise ValueError("integer_overflow")
        return value

    def real(text: str) -> float:    # and a real that overflows a double
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("real_overflow")
        return value

    try:
        document = json.loads(primitives, object_pairs_hook=unique, parse_constant=constant,
                              parse_int=integer, parse_float=real)
    except (ValueError, RecursionError):
        return None
    if not isinstance(document, list) or len(document) != 4:
        return None

    def number(point: object, index: int) -> float:
        value = point[index] if isinstance(point, list) and len(point) > index else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        return float(value)

    return tuple((number(point, 0), number(point, 1)) for point in document)  # type: ignore[return-value]
