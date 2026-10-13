"""The calibration targets' layout and code (decision 0026; owner, 2026-10-13: "build the calibration
targets"). Stdlib only: the Pi's Player draws the screen target from it.

Two targets read from one phone photo by the agent's reader (scripts/calibration_target/):
- the **screen target**, drawn by the Player at its physical size in a Frame's opening while the
  picture session's test pattern is a target (contracts/tuning.py TARGET_PATTERNS): four ArUco
  markers at its corners, the **dark box** (raw black, the size of a landscape Letter or A4 sheet
  with slack) at its centre, local groups of screen patches round the box, the near-black ruler and
  the QR naming the port's picture change;
- the **paper target**, printed by Central (central/displays/calibration_paper.py) on a landscape
  Letter or A4 sheet, black ink only, laid uncut on the panel under the Frame's glass inside the
  dark box with a black card behind it (owner, 2026-10-13): four ArUco markers at its content's
  corners and, along the content's edges, a paper black and a bare-paper white per local group.

Measurement rules the layout serves (owner's reflection plan, 2026-10-13; design rule 1):
- Reflections only add light and are spatially uneven, so each patch is read only above its own
  surface's black in its own **local group**; a group whose screen black and paper black disagree
  with the rest has a reflection edge across it and is dropped, wherever it is (no "middle").
- Each screen patch of a group sits within PAIR_MM of its paper twin, so a reflection larger than a
  pair adds the same light to both.
- Every value has copies in several groups round the box: the white in every group, each grey in
  at least GREY_COPIES, each near-black step in at least NEAR_BLACK_COPIES.
- Only the paper's white is read from the paper (its printed greys would build the print look into
  the display); the screen's greys are read against the screen white of their own group.

Coordinates: each target in its own plane, millimetres, origin at its top-left corner, x right,
y down; the screen target's top is the opening's top when the opening is landscape (the Player
turns it a quarter clockwise in a portrait opening: player/geometry.py `target_point`).

Rules this module owns (one home each): both targets' sizes, the sheets, the markers (dictionary,
ids, bits, sizes, places), the dark box, the groups and every patch's place and value, the
near-black ruler, the QR's place and the code's text. Rules it does NOT own: how the Player maps an
opening to the target and which stage is raw (player/geometry.py `target_point`, `target_signal`),
the QR's modules (player/calibration_target.py `target_modules`, segno), the PDF
(central/displays/calibration_paper.py), how a photo is read (scripts/calibration_target/).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from contracts.tuning import (
    SIGNAL_LEVELS,  # noqa: F401  (slice P1: a NEAR_BLACK step k is k / SIGNAL_LEVELS)
)

# -- Sizes (millimetres; placeholders tuned on the bench, each change an erratum) --

LETTER_MM: Final = (279.4, 215.9)          # a US Letter sheet, landscape
A4_MM: Final = (297.0, 210.0)              # an ISO A4 sheet, landscape
PRINT_MARGIN_MM: Final = 6.35              # the least unprinted edge a home printer leaves
PAPER_TARGET_MM: Final = (264.0, 196.0)    # the printed content, centred on either sheet: within both
                                           # sheets less PRINT_MARGIN_MM on every side
BOX_SLACK_MM: Final = 4.0                  # the dark box exceeds the larger of both sheets by this on every side
DARK_BOX_MM: Final = (305.0, 224.0)        # max(LETTER_MM, A4_MM) + 2 * BOX_SLACK_MM, rounded up
SCREEN_TARGET_MM: Final = (385.0, 340.0)   # the whole screen target; an opening smaller than it (either
                                           # way round) cannot hold it (decision 0026, Costs)
PAIR_MM: Final = 36.0                      # the most from a screen patch's centre to its paper twin's, the sheet centred

# -- Markers (OpenCV's ArUco, prior art; the bits below are its DICT_4X4_50's, held by a test) --

MARKER_DICTIONARY: Final = "DICT_4X4_50"   # cv2.aruco's predefined dictionary name
MARKER_MODULES: Final = 6                  # 4 x 4 data bits inside a one-module black border
SCREEN_MARKER_IDS: Final = (0, 1, 2, 3)    # top-left, top-right, bottom-right, bottom-left
PAPER_MARKER_IDS: Final = (4, 5, 6, 7)     # the same corners of the paper target's content
SCREEN_MARKER_MM: Final = 36.0             # a screen marker's black border's outer side
PAPER_MARKER_MM: Final = 30.0              # a paper marker's
# id -> its 4 data rows, '1' white, '0' black, top row first (cv2.aruco generateImageMarker's bits).
MARKER_BITS: Final[Mapping[int, tuple[str, str, str, str]]] = {}

# -- The values a target carries --

GREY_SIGNALS: Final = (0.70, 0.50, 0.30, 0.15, 0.05)   # the screen greys, encoded signal
GREY_COPIES: Final = 3                     # the least groups each grey is drawn in
NEAR_BLACK_STEPS: Final = tuple(range(1, 17))           # raw codes k of 255 beside raw 0
NEAR_BLACK_COPIES: Final = 2               # the least copies of each near-black step
WHITE_COPIES: Final = 12                   # the least groups (each carries one white pair)

# -- The code (a QR, the owner's idea; OpenCV's detector reads it) --

CODE_PREFIX: Final = "PWCT1"               # the code text's first word: Photo Wall calibration target, v1
QR_ERROR: Final = "m"                      # segno's error correction level (15 %)
QR_QUIET_MODULES: Final = 4                # QR's quiet zone (ISO/IEC 18004), drawn white
QR_MM: Final = 40.0                        # the QR's square side, quiet zone included (version 5
                                           # at most for the longest code: about 0.9 mm a module)


class Surface(StrEnum):
    """Which target a patch or marker is on."""
    SCREEN = "screen"
    PAPER = "paper"


class PaperSize(StrEnum):
    """The sheet the paper target is printed on, landscape (the PDF route's `paper`)."""
    LETTER = "letter"
    A4 = "a4"


SHEETS_MM: Final[Mapping[PaperSize, tuple[float, float]]] = {PaperSize.LETTER: LETTER_MM, PaperSize.A4: A4_MM}


class Role(StrEnum):
    """What a patch is read for. BLACK: its surface's black in its group (screen raw 0, paper ink);
    WHITE: screen signal 1 or bare paper; GREY: a screen grey of GREY_SIGNALS; NEAR_BLACK: a raw
    screen step of NEAR_BLACK_STEPS, read against the BLACK of its ruler group."""
    BLACK = "black"
    WHITE = "white"
    GREY = "grey"
    NEAR_BLACK = "near-black"


class GroupKind(StrEnum):
    """PAIRED: one screen black, white and grey beside one paper black and white (the white and the
    grey read; the blacks detect a reflection edge). RULER: screen raw 0 with near-black steps."""
    PAIRED = "paired"
    RULER = "ruler"


@dataclass(frozen=True)
class Rect:
    """An axis-aligned rectangle in its surface's plane (mm; origin top-left, x right, y down)."""
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float

    def contains(self, x_mm: float, y_mm: float) -> bool:
        """x_mm <= x < x_mm + width_mm and y_mm <= y < y_mm + height_mm (half-open). Pure."""
        ...

    def centre(self) -> tuple[float, float]:
        """The rectangle's centre. Pure."""
        ...


@dataclass(frozen=True)
class Patch:
    """One flat patch. `signal`: on the screen the encoded signal drawn (0..1, equal channels; a
    NEAR_BLACK step k is k / SIGNAL_LEVELS); on paper 0.0 for ink, 1.0 for bare paper."""
    surface: Surface
    role: Role
    rect: Rect
    signal: float


@dataclass(frozen=True)
class Group:
    """A local group: every patch of it is read only above its own surface's BLACK of this group.
    PAIRED: exactly one screen BLACK, WHITE and GREY and one paper BLACK and WHITE, each screen
    patch within PAIR_MM of its paper twin. RULER: one or more screen BLACKs and NEAR_BLACK steps,
    each step touching a BLACK."""
    index: int
    kind: GroupKind
    patches: tuple[Patch, ...]


@dataclass(frozen=True)
class Marker:
    """One ArUco marker of MARKER_DICTIONARY: `rect` its black border's outer square. On the
    screen a one-module white quiet zone surrounds it (drawn); on paper the bare paper does."""
    surface: Surface
    marker_id: int
    rect: Rect


@dataclass(frozen=True)
class CalibrationTarget:
    """Both targets. `dark_box`, `qr` and every screen patch and marker are in the screen target's
    plane (SCREEN_TARGET_MM); every paper patch and marker in the paper target's (PAPER_TARGET_MM,
    the printed content). `paper_in_box`: where the paper content's top-left sits in the screen
    plane when the sheet lies centred in the dark box (the reader's PAIR_MM and scale check).
    `paper_words`: the paper content's free middle, where the PDF prints its words and no patch or
    marker lies."""
    dark_box: Rect
    qr: Rect
    paper_in_box: tuple[float, float]
    paper_words: Rect
    markers: tuple[Marker, ...]
    groups: tuple[Group, ...]


@dataclass(frozen=True)
class ScreenPoint:
    """What the screen target draws at one point, before the QR: `signal` encoded 0..1; `raw` True
    inside the dark box (raw black in every stage); `qr` True inside the QR's square (the drawer
    takes the module instead of `signal`)."""
    signal: float
    raw: bool
    qr: bool


@dataclass(frozen=True)
class TargetCode:
    """What the screen target's QR names: the port and its picture change, the key of Central's
    sent picture entry (GET /v1/operator/players/{player_id}/outputs/{output_id}/picture-changes/
    {picture_change})."""
    player_id: str
    output_id: str
    picture_change: int

    def __post_init__(self) -> None:
        """ValueError("target_code") unless player_id and output_id are node tokens
        (contracts.node_protocol.token) and picture_change an int >= 1 (never a bool)."""
        ...


# A QR's modules, row by row, True for a dark module; no quiet zone (the drawer adds QR_QUIET_MODULES).
QrModules = tuple[tuple[bool, ...], ...]


def calibration_target() -> CalibrationTarget:
    """The one layout, built once (cached) from the constants above. Invariants (held by
    tests/test_calibration_target_layout.py): every screen rect lies inside SCREEN_TARGET_MM and
    outside `dark_box` and `qr`, none overlaps another; every paper rect inside PAPER_TARGET_MM, none
    overlapping; the paper content at `paper_in_box` lies inside `dark_box`; at least WHITE_COPIES
    PAIRED groups spread over all four sides of the box; each GREY_SIGNALS value in at least
    GREY_COPIES groups on at least two sides; each NEAR_BLACK_STEPS code at least NEAR_BLACK_COPIES
    times; four screen markers at the target's corners (SCREEN_MARKER_IDS order) and four paper
    markers at the content's corners (PAPER_MARKER_IDS order); `paper_words` at least 120 x 30 mm
    inside the content, apart from every paper rect. Pure."""
    ...


def screen_point(x_mm: float, y_mm: float) -> ScreenPoint:
    """What the screen target draws at (x_mm, y_mm) of its plane: a marker's module (its quiet
    zone 1.0), a patch's signal, the QR's square (`qr` True), the dark box (0.0, `raw` True), else
    the surround 0.0. Outside SCREEN_TARGET_MM: the surround. Pure; the Player's reference
    (player/geometry.py `target_signal`) and its texture are built from it."""
    ...


def paper_ink(x_mm: float, y_mm: float) -> bool:
    """True where the paper target prints black ink at (x_mm, y_mm) of its content's plane: a
    paper BLACK patch, a paper marker's black module. Outside PAPER_TARGET_MM: False. Pure; the
    PDF (central/displays/calibration_paper.py) is drawn from it."""
    ...


def code_text(code: TargetCode) -> str:
    """The QR's text: CODE_PREFIX, player_id, output_id and the decimal picture_change, joined by
    single spaces (a node token holds no space). Pure."""
    ...


def parsed_code(text: str) -> TargetCode | None:
    """The code `text` names, or None when it is not exactly code_text's shape (another QR, a
    misread). Pure."""
    ...


__all__ = [
    "A4_MM", "BOX_SLACK_MM", "CODE_PREFIX", "DARK_BOX_MM", "GREY_COPIES", "GREY_SIGNALS", "LETTER_MM",
    "MARKER_BITS", "MARKER_DICTIONARY", "MARKER_MODULES", "NEAR_BLACK_COPIES", "NEAR_BLACK_STEPS",
    "PAIR_MM", "PAPER_MARKER_IDS", "PAPER_MARKER_MM", "PAPER_TARGET_MM", "PRINT_MARGIN_MM", "QR_ERROR",
    "QR_MM", "QR_QUIET_MODULES", "SCREEN_MARKER_IDS", "SCREEN_MARKER_MM", "SCREEN_TARGET_MM", "SHEETS_MM",
    "WHITE_COPIES", "CalibrationTarget", "Group", "GroupKind", "Marker", "PaperSize",
    "Patch", "QrModules", "Rect", "Role", "ScreenPoint", "Surface", "TargetCode", "calibration_target",
    "code_text", "paper_ink", "parsed_code", "screen_point",
]
