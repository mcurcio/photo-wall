"""The calibration targets' layout and code (decision 0026; owner, 2026-10-13: "build the calibration
targets"). Stdlib only: the Pi's Player draws the screen target from it.

**v2 (decision 0027; owner, 2026-10-14: "Start working on a v2 that isn't so picky")**, the section
"v2" below: both targets are OpenCV ChArUco boards (cv2.aruco.CharucoBoard; prior art), so any
part of either, at any scale, turn or print, locates itself. This module keeps the boards'
constants, which square is what (`square`), the QR copies and rulers, the code's text, and the
two rasters OpenCV drew at build time (`board_raster`; scripts/calibration_boards.py writes them
into contracts/calibration_boards.py, a test regenerates and compares them). The Pi never imports
OpenCV. Rules v2 does NOT own: the cv2 board objects (scripts/calibration_target/boards.py), how
the Player maps an opening to the board (player/geometry.py `board_point`, `board_signal`), the
PDF (central/displays/calibration_paper.py), how a photo is read (scripts/calibration_target/).

The v1 layout (everything above the "v2" section: millimetres, the dark box, hand-placed marker
bits) is retiring: its last importers move to v2 slice by slice, and the closing slice deletes it.

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

import functools
import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from contracts.node_protocol import counter, token
from contracts.tuning import SIGNAL_LEVELS

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
MARKER_BITS: Final[Mapping[int, tuple[str, str, str, str]]] = {
    0: ("1011", "0101", "0011", "0010"),
    1: ("0000", "1111", "1001", "1010"),
    2: ("0011", "0011", "0010", "1101"),
    3: ("1001", "1001", "0100", "0110"),
    4: ("0101", "0100", "1001", "1110"),
    5: ("0111", "1001", "1100", "1101"),
    6: ("1001", "1110", "0010", "1110"),
    7: ("1100", "0100", "1111", "0010"),
}

# -- The values a target carries --

GREY_CODES: Final = (179, 128, 77, 38, 13)   # the screen greys, 8-bit codes of SIGNAL_LEVELS (about
                                             # 0.70, 0.50, 0.30, 0.15, 0.05): on the grid the Player's
                                             # texture stores, so drawn, reference and reader agree
GREY_SIGNALS: Final = tuple(code / SIGNAL_LEVELS for code in GREY_CODES)   # the same, encoded signal
GREY_COPIES: Final = 3                     # the least groups each grey is drawn in
NEAR_BLACK_STEPS: Final = tuple(range(1, 17))           # raw codes k of 255 beside raw 0
NEAR_BLACK_COPIES: Final = 2               # the least copies of each near-black step
WHITE_COPIES: Final = 12                   # the least groups (each carries one white pair)

# -- The code (a QR, the owner's idea; OpenCV's detector reads it) --

CODE_PREFIX: Final = "PWCT2"               # the code text's first word: Photo Wall calibration target, v2
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
    WHITE: screen signal 1 or bare paper; GREY: a screen grey of GREY_SIGNALS; NEAR_BLACK: a screen
    step of NEAR_BLACK_STEPS, read against the BLACK of its ruler group. Every screen patch is drawn
    raw in the pre-software target and through Photo Wall's steps in the post-software one (only
    the dark box stays raw there: player/geometry.py `target_signal`)."""
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
        return (self.x_mm <= x_mm < self.x_mm + self.width_mm
                and self.y_mm <= y_mm < self.y_mm + self.height_mm)

    def centre(self) -> tuple[float, float]:
        """The rectangle's centre. Pure."""
        return (self.x_mm + self.width_mm / 2, self.y_mm + self.height_mm / 2)


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
        try:
            token(self.player_id)
            token(self.output_id)
            counter(self.picture_change, 1)
        except ValueError:
            raise ValueError("target_code") from None


# A QR's modules, row by row, True for a dark module; no quiet zone (the drawer adds QR_QUIET_MODULES).
QrModules = tuple[tuple[bool, ...], ...]


