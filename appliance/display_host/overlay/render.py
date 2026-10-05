"""What the overlay client draws, as a draw-list of plain ops. PURE: no cairo, no Wayland.

`render_slate` is the base page the private client shows under a released Output (and, translucent,
over a starting candidate: `testing`); `render_trial` is the live-calibration overlay. `paint.paint`
executes a draw-list with cairo; tests read the draw-list directly. Text, colours and geometry are
the retired C client's (native/diagnostic-client.c), so the slate is pixel-identical.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

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
REASON_TEXT: Mapping[str, str] = MappingProxyType({
    "starting_new": "Starting Player - waiting for authorized handoff",
    "output_mode_changed": "Display mode changed - checking Player output",
    "surface_lease_or_process_lost": "Player output unavailable - management remains separate",
    "authorized_withdrawal": "Player restarting - waiting for new output",
})
REASON_DEFAULT = "Player output unavailable"
MAX_PRIMITIVES = 512          # the trial event's primitives text, as the shell bounds it


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


def render_slate(name: str, width: int, height: int, reason: str, testing: bool) -> DrawList:
    """The base page: background, then six lines laid out on a 1280-wide page scaled to the Output
    (never below 0.35)."""
    alpha = TESTING_ALPHA if testing else OPAQUE_ALPHA
    scale = max(width / 1280.0, 0.35)

    def line(x: float, y: float, size: float, text: str) -> Text:
        return Text(x * scale, y * scale, size * scale, text, TEXT_RGBA, "sans")

    return (
        Paint((*SLATE_RGB, alpha), source=True),
        line(48, 90, 36, "Photo Wall"),
        line(48, 150, 24, reason_text(reason)),
        line(48, 215, 20, f"Output {name}  |  {width} x {height} pixels"),
        line(48, 260, 20, "Frame binding unconfirmed"),
        line(48, 305, 18, "Base display service - app content is independently supervised"),
        line(48, 345, 18, "Panel brightness unavailable"),
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
