"""The reader's vocabulary and thresholds (decision 0027; owner, 2026-10-14: "Start working on a v2
that isn't so picky"). Pure.

Both targets are OpenCV ChArUco boards (contracts/calibration_target.py "v2"): every value has many
copies across both boards; a copy that is covered, clipped, crossed by a reflection edge or unlike
the rest is dropped and reported with its place, never averaged in. Only a Reading (never a
Refused) yields a suggestion, and each refusal stands for a real failure (design-v2, "Remaining
refusals").

The thresholds are placeholders tuned on the bench (the first real-photo check); each has its one
home here.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from enum import Enum, StrEnum
from typing import Any, Final

import numpy as np
from pydantic import BaseModel

from central.displays.views import PictureEntryView
from contracts.calibration_target import Surface
from contracts.node_picture import SoftwarePicture
from contracts.tuning import TestPattern

Rgb = tuple[float, float, float]

# -- Finding the boards (locate) --
MIN_SCREEN_MARKERS: Final = 2       # screen markers found: fewer give no plane to read
MIN_PAPER_MARKERS: Final = 1        # paper markers found (ChArUco interpolates the rest of the piece)
CHARUCO_CORNERS_MIN: Final = 8      # ChArUco corners: under it the homography takes the markers' corners
HOMOGRAPHY_PX: Final = 3.0          # USAC_MAGSAC's reprojection threshold, photo pixels
SMALL_SQUARE_PX: Final = 20.0       # a found square under this side is too small to read: come closer
VIEW_WHITE_PERCENTILE: Final = 99.5  # a RAW photo is shown to the detectors with this percentile at white
QR_PX_PER_SQUARE: Final = 120       # a QR copy is rectified at this scale before it is decoded
QR_MARGIN_SQUARES: Final = 0.25     # with this much of the surround round it
PAPER_REACH_SQUARES: Final = 3.0    # the paper's middle at most this many columns beyond the found screen markers

# -- Reading squares (read) --
RECTIFIED_PX: Final = 32            # each surface is rectified at this many pixels a square
SAMPLE_SHARE: Final = 0.5           # the middle share of a square sampled, each way (avoids edge blur only)
EDGE_TOLERANCE: Final = 0.015       # a white's black neighbours spread over this share of its surface's
                                    # white: a reflection edge crosses it (Detection row 6)
CONTRAST_FLOOR: Final = 1.3         # a white under this over its blacks is covered (grey ink is about 2:1)
COVERED_SHARE: Final = 0.5          # a white under this share of its surface's median white is covered
CLIP_SHARE: Final = 0.01            # a square with more of its sampled pixels clipped is dropped
NEAREST: Final = 6                  # the screen whites (and the ink's screen blacks) near each paper white
WHITE_TOLERANCE: Final = 0.04       # relative, per channel: a white ratio unlike the median of its copies
FALLOFF_KEEP: Final = 0.03          # the falloff fit's second pass keeps log whites within this of the first
FALLOFF_MAX: Final = 0.4            # the largest lens falloff k (1 - k r^2) the fit may report
GREY_TOLERANCE: Final = (0.004, 0.04)   # absolute + relative to the share: a grey copy unlike the rest
DISTINCT_SIGMAS: Final = 3.0        # a near-black step apart from raw 0 by this many of its noise
KEEP_SPREADS: Final = 2.0           # a gap within this many of its spread is "keep" / no change


class PhotoKind(StrEnum):
    """RAW: Apple ProRAW (DNG) decoded linear: white, tone and black read. NORMAL: a JPEG or HEIC
    with the phone's own tone curve: the white only (a rough reading), no tone, no black."""
    RAW = "raw"
    NORMAL = "normal"


@dataclass(frozen=True, eq=False)
class Photo:
    """One decoded photo: `pixels` float32 H x W x 3, LINEAR light with sRGB primaries for both
    kinds (the edge decodes; nothing past it linearises); `clipped` bool H x W, True where any
    channel reached the clip; `focal_px` the lens's focal length in pixels from the file's EXIF,
    None when it does not say; `name` the file's name, for messages."""
    name: str
    kind: PhotoKind
    pixels: np.ndarray
    clipped: np.ndarray
    focal_px: float | None