# -- The layout's own measures (private: the reader and the drawers take rects, never these) --

_PATCH_MM: Final = 10.0           # a PAIRED patch's side (the reader samples its middle 60 %)
_GAP_MM: Final = 2.0              # between a screen patch and the dark box, and between a group's patches
_PATCH_PITCH_MM: Final = _PATCH_MM + _GAP_MM
_GROUP_GAP_MM: Final = 4.0        # between two groups along an edge
_GROUP_PITCH_MM: Final = 3 * _PATCH_PITCH_MM - _GAP_MM + _GROUP_GAP_MM   # black, white, grey
_STEP_MM: Final = (9.0, 8.0)      # a near-black step, along and across its ruler (>= 8 x 8 mm)
_RULER_TOP_Y_MM: Final = 12.0     # the first ruler's top, in the band above the top groups
_RULER_BOTTOM_X_MM: Final = 60.0  # the second ruler's left, in the band below the bottom groups
_QR_X_MM: Final = 270.0           # the QR square's left, beside the second ruler
_WORDS: Final = Rect(40.0, 40.0, 184.0, 116.0)   # paper_words, in the paper content's plane
_SURROUND: Final = ScreenPoint(0.0, raw=False, qr=False)


@functools.cache
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
    # Groups: the PAIRED ones clockwise from the top-left (top, right, bottom, left edges), then
    # the two RULER groups (above the top groups, below the bottom groups, steps reversed).
    screen_w, screen_h = SCREEN_TARGET_MM
    box_w, box_h = DARK_BOX_MM
    dark_box = Rect((screen_w - box_w) / 2, (screen_h - box_h) / 2, box_w, box_h)
    paper_in_box = (dark_box.x_mm + (box_w - PAPER_TARGET_MM[0]) / 2,
                    dark_box.y_mm + (box_h - PAPER_TARGET_MM[1]) / 2)
    paired = tuple(_paired_groups(dark_box, paper_in_box))
    bottom_band = dark_box.y_mm + box_h + _GAP_MM + _PATCH_MM          # below the bottom groups
    ruler_length = _STEP_MM[0] * len(NEAR_BLACK_STEPS)
    rulers = (
        _ruler(len(paired), (screen_w - ruler_length) / 2, _RULER_TOP_Y_MM, NEAR_BLACK_STEPS),
        _ruler(len(paired) + 1, _RULER_BOTTOM_X_MM, (bottom_band + screen_h - 3 * _STEP_MM[1]) / 2,
               tuple(reversed(NEAR_BLACK_STEPS))),
    )
    qr = Rect(_QR_X_MM, (bottom_band + screen_h - QR_MM) / 2, QR_MM, QR_MM)
    markers = (_corner_markers(Surface.SCREEN, SCREEN_MARKER_IDS, SCREEN_TARGET_MM, SCREEN_MARKER_MM,
                               SCREEN_MARKER_MM / MARKER_MODULES)
               + _corner_markers(Surface.PAPER, PAPER_MARKER_IDS, PAPER_TARGET_MM, PAPER_MARKER_MM, 0.0))
    return CalibrationTarget(dark_box=dark_box, qr=qr, paper_in_box=paper_in_box, paper_words=_WORDS,
                             markers=markers, groups=paired + rulers)


