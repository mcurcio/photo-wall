"""The reader's vocabulary and thresholds (decision 0026). Pure.

Each value has copies in several local groups (contracts/calibration_target.py); copies that
disagree are dropped, never averaged, and every drop is reported with where it was and why. Only a
Reading (never a Refused) yields a suggestion.

The thresholds are placeholders tuned on the bench (the first real-photo check); each has its one
home here.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

import numpy as np

from central.displays.views import PictureEntryView
from contracts.calibration_target import Surface
from contracts.node_picture import SoftwarePicture
from contracts.tuning import TestPattern

Rgb = tuple[float, float, float]

PATCH_SHARE: Final = 0.6            # the middle share of a patch sampled, each way (avoids edge blur only)
WHITE_TOLERANCE: Final = 0.03       # relative, per channel: a white copy unlike the median of its copies
GREY_TOLERANCE: Final = (0.004, 0.04)   # absolute + relative to the share: a grey copy unlike the rest
BLACK_EDGE_TOLERANCE: Final = 0.004     # a group's black above the blacks' median by more (a share of
                                        # the white): a reflection edge crosses the group
PHOTOS_TOLERANCE: Final = 0.02      # relative: two photos' whites must agree this closely
SCALE_TOLERANCE: Final = 0.02       # relative: the paper's printed size against the layout, in the screen's mm
MARKERS_NEEDED: Final = 3           # markers found per surface, of four
CLIP_SHARE: Final = 0.01            # a patch with more of its sampled pixels clipped is unread
DISTINCT_SIGMAS: Final = 3.0        # a near-black step apart from raw 0 by this many of the black's spreads
KEEP_SPREADS: Final = 2.0           # a gap within this many of its spread is "keep" / no change


class PhotoKind(StrEnum):
    """RAW: Apple ProRAW (DNG) decoded linear: white, tone and black read. NORMAL: a JPEG or HEIC
    with the phone's own tone curve: the white only (about 4 % off), no tone, no black."""
    RAW = "raw"
    NORMAL = "normal"


@dataclass(frozen=True, eq=False)
class Photo:
    """One decoded photo: `pixels` float32 H x W x 3, linear light for RAW (the sensor's clip at
    1.0), the decoded sRGB values for NORMAL; `clipped` bool H x W, True where any channel reached
    the clip; `name` the file's name, for messages."""
    name: str
    kind: PhotoKind
    pixels: np.ndarray
    clipped: np.ndarray


@dataclass(frozen=True)
class Place:
    """Where on a target, in its own plane (mm; contracts/calibration_target.py): what to move or
    retake."""
    surface: Surface
    x_mm: float
    y_mm: float

    def words(self) -> str:
        """The place in plain words, by thirds of its target each way, e.g. "the paper's lower
        left" or "the screen target's top middle". Pure."""
        ...


class DropReason(StrEnum):
    """Why a copy or a group was not used."""
    COPY_UNLIKE_THE_REST = "copy-unlike-the-rest"       # outside its tolerance of its copies' median
    COPIES_DISAGREE = "copies-disagree"                 # too few to say which is wrong: the value is unread
    TOO_FEW_CLEAN_COPIES = "too-few-clean-copies"       # fewer than two copies left
    REFLECTION_EDGE = "reflection-edge"                 # a group whose blacks disagree with the rest


@dataclass(frozen=True)
class Dropped:
    """`value`: "group", "white", "grey <signal>" or "near-black <code>"."""
    value: str
    where: Place
    why: DropReason


class Refusal(StrEnum):
    TARGET_NOT_FOUND = "target-not-found"     # fewer than MARKERS_NEEDED markers on a surface
    CODE_UNREADABLE = "code-unreadable"       # no QR read, or not contracts.calibration_target's code
    DIFFERENT_ENTRIES = "different-entries"   # two photos' codes differ
    UNKNOWN_ENTRY = "unknown-entry"           # Central has no sent entry for the code
    NOT_A_TARGET = "not-a-target"             # the entry's test pattern is no calibration target
    CLIPPED = "clipped"                       # a white patch's pixels reached the sensor's clip
    SCALE_MISMATCH = "scale-mismatch"         # the paper not printed at 100 %
    WHITE_UNREAD = "white-unread"             # under half the white copies agree: too many groups dropped
    PHOTOS_DISAGREE = "photos-disagree"       # two photos' whites differ: the light on the sheet changed


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
    """A screen grey (`signal`, encoded) as a share of the screen white in its own group."""
    signal: float
    share: Value


@dataclass(frozen=True)
class Reading:
    """What the photos show. `stage`: the entry's test pattern (a calibration target). `white`:
    screen white over paper white, each above its own surface's black in its group. `tone`: empty
    for a NORMAL photo. `lowest_distinct`: the lowest near-black code apart from raw 0 (None: a
    NORMAL photo, or none apart). With two photos, per patch the darker reading (reflections only
    add light)."""
    entry: PictureEntryView
    stage: TestPattern
    kind: PhotoKind
    white: Value
    tone: tuple[ToneStep, ...]
    lowest_distinct: int | None
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


@dataclass(frozen=True)
class SuggestionRefused:
    why: SuggestionRefusal
    detail: str


Suggestion = PreSoftwareSuggestion | PostSoftwareSuggestion | SuggestionRefused


def to_json(value: Reading | Refused | Suggestion) -> dict[str, Any]:
    """The plain JSON the command prints: enums as their values, each Place with its words, the
    entry as the operator route answers it (PictureEntryView.model_dump(mode="json")). Pure."""
    ...


__all__ = [
    "BLACK_EDGE_TOLERANCE", "CLIP_SHARE", "DISTINCT_SIGMAS", "GREY_TOLERANCE", "KEEP_SPREADS",
    "MARKERS_NEEDED", "PATCH_SHARE", "PHOTOS_TOLERANCE", "SCALE_TOLERANCE", "WHITE_TOLERANCE",
    "Direction", "DropReason", "Dropped", "Photo", "PhotoKind", "Place", "PostSoftwareSuggestion",
    "PreProperty", "PreSoftwareStep", "PreSoftwareSuggestion", "Reading", "Refusal", "Refused", "Rgb",
    "Suggestion", "SuggestionRefusal", "SuggestionRefused", "ToneStep", "Unit", "Value", "to_json",
]