class FocalSource(StrEnum):
    """Where an Angle's focal length came from: the photo's EXIF, or estimated from the one view."""
    EXIF = "exif"
    ESTIMATED = "estimated"


@dataclass(frozen=True)
class Angle:
    """The viewing angle in degrees: the median angle between the screen board's normal and the
    rays to the screen whites read. Measured, never corrected (design-v2, "Angle")."""
    degrees: float
    focal: FocalSource


@dataclass(frozen=True)
class Place:
    """Where a square is: (row, column) on `surface`'s board, in board squares, and (x, y) in the
    photo as fractions of its width and height (x right, y down): what to move or retake."""
    surface: Surface
    row: float
    column: float
    x: float
    y: float

    def words(self) -> str:
        """The place in plain words, by thirds of the photo, e.g. "the paper, the photo's lower
        left" or "the screen, the photo's middle". Pure."""
        row = _ROWS[_third(self.y)]
        column = _COLUMNS[_third(self.x)]
        where = "middle" if row == column == "middle" else f"{row} {column}"
        return f"the {self.surface.value}, the photo's {where}"


_ROWS: Final = ("top", "middle", "lower")
_COLUMNS: Final = ("left", "middle", "right")


def _third(share: float) -> int:
    """Which third of the photo holds `share` (0, 1 or 2; beyond the edges the nearest)."""
    return min(2, max(0, int(3 * share)))


class DropReason(StrEnum):
    """Why a square or a copy was not used."""
    COVERED = "covered"                                 # little over its blacks, or far under its surface's whites
    TOO_FEW_BLACKS = "too-few-blacks"                   # under 2 of its black neighbours read
    REFLECTION_EDGE = "reflection-edge"                 # its blacks disagree: a reflection edge crosses it
    CLIPPED = "clipped"                                 # over CLIP_SHARE of its pixels at the camera's clip
    COPY_UNLIKE_THE_REST = "copy-unlike-the-rest"       # outside its tolerance of its copies' median
    COPIES_DISAGREE = "copies-disagree"                 # too few to say which is wrong: the value is unread
    TOO_FEW_CLEAN_COPIES = "too-few-clean-copies"       # fewer than two copies left


@dataclass(frozen=True)
class Dropped:
    """`value`: "white", "grey <code>" or "near-black <code>" (8-bit codes)."""
    value: str
    where: Place
    why: DropReason


class Refusal(StrEnum):
    TARGET_NOT_FOUND = "target-not-found"     # under MIN_SCREEN_MARKERS or MIN_PAPER_MARKERS found
    PAPER_OFF_SCREEN = "paper-off-screen"     # the paper's middle lies off the screen board
    CODE_UNREADABLE = "code-unreadable"       # no QR copy decodes to contracts.calibration_target's code
    UNKNOWN_ENTRY = "unknown-entry"           # Central has no sent entry for the code
    NOT_A_TARGET = "not-a-target"             # the entry's test pattern is no calibration target
    WHITE_UNREAD = "white-unread"             # under 2 agreeing whites on a surface after the drops


@dataclass(frozen=True)
class Refused:
    """No reading: `where` says what to move or retake (empty: the whole photo)."""
    why: Refusal
    where: tuple[Place, ...]
    detail: str


@dataclass(frozen=True)
class Value:
    """A value read from its agreeing copies: their median; `spread` the widest agreeing copy's
    relative distance from it (the uncertainty); `copies` how many agree (at least 2)."""
    value: Rgb
    spread: float
    copies: int


@dataclass(frozen=True)
class ToneStep:
    """A screen grey (`signal`, encoded) as a share of the screen white near it."""
    signal: float
    share: Value


@dataclass(frozen=True)
class Reading:
    """What the photo shows. `stage`: the entry's test pattern (a calibration target). `white`:
    screen white over bare paper white, each above its own blacks, the paper's corrected by the
    measured ink. `tone`: empty for a NORMAL photo. `lowest_distinct`: the lowest near-black code
    apart from raw 0 (None: a NORMAL photo, or none apart). `ink`: the print's ink as a share of
    bare paper white (Detection row 7). `falloff`: k of the lens falloff 1 - k r^2, r over half the
    photo's width (row 8). `angle`: the viewing angle. `dropped`: every square or copy left out,
    with its place and why."""
    entry: PictureEntryView
    stage: TestPattern
    kind: PhotoKind
    white: Value
    tone: tuple[ToneStep, ...]
    lowest_distinct: int | None
    ink: float
    falloff: float
    angle: Angle
    dropped: tuple[Dropped, ...]