def screen_point(x_mm: float, y_mm: float) -> ScreenPoint:
    """What the screen target draws at (x_mm, y_mm) of its plane: a marker's module (its quiet
    zone 1.0), a patch's signal, the QR's square (`qr` True), the dark box (0.0, `raw` True), else
    the surround 0.0. Outside SCREEN_TARGET_MM: the surround. Pure; the Player's reference
    (player/geometry.py `target_signal`) and its texture are built from it."""
    if not Rect(0.0, 0.0, *SCREEN_TARGET_MM).contains(x_mm, y_mm):
        return _SURROUND
    for marker in _markers(Surface.SCREEN):
        if _quiet_zone(marker).contains(x_mm, y_mm):
            return ScreenPoint(1.0 if _marker_white(marker, x_mm, y_mm) else 0.0, raw=False, qr=False)
    for patch in _patches(Surface.SCREEN):
        if patch.rect.contains(x_mm, y_mm):
            return ScreenPoint(patch.signal, raw=False, qr=False)
    target = calibration_target()
    if target.qr.contains(x_mm, y_mm):
        return ScreenPoint(1.0, raw=False, qr=True)
    if target.dark_box.contains(x_mm, y_mm):
        return ScreenPoint(0.0, raw=True, qr=False)
    return _SURROUND


def paper_ink(x_mm: float, y_mm: float) -> bool:
    """True where the paper target prints black ink at (x_mm, y_mm) of its content's plane: a
    paper BLACK patch, a paper marker's black module. Outside PAPER_TARGET_MM: False. Pure; the
    PDF (central/displays/calibration_paper.py) is drawn from it."""
    if not Rect(0.0, 0.0, *PAPER_TARGET_MM).contains(x_mm, y_mm):
        return False
    for marker in _markers(Surface.PAPER):
        if marker.rect.contains(x_mm, y_mm):
            return not _marker_white(marker, x_mm, y_mm)
    return any(patch.role is Role.BLACK and patch.rect.contains(x_mm, y_mm) for patch in _patches(Surface.PAPER))


def code_text(code: TargetCode) -> str:
    """The QR's text: CODE_PREFIX, player_id, output_id and the decimal picture_change, joined by
    single spaces (a node token holds no space). Pure."""
    return f"{CODE_PREFIX} {code.player_id} {code.output_id} {code.picture_change}"


def parsed_code(text: str) -> TargetCode | None:
    """The code `text` names, or None when it is not exactly code_text's shape (another QR, a
    misread). Pure."""
    fields = text.split(" ") if isinstance(text, str) else []
    if len(fields) != 4 or fields[0] != CODE_PREFIX or not re.fullmatch(r"[1-9][0-9]*", fields[3]):
        return None
    try:
        return TargetCode(fields[1], fields[2], int(fields[3]))
    except ValueError:
        return None


# == v2 (decision 0027): both targets are OpenCV ChArUco boards ==================================
#
# A board is OpenCV's ChArUco board: a chessboard whose white squares each hold an ArUco marker of
# BOARD_DICTIONARY, unless the layout leaves the marker out (OpenCV's detector treats a left-out
# marker as a covered square, so measuring squares cost nothing in detection). Coordinates are
# board squares: (row, column) of a square, row 0 at the board's top, column 0 at its left; a
# point (column, row) in squares, x right, y down. Both boards are drawn with OpenCV's non-legacy
# pattern: the top-left square is black, so a square is white when row + column is odd.

BOARD_DICTIONARY: Final = "DICT_4X4_250"   # cv2.aruco's predefined dictionary of both boards' markers
BOARD_MARKER_SHARE: Final = 0.75           # a marker's side over its square's (markerLength, squareLength 1)
SQUARE_PX: Final = 16                      # the rasters' pixels a square: a marker 12 px, its 6 modules 2 px
SCREEN_ROWS: Final = 10                    # the screen board's rows, across the opening's short side
SCREEN_COLUMNS_MAX: Final = 25             # the screen board's columns; an opening shows the first
                                           # `screen_columns` of them (its 125 white squares: ids 0-124)
PAPER_ROWS: Final = 13                     # the paper board's rows (its 110 white squares: ids 125-234)
PAPER_COLUMNS: Final = 17                  # the paper board's columns
BOARD_FIRST_ID: Final[Mapping[Surface, int]] = {Surface.SCREEN: 0, Surface.PAPER: 125}

