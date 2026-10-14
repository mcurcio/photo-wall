"""The calibration targets' layout and code (decisions 0026 and 0027; owner, 2026-10-13: "build the
calibration targets"; 2026-10-14: "Start working on a v2 that isn’t so picky"). Stdlib only: the
Pi's Player draws the screen board from it.

Two targets read from one phone photo by the agent's reader (scripts/calibration_target/), both
OpenCV ChArUco boards (cv2.aruco.CharucoBoard; prior art), so any part of either, at any scale,
turn or print, locates itself:
- the **screen board**, drawn by the Player in a Frame's opening while the picture session's test
  pattern is a target (contracts/tuning.py TARGET_PATTERNS): SCREEN_ROWS squares across the
  opening's short side and the first `screen_columns` columns along its long side, with whites,
  greys, QR copies naming the port's picture change and their near-black rulers;
- the **paper board**, printed from Central's committed sheets (central/displays/calibration_paper.py) on any printer,
  in any ink, at any size, laid whole or as a cut piece anywhere on the screen, under the glass
  when there is one, with a black card behind it.

Rules this module owns (one home each): both boards' constants, which square is what (`square`,
`grey_code`), the QR copies and their rulers (`qr_copies`, `ruler_steps`), how many columns an
opening shows (`screen_columns`), the sheets and the piece of the paper board each carries
(`sheet_squares`), the code's text (`code_text`, `parsed_code`), and the two rasters OpenCV drew
at build time (`board_raster`; scripts/calibration_boards.py writes them into
contracts/calibration_boards.py, a test regenerates and compares them). The Pi never imports
OpenCV. Rules it does NOT own: the cv2 board objects (scripts/calibration_target/boards.py), how
the Player maps an opening to the board and which stage is raw (player/geometry.py `board_point`,
`board_signal`), the QR's modules (player/calibration_target.py `target_modules`, segno), the
printed sheets (drawn by scripts/calibration_boards.py, committed where
central/displays/calibration_paper.py names them), how a photo is read (scripts/calibration_target/).
"""
from __future__ import annotations

import functools
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from contracts.node_protocol import counter, token

# -- Sheets (millimetres: the PDF's page; the boards themselves are measured in squares) --

LETTER_MM: Final = (279.4, 215.9)          # a US Letter sheet, landscape
A4_MM: Final = (297.0, 210.0)              # an ISO A4 sheet, landscape
FOUR_BY_SIX_MM: Final = (152.4, 101.6)     # a 4 x 6 inch photo print, landscape
PRINT_MARGIN_MM: Final = 6.35              # the least unprinted edge a home printer leaves
PAPER_SQUARE_MM: Final = 15.0              # a paper board square at 100 %, on every sheet (owner, 2026-10-14:
                                           # "Can I print it on a 4x6 photo printer?": a small sheet
                                           # carries fewer squares, never smaller ones)

# -- The values a target carries --

GREY_CODES: Final = (179, 128, 77, 38, 13)   # the screen greys, 8-bit codes of SIGNAL_LEVELS (about
                                             # 0.70, 0.50, 0.30, 0.15, 0.05): on the grid the screen
                                             # raster stores, so drawn, reference and reader agree
NEAR_BLACK_STEPS: Final = tuple(range(1, 17))           # raw codes k of 255 beside raw 0, on each ruler

# -- The code (a QR, the owner's idea; OpenCV's detector reads it) --

CODE_PREFIX: Final = "PWCT2"               # the code text's first word: Photo Wall calibration target, v2
QR_ERROR: Final = "m"                      # segno's error correction level (15 %)
QR_QUIET_MODULES: Final = 4                # QR's quiet zone (ISO/IEC 18004), drawn white


class Surface(StrEnum):
    """Which target (board) a square is on."""
    SCREEN = "screen"
    PAPER = "paper"


class PaperSize(StrEnum):
    """The sheet the paper target is printed on, landscape (the PDF route's `paper`)."""
    LETTER = "letter"
    A4 = "a4"
    FOUR_BY_SIX = "4x6"


SHEETS_MM: Final[Mapping[PaperSize, tuple[float, float]]] = {
    PaperSize.LETTER: LETTER_MM, PaperSize.A4: A4_MM, PaperSize.FOUR_BY_SIX: FOUR_BY_SIX_MM}


@dataclass(frozen=True)
class TargetCode:
    """What the screen board's QR copies name: the port and its picture change, the key of Central's
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


# -- The boards (OpenCV's ChArUco boards) --
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


def sheet_squares(paper: PaperSize) -> tuple[int, int]:
    """(columns, rows) of the paper board that `paper` carries: its top-left piece (from row 0 and
    column 0, so OpenCV's own pattern and marker ids hold and the reader reads it as any cut piece),
    as many PAPER_SQUARE_MM squares each way as fit the sheet less PRINT_MARGIN_MM on each side, at
    most the whole board (PAPER_COLUMNS x PAPER_ROWS). Letter and A4: the whole 17 x 13; 4x6: 9 x 5.
    Pure."""
    width, height = SHEETS_MM[paper]
    fit = lambda mm: math.floor((mm - 2 * PRINT_MARGIN_MM) / PAPER_SQUARE_MM)  # noqa: E731
    return min(PAPER_COLUMNS, fit(width)), min(PAPER_ROWS, fit(height))


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


__all__ = [
    "A4_MM", "BOARD_DICTIONARY", "BOARD_FIRST_ID", "BOARD_MARKER_SHARE", "CODE_PREFIX", "FOUR_BY_SIX_MM", "GREY_CODES",
    "LETTER_MM", "NEAR_BLACK_STEPS", "PAPER_COLUMNS", "PAPER_ROWS", "PAPER_SQUARE_MM", "PRINT_MARGIN_MM", "QR_COPY_PLACES", "QR_ERROR", "QR_PERIOD",
    "QR_QUIET_MODULES", "QR_SQUARES", "RULER_BAND", "SCREEN_COLUMNS_MAX", "SCREEN_ROWS", "SHEETS_MM", "SQUARE_PX",
    "PaperSize", "QrCopy", "QrModules", "Raster", "RulerStep", "Square", "Surface", "TargetCode", "board_ids",
    "board_raster", "code_text", "grey_code", "parsed_code", "qr_copies", "ruler_steps", "screen_columns",
    "sheet_squares", "square",
]