class PreProperty(StrEnum):
    WHITE_LEVEL = "white-level"
    WHITE_COLOUR = "white-colour"
    TONE = "tone"
    BLACK = "black"


class Direction(StrEnum):
    RAISE = "raise"
    LOWER = "lower"
    WARMER = "warmer"
    COOLER = "cooler"
    KEEP = "keep"           # the gap is within KEEP_SPREADS of its spread


class Unit(StrEnum):
    PERCENT = "percent"
    KELVIN = "kelvin"
    GAMMA = "gamma"
    STEP = "step"


@dataclass(frozen=True)
class PreSoftwareStep:
    """One of the display's own controls to move: a direction and the gap's size, never a menu
    value (design Q2, as recommended: the display's menus are not recorded)."""
    property: PreProperty
    direction: Direction
    gap: float
    unit: Unit


@dataclass(frozen=True)
class PreSoftwareSuggestion:
    steps: tuple[PreSoftwareStep, ...]


@dataclass(frozen=True)
class PostSoftwareSuggestion:
    """The software picture to try: the entry's, with only brightness, contrast, colour temperature
    and gamma moved, each away from neutral only when that moves the readings by more than
    KEEP_SPREADS of their spread; `no_change` when `to` equals the entry's software picture."""
    to: SoftwarePicture
    no_change: bool


class SuggestionRefusal(StrEnum):
    LUT_UNREAD = "lut-unread"               # post-software with a LUT whose table could not be read
    TONE_UNREAD = "tone-unread"             # post-software with no grey read (a normal photo, or every
                                            # grey dropped): the display's response is unknown


@dataclass(frozen=True)
class SuggestionRefused:
    why: SuggestionRefusal
    detail: str


Suggestion = PreSoftwareSuggestion | PostSoftwareSuggestion | SuggestionRefused


def to_json(value: Reading | Refused | Suggestion) -> dict[str, Any]:
    """The plain JSON the command prints: enums as their values, each Place with its fields and
    words, the entry as the operator route answers it (PictureEntryView.model_dump(mode="json")).
    Pure."""
    out = _plain(value)
    if isinstance(value, PreSoftwareSuggestion):
        out = {"stage": "pre-software", **out}
    elif isinstance(value, PostSoftwareSuggestion):
        out = {"stage": "post-software", **out}
    return out


def _plain(value: Any) -> Any:
    """`value` as plain JSON values (to_json's walk). Pure."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Place):
        return {"surface": value.surface.value, "row": round(value.row, 2), "column": round(value.column, 2),
                "x": round(value.x, 3), "y": round(value.y, 3), "words": value.words()}
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


__all__ = [
    "CHARUCO_CORNERS_MIN", "CLIP_SHARE", "CONTRAST_FLOOR", "COVERED_SHARE", "DISTINCT_SIGMAS", "EDGE_TOLERANCE",
    "FALLOFF_KEEP", "FALLOFF_MAX", "GREY_TOLERANCE", "HOMOGRAPHY_PX", "KEEP_SPREADS", "MIN_PAPER_MARKERS",
    "MIN_SCREEN_MARKERS", "NEAREST", "PAPER_REACH_SQUARES", "QR_MARGIN_SQUARES", "QR_PX_PER_SQUARE",
    "RECTIFIED_PX", "SAMPLE_SHARE", "SMALL_SQUARE_PX", "VIEW_WHITE_PERCENTILE", "WHITE_TOLERANCE",
    "Angle", "Direction", "DropReason", "Dropped", "FocalSource", "Photo", "PhotoKind", "Place",
    "PostSoftwareSuggestion", "PreProperty", "PreSoftwareStep", "PreSoftwareSuggestion", "Reading", "Refusal",
    "Refused", "Rgb", "Suggestion", "SuggestionRefusal", "SuggestionRefused", "ToneStep", "Unit", "Value",
    "to_json",
]