QR_PERIOD: Final = 8                       # the QR copies repeat every this many columns
QR_COPY_PLACES: Final = ((1, 1), (5, 5))   # (row, column within a period) of each copy's top-left square
QR_SQUARES: Final = 3                      # a QR copy's side in squares, its QR_QUIET_MODULES quiet zone inside
RULER_BAND: Final = (0.25, 0.75)           # the share of its row a ruler's steps fill, top to bottom;
                                           # raw 0 above and below (PLUGE's bars)


class Square(StrEnum):
    """What one board square is. MARKER: OpenCV's marker on white (the board's own drawing). WHITE:
    a white square left without its marker: screen signal 1, bare paper. BLACK: a black square:
    screen signal 0, the paper's ink (any ink). GREY: a white square left out, drawn at its
    `grey_code` (screen only). QR: a square of a QR copy (screen only; the Player draws the modules
    on light). RULER: a square of a QR copy's near-black ruler (screen only; `ruler_steps`)."""
    MARKER = "marker"
    WHITE = "white"
    BLACK = "black"
    GREY = "grey"
    QR = "qr"
    RULER = "ruler"


@dataclass(frozen=True)
class QrCopy:
    """One QR copy on the screen board: its QR fills the QR_SQUARES x QR_SQUARES squares from
    (row, column), the QR_QUIET_MODULES quiet zone inside them; its ruler is the row below
    (row + QR_SQUARES), the same columns."""
    row: int
    column: int


@dataclass(frozen=True)
class RulerStep:
    """One near-black step of a ruler: drawn at raw code `code` (of NEAR_BLACK_STEPS) over
    [left, right) x [top, bottom) in board squares."""
    code: int
    left: float
    right: float
    top: float
    bottom: float


@dataclass(frozen=True)
class Raster:
    """A board as OpenCV drew it (`board_raster`): `width` x `height` pixels at SQUARE_PX a square,
    one byte a pixel, row-major, the top row first. Screen: the encoded signal (0..255; QR squares
    255, their modules left to the Player). Paper: 0 for ink, 255 for bare paper."""
    width: int
    height: int
    pixels: bytes


