"""What the overlay client draws, as a draw-list of plain ops. PURE: no cairo, no Wayland.

`render_slate` is the base page the private client shows under a released Output (and, translucent,
over a starting candidate: `testing`); `render_trial` is the live-calibration overlay; `render_health`
is a tint-on health page (the health layer, above everything, app included); `render_central` is
Central's card, drawn on the same layer when no health card shows. `paint.paint`
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
from collections.abc import Callable, Mapping
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
# Central's card (the Frame shows Central's error): its own look, never the Pi card's tint, slate
# or the shell's amber. A light card, the words dark, labelled "Central" in Central's blue.
CENTRAL_LABEL = "Central"
CENTRAL_CARD_RGBA: RGBA = (0.93, 0.94, 0.96, 0.96)
CENTRAL_TEXT_RGBA: RGBA = (0.09, 0.10, 0.13, 1.0)
CENTRAL_LABEL_RGBA: RGBA = (0.16, 0.36, 0.72, 1.0)
CENTRAL_SHARE = 0.2                           # of the explain area's height: three rows
CENTRAL_LEAST = 64
OUTSIDE_RGBA: RGBA = (0.0, 0.0, 0.0, 1.0)     # the Output outside the explain area
TEXT_FACE = "sans"
# How far right of its origin `text` inks at `size` in `face` (pixels): the layout's one question
# about a font. paint.py `text_width` answers it with cairo; render.py never imports cairo.
TextMeasure = Callable[[str, float, str], float]


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


def card_pad(area_width: int) -> int:
    """The health card's inner margin in an explain area `area_width` wide."""
    return max(8, round(0.024 * area_width))


def card_rect(rect: tuple[int, int, int, int], content: float = 0.0, *, share: float = 0.15,
              least: int = 48) -> tuple[int, int, int, int]:
    """A card's (x, y, w, h) inside `rect` (x, y, w, h: the explain area): as wide as its
    `content` (its widest line's ink) plus its margins, at least 60 % of the area's width and never
    wider than the area, centred; its bottom 5 % of the area's height above its bottom edge, `share`
    of the area's height tall and at least `least` px (the health card's two lines: 15 %, 48 px;
    Central's three rows: CENTRAL_SHARE, CENTRAL_LEAST)."""
    left, top, width, height = rect
    w = min(width, max(round(0.6 * width), math.ceil(content) + 2 * card_pad(width)))
    h = max(least, round(share * height))
    return left + (width - w) // 2, top + height - h - round(0.05 * height), w, h


def fit_size(text: str, size: float, room: float, measure: TextMeasure) -> float:
    """`size`, or the largest size found below it at which `text` inks within `room` pixels:
    shrink to fit, so no line of the card leaves the explain area."""
    for _ in range(32):
        used = measure(text, size, TEXT_FACE)
        if used <= room:
            return size
        size *= 0.98 * room / used
    return size


def render_health(width: int, height: int, page: HealthPage, *, area: ExplainArea,
                  measure: TextMeasure) -> DrawList:
    """A health page (overlay.health.HealthPage) on a width x height Output with the explain area
    `area`. Tint on: the area darkened, opaque black outside it, then the opaque card with the
    page's two lines inside the area: each line at its size for the card's height, shrunk until
    its measured ink fits the area's width less the margins, and the card as wide as the wider
    line (card_rect). Tint off: fully transparent (the layer unmapped)."""
    if not page.tint:
        return (Paint((0.0, 0.0, 0.0, 0.0), source=True),)
    rect = area_rect(area, (width, height))
    pad = card_pad(rect[2])
    room = max(1, rect[2] - 2 * pad)
    h = card_rect(rect)[3]
    first, second = page.lines
    first_size = fit_size(first, max(12, round(0.26 * h)), room, measure)
    second_size = fit_size(second, max(10, round(0.18 * h)), room, measure)
    content = max(measure(first, first_size, TEXT_FACE), measure(second, second_size, TEXT_FACE))
    x, y, w, h = card_rect(rect, content)
    return (
        Paint(HEALTH_TINT_RGBA, source=True),
        *outside(rect, width, height),
        Rect(x, y, w, h, CARD_RGBA),
        Text(x + pad, y + round(0.45 * h), first_size, first, TEXT_RGBA, TEXT_FACE),
        Text(x + pad, y + round(0.8 * h), second_size, second, TEXT_RGBA, TEXT_FACE),
    )


def render_central(width: int, height: int, lines: tuple[str, str], *, area: ExplainArea,
                   measure: TextMeasure) -> DrawList:
    """Central's card (the Frame shows Central's error when it blacks the Frame) on a width x height
    Output with the explain area `area`: nothing outside the card (no tint, no black: the Frame's own
    picture shows around it), then Central's card inside the area in Central's look, three rows: the
    label "Central", then Central's two lines verbatim, each shrunk until its ink fits the area's
    width less the margins, the card as wide as the widest (card_rect, CENTRAL_SHARE tall)."""
    rect = area_rect(area, (width, height))
    pad = card_pad(rect[2])
    room = max(1, rect[2] - 2 * pad)
    h = card_rect(rect, share=CENTRAL_SHARE, least=CENTRAL_LEAST)[3]
    first, second = lines
    label_size = fit_size(CENTRAL_LABEL, max(10, round(0.14 * h)), room, measure)
    first_size = fit_size(first, max(12, round(0.22 * h)), room, measure)
    second_size = fit_size(second, max(10, round(0.16 * h)), room, measure)
    content = max(measure(CENTRAL_LABEL, label_size, TEXT_FACE), measure(first, first_size, TEXT_FACE),
                  measure(second, second_size, TEXT_FACE))
    x, y, w, h = card_rect(rect, content, share=CENTRAL_SHARE, least=CENTRAL_LEAST)
    return (
        Paint((0.0, 0.0, 0.0, 0.0), source=True),
        Rect(x, y, w, h, CENTRAL_CARD_RGBA),
        Text(x + pad, y + round(0.27 * h), label_size, CENTRAL_LABEL, CENTRAL_LABEL_RGBA, TEXT_FACE),
        Text(x + pad, y + round(0.59 * h), first_size, first, CENTRAL_TEXT_RGBA, TEXT_FACE),
        Text(x + pad, y + round(0.85 * h), second_size, second, CENTRAL_TEXT_RGBA, TEXT_FACE),
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