def board_ids(surface: Surface) -> range:
    """The ids OpenCV's board gives its white squares, in its own order (row-major): from
    BOARD_FIRST_ID[surface], one per white square of the whole board (SCREEN_COLUMNS_MAX columns
    on the screen). Pure."""
    columns, rows = _board_size(surface)
    first = BOARD_FIRST_ID[surface]
    return range(first, first + columns * rows // 2)


def square(surface: Surface, row: int, column: int) -> Square:
    """What the square at (row, column) of `surface`'s whole board is. ValueError("square") outside
    its rows and columns (SCREEN_ROWS x SCREEN_COLUMNS_MAX, PAPER_ROWS x PAPER_COLUMNS). Pure.

    Screen, first match: QR in a copy's QR squares and RULER in its ruler row (`qr_copies` of
    SCREEN_COLUMNS_MAX); BLACK when row + column is even; else by (column + 2 * row) // 2 % 3:
    0 MARKER, 1 WHITE, 2 GREY. Paper: BLACK when row + column is even; else MARKER on even rows,
    WHITE on odd rows."""
    columns, rows = _board_size(surface)
    if not (0 <= row < rows and 0 <= column < columns):
        raise ValueError("square")
    if surface is Surface.SCREEN:
        for copy in qr_copies(SCREEN_COLUMNS_MAX):
            if copy.column <= column < copy.column + QR_SQUARES:
                if copy.row <= row < copy.row + QR_SQUARES:
                    return Square.QR
                if row == copy.row + QR_SQUARES:
                    return Square.RULER
    if (row + column) % 2 == 0:
        return Square.BLACK
    if surface is Surface.SCREEN:
        return (Square.MARKER, Square.WHITE, Square.GREY)[(column + 2 * row) // 2 % 3]
    return Square.MARKER if row % 2 == 0 else Square.WHITE


def grey_code(row: int, column: int) -> int:
    """The 8-bit code a screen GREY square is drawn at: GREY_CODES[k % len(GREY_CODES)], k the
    square's rank among the whole screen board's GREY squares taken column by column (left to
    right, each column top to bottom), so any run of columns holds every grey about equally (a
    13-column board: each at least twice) and a cropped board keeps them all.
    ValueError("grey_code") for a square that is not GREY. Pure."""
    code = _grey_codes().get((row, column))
    if code is None:
        raise ValueError("grey_code")
    return code


@functools.cache
def qr_copies(columns: int) -> tuple[QrCopy, ...]:
    """The QR copies wholly inside the board's first `columns` columns: for each period start p
    (0, QR_PERIOD, ...) and each (row, offset) of QR_COPY_PLACES, QrCopy(row, p + offset) when
    p + offset + QR_SQUARES <= columns; by column, then row. ValueError("columns") unless
    1 <= columns <= SCREEN_COLUMNS_MAX. Pure."""
    if isinstance(columns, bool) or not isinstance(columns, int) or not 1 <= columns <= SCREEN_COLUMNS_MAX:
        raise ValueError("columns")
    copies = (QrCopy(row, start + offset) for start in range(0, columns, QR_PERIOD)
              for row, offset in QR_COPY_PLACES if start + offset + QR_SQUARES <= columns)
    return tuple(sorted(copies, key=lambda copy: (copy.column, copy.row)))


def ruler_steps(copy: QrCopy) -> tuple[RulerStep, ...]:
    """The copy's ruler: one RulerStep per NEAR_BLACK_STEPS code in order, side by side across its
    QR_SQUARES columns (each QR_SQUARES / len(NEAR_BLACK_STEPS) squares wide; at SQUARE_PX a whole
    number of pixels), its top and bottom RULER_BAND of the ruler row. Pure."""
    width = QR_SQUARES / len(NEAR_BLACK_STEPS)
    top = copy.row + QR_SQUARES
    return tuple(RulerStep(code, copy.column + index * width, copy.column + (index + 1) * width,
                           top + RULER_BAND[0], top + RULER_BAND[1])
                 for index, code in enumerate(NEAR_BLACK_STEPS))


def screen_columns(aspect: float) -> int:
    """How many of the screen board's columns an opening shows: SCREEN_ROWS squares across its
    short side, so floor(aspect * SCREEN_ROWS) along its long side (a 1e-9 slack for a float
    aspect), at most SCREEN_COLUMNS_MAX. `aspect`: the opening's physical long side over its short
    side. ValueError("aspect") unless finite and >= 1. Pure."""
    if isinstance(aspect, bool) or not isinstance(aspect, int | float) or not math.isfinite(aspect) or aspect < 1:
        raise ValueError("aspect")
    return min(SCREEN_COLUMNS_MAX, math.floor(aspect * SCREEN_ROWS + 1e-9))


@functools.cache
def board_raster(surface: Surface) -> Raster:
    """`surface`'s whole board as OpenCV's CharucoBoard.generateImage drew it at SQUARE_PX a
    square (no margin, one-module marker borders), its left-out squares painted per `square`
    (WHITE 255, GREY its `grey_code`, QR 255, RULER 0 with its `ruler_steps` at their codes):
    contracts/calibration_boards.py's data, written by scripts/calibration_boards.py (imported here
    at first use; built once). Pure."""
    from contracts import calibration_boards  # data only; read once, on the first draw

    width, height, pixels = calibration_boards.SCREEN if surface is Surface.SCREEN else calibration_boards.PAPER
    return Raster(width, height, pixels)


def _board_size(surface: Surface) -> tuple[int, int]:
    """`surface`'s whole board as (columns, rows)."""
    if surface is Surface.SCREEN:
        return SCREEN_COLUMNS_MAX, SCREEN_ROWS
    return PAPER_COLUMNS, PAPER_ROWS


@functools.cache
def _grey_codes() -> Mapping[tuple[int, int], int]:
    """(row, column) -> code of every screen GREY square, ranked column by column (`grey_code`)."""
    greys = ((row, column) for column in range(SCREEN_COLUMNS_MAX) for row in range(SCREEN_ROWS)
             if square(Surface.SCREEN, row, column) is Square.GREY)
    return {place: GREY_CODES[rank % len(GREY_CODES)] for rank, place in enumerate(greys)}


def _paired_groups(dark_box: Rect, paper_in_box: tuple[float, float]) -> Iterator[Group]:
    """The PAIRED groups clockwise from the top-left: along the paper content's top (left to
    right), right (down), bottom (right to left) and left (up) edges, between the paper markers'
    quiet zones, each grey the next of GREY_SIGNALS. In a group the paper BLACK and WHITE touch the
    content's edge; the screen BLACK, WHITE and GREY face them across the dark box's edge, in the
    same clockwise order (screen BLACK and WHITE straight across from their paper twins)."""
    paper_w, paper_h = PAPER_TARGET_MM
    px, py = paper_in_box
    box_right, box_bottom = dark_box.x_mm + dark_box.width_mm, dark_box.y_mm + dark_box.height_mm
    keep_out = PAPER_MARKER_MM + PAPER_MARKER_MM / MARKER_MODULES      # a paper marker and its quiet zone
    size = _PATCH_MM

    # Each edge: (t along it clockwise, paper?) -> the patch's rect in its own plane.
    def top(t: float, paper: bool) -> Rect:
        return Rect(t, 0.0, size, size) if paper else Rect(px + t, dark_box.y_mm - _GAP_MM - size, size, size)

    def right(t: float, paper: bool) -> Rect:
        return Rect(paper_w - size, t, size, size) if paper else Rect(box_right + _GAP_MM, py + t, size, size)

    def bottom(t: float, paper: bool) -> Rect:
        x = paper_w - t - size
        return Rect(x, paper_h - size, size, size) if paper else Rect(px + x, box_bottom + _GAP_MM, size, size)

    def left(t: float, paper: bool) -> Rect:
        y = paper_h - t - size
        return Rect(0.0, y, size, size) if paper else Rect(dark_box.x_mm - _GAP_MM - size, py + y, size, size)

    index = 0
    for place, length in ((top, paper_w), (right, paper_h), (bottom, paper_w), (left, paper_h)):
        count = math.floor((length - 2 * keep_out + _GROUP_GAP_MM) / _GROUP_PITCH_MM)
        start = (length - (count * _GROUP_PITCH_MM - _GROUP_GAP_MM)) / 2
        for k in range(count):
            t = start + k * _GROUP_PITCH_MM
            yield Group(index, GroupKind.PAIRED, (
                Patch(Surface.SCREEN, Role.BLACK, place(t, False), 0.0),
                Patch(Surface.SCREEN, Role.WHITE, place(t + _PATCH_PITCH_MM, False), 1.0),
                Patch(Surface.SCREEN, Role.GREY, place(t + 2 * _PATCH_PITCH_MM, False),
                      GREY_SIGNALS[index % len(GREY_SIGNALS)]),
                Patch(Surface.PAPER, Role.BLACK, place(t, True), 0.0),
                Patch(Surface.PAPER, Role.WHITE, place(t + _PATCH_PITCH_MM, True), 1.0),
            ))
            index += 1


def _ruler(index: int, x_mm: float, y_mm: float, steps: tuple[int, ...]) -> Group:
    """A RULER group (PLUGE): the near-black steps in a row between two raw-0 BLACK bars, each step
    touching both."""
    along, across = _STEP_MM
    length = along * len(steps)
    return Group(index, GroupKind.RULER, (
        Patch(Surface.SCREEN, Role.BLACK, Rect(x_mm, y_mm, length, across), 0.0),
        *(Patch(Surface.SCREEN, Role.NEAR_BLACK, Rect(x_mm + along * i, y_mm + across, along, across),
                step / SIGNAL_LEVELS) for i, step in enumerate(steps)),
        Patch(Surface.SCREEN, Role.BLACK, Rect(x_mm, y_mm + 2 * across, length, across), 0.0),
    ))


def _corner_markers(surface: Surface, ids: tuple[int, ...], plane: tuple[float, float], side: float,
                    inset: float) -> tuple[Marker, ...]:
    """Four markers at a plane's corners, `inset` in from its edges, in TL, TR, BR, BL order."""
    near, far_x, far_y = inset, plane[0] - inset - side, plane[1] - inset - side
    places = ((near, near), (far_x, near), (far_x, far_y), (near, far_y))
    return tuple(Marker(surface, marker_id, Rect(x, y, side, side))
                 for marker_id, (x, y) in zip(ids, places, strict=True))


@functools.cache
def _markers(surface: Surface) -> tuple[Marker, ...]:
    return tuple(marker for marker in calibration_target().markers if marker.surface is surface)


@functools.cache
def _patches(surface: Surface) -> tuple[Patch, ...]:
    return tuple(patch for group in calibration_target().groups for patch in group.patches
                 if patch.surface is surface)


def _quiet_zone(marker: Marker) -> Rect:
    """A screen marker's rect grown by its one-module quiet zone."""
    module = marker.rect.width_mm / MARKER_MODULES
    rect = marker.rect
    return Rect(rect.x_mm - module, rect.y_mm - module, rect.width_mm + 2 * module, rect.height_mm + 2 * module)


def _marker_white(marker: Marker, x_mm: float, y_mm: float) -> bool:
    """Whether a marker's module at the point is white: its quiet zone and its '1' bits; the
    one-module border and the '0' bits are black."""
    if not marker.rect.contains(x_mm, y_mm):
        return True
    module = marker.rect.width_mm / MARKER_MODULES
    column = min(MARKER_MODULES - 1, int((x_mm - marker.rect.x_mm) // module))
    row = min(MARKER_MODULES - 1, int((y_mm - marker.rect.y_mm) // module))
    if column in (0, MARKER_MODULES - 1) or row in (0, MARKER_MODULES - 1):
        return False
    return MARKER_BITS[marker.marker_id][row - 1][column - 1] == "1"


__all__ = [
    "A4_MM", "BOX_SLACK_MM", "CODE_PREFIX", "DARK_BOX_MM", "GREY_CODES", "GREY_COPIES", "GREY_SIGNALS", "LETTER_MM",
    "MARKER_BITS", "MARKER_DICTIONARY", "MARKER_MODULES", "NEAR_BLACK_COPIES", "NEAR_BLACK_STEPS",
    "PAIR_MM", "PAPER_MARKER_IDS", "PAPER_MARKER_MM", "PAPER_TARGET_MM", "PRINT_MARGIN_MM", "QR_ERROR",
    "QR_MM", "QR_QUIET_MODULES", "SCREEN_MARKER_IDS", "SCREEN_MARKER_MM", "SCREEN_TARGET_MM", "SHEETS_MM",
    "WHITE_COPIES", "CalibrationTarget", "Group", "GroupKind", "Marker", "PaperSize",
    "Patch", "QrModules", "Rect", "Role", "ScreenPoint", "Surface", "TargetCode", "calibration_target",
    "code_text", "paper_ink", "parsed_code", "screen_point",
    # v2 (decision 0027)
    "BOARD_DICTIONARY", "BOARD_FIRST_ID", "BOARD_MARKER_SHARE", "PAPER_COLUMNS", "PAPER_ROWS", "QR_COPY_PLACES",
    "QR_PERIOD", "QR_SQUARES", "RULER_BAND", "SCREEN_COLUMNS_MAX", "SCREEN_ROWS", "SQUARE_PX", "QrCopy", "Raster",
    "RulerStep", "Square", "board_ids", "board_raster", "grey_code", "qr_copies", "ruler_steps", "screen_columns",
    "square",
]
