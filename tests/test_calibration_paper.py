"""The paper calibration target to print (decisions 0026, 0027; owner, 2026-10-13: "photo-wall could
give me a printable asset"; 2026-10-14: "Can I print it on a 4x6 photo printer?"): the sheets
scripts/calibration_boards.py draws and commits (central/displays/calibration_paper.py names them).

(CA1) each sheet is its size at PAPER_DPI, its piece of the board (`sheet_squares`) centred inside
the printer's margin at PAPER_SQUARE_MM a square; (CA2) the reader's own ChArUco board finds the
printed board, whole, as the 4x6 sheet's piece and as a small cut piece; (CA3) only black and white
dots; (CA4) the PDF is one page the sheet's size, titled, the same bytes every time.
"""
from __future__ import annotations

import re

import cv2
import numpy as np
import pytest
from PIL import Image, ImageOps, PdfParser

from contracts.calibration_target import (
    PAPER_COLUMNS,
    PAPER_ROWS,
    PAPER_SQUARE_MM,
    PRINT_MARGIN_MM,
    SHEETS_MM,
    PaperSize,
    Surface,
    board_raster,
    sheet_squares,
)
from scripts.calibration_boards import PAPER_DPI, sheet_image, sheet_pdf
from scripts.calibration_target.boards import charuco_board, marker_squares

POINTS = {PaperSize.LETTER: (792.0, 612.0), PaperSize.A4: (841.89, 595.28), PaperSize.FOUR_BY_SIX: (432.0, 288.0)}
TITLES = {PaperSize.LETTER: "Letter", PaperSize.A4: "A4", PaperSize.FOUR_BY_SIX: "4 x 6 in"}
READ_REDUCTION = 4             # 600 DPI to 150 DPI: what a phone photo of the sheet resolves, roughly
INNER_CORNERS = (PAPER_COLUMNS - 1) * (PAPER_ROWS - 1)


def _dots(mm: float, dpi: float = PAPER_DPI) -> int:
    return round(mm / 25.4 * dpi)


def paper_raster(paper: PaperSize):
    return sheet_image(paper, board_raster(Surface.PAPER))


def paper_pdf(paper: PaperSize) -> bytes:
    return sheet_pdf(paper, board_raster(Surface.PAPER))


@pytest.fixture(scope="module", params=list(PaperSize))
def sheet(request):
    """(paper, the raster, a bool array True where inked)."""
    image = paper_raster(request.param)
    return request.param, image, ~np.array(image, dtype=bool)


def _detect(image: Image.Image):
    """OpenCV's ChArUco detection of the paper board in an 'L' image: (marker ids, ChArUco corner ids)."""
    parameters = cv2.aruco.CharucoParameters()
    parameters.minMarkers = 1
    detector = cv2.aruco.CharucoDetector(charuco_board(Surface.PAPER), parameters)
    corners, corner_ids, _marker_corners, marker_ids = detector.detectBoard(np.array(image))
    as_set = lambda ids: set() if ids is None else {int(i) for i in ids.ravel()}  # noqa: E731
    return as_set(marker_ids), as_set(corner_ids)


def test_each_sheet_is_its_size_and_the_board_is_centred_inside_the_margin(sheet):
    """CA1. Probe C1-centre: the board pasted at (0, 0) -> not centred. Probe C1-size: PAPER_SQUARE_MM
    16.0 -> the A4 board reaches into the printer's margin."""
    paper, image, inked = sheet
    sheet_w, sheet_h = SHEETS_MM[paper]
    assert image.size == (_dots(sheet_w), _dots(sheet_h))
    # The piece's corner squares are ink (an odd number of columns and rows from the board's black
    # top-left), so the ink's bounding box is the piece.
    columns_kept, rows_kept = sheet_squares(paper)
    rows, columns = np.flatnonzero(inked.any(axis=1)), np.flatnonzero(inked.any(axis=0))
    top, bottom, left, right = rows[0], rows[-1] + 1, columns[0], columns[-1] + 1
    assert (right - left, bottom - top) == (_dots(columns_kept * PAPER_SQUARE_MM),
                                            _dots(rows_kept * PAPER_SQUARE_MM))
    assert abs(left - (image.width - right)) <= 1 and abs(top - (image.height - bottom)) <= 1
    margin = _dots(PRINT_MARGIN_MM)
    assert min(left, top, image.width - right, image.height - bottom) >= margin


def test_the_readers_board_finds_the_printed_board_whole_and_cut_small():
    """CA2: the Letter sheet at 150 DPI shows every marker and nearly every inner corner; a 4 x 4
    square piece cut from it, a quarter square of bare paper round it, still shows a marker (a screen
    smaller than the sheet). Probe C1-invert: the raster inverted -> no marker is found."""
    image = paper_raster(PaperSize.LETTER).convert("L").reduce(READ_REDUCTION)
    markers, corners = _detect(image)
    assert markers == set(marker_squares(Surface.PAPER))
    assert len(corners) >= 180, len(corners)

    square = PAPER_SQUARE_MM / 25.4 * PAPER_DPI / READ_REDUCTION
    left = (image.width - PAPER_COLUMNS * square) / 2
    top = (image.height - PAPER_ROWS * square) / 2
    first_row, first_column = 4, 6
    piece = image.crop((round(left + first_column * square), round(top + first_row * square),
                        round(left + (first_column + 4) * square), round(top + (first_row + 4) * square)))
    piece = ImageOps.expand(piece, border=round(square / 4), fill=255)
    found, _corners = _detect(piece)
    inside = {marker_id for marker_id, (row, column) in marker_squares(Surface.PAPER).items()
              if first_row <= row < first_row + 4 and first_column <= column < first_column + 4}
    assert found and found <= inside, (found, inside)


def test_the_sheets_carry_the_whole_board_or_the_4x6_piece():
    """A small sheet carries fewer squares, never smaller ones (owner, 2026-10-14: "Can I print it on
    a 4x6 photo printer?"). Probe C1-fit: the margin left out of sheet_squares -> 4x6 holds 10 x 6."""
    assert sheet_squares(PaperSize.LETTER) == sheet_squares(PaperSize.A4) == (PAPER_COLUMNS, PAPER_ROWS)
    assert sheet_squares(PaperSize.FOUR_BY_SIX) == (9, 5)


def test_the_readers_board_finds_the_4x6_sheets_piece():
    """CA2 for the 4x6 sheet at 150 DPI: every marker of its 9 x 5 piece, and only those (the reader
    reads it as any cut piece of the one paper board). Probe C1-crop: the piece cropped from the
    board's middle -> other ids are found."""
    image = paper_raster(PaperSize.FOUR_BY_SIX).convert("L").reduce(READ_REDUCTION)
    markers, corners = _detect(image)
    columns, rows = sheet_squares(PaperSize.FOUR_BY_SIX)
    assert markers == {marker_id for marker_id, (row, column) in marker_squares(Surface.PAPER).items()
                       if row < rows and column < columns}
    assert len(corners) >= (columns - 1) * (rows - 1) - 2, len(corners)


def test_only_black_and_white_dots(sheet):
    """CA3: any printer and any ink print it the same way: no grey for a driver to dither. Probe
    C1-grey: the raster converted to 'L' before saving -> the mode and the PDF's CCITT encoding fail."""
    paper, image, _inked = sheet
    assert image.mode == "1"
    assert set(np.unique(np.array(image.convert("L")))) <= {0, 255}
    assert b"/CCITTFaxDecode" in paper_pdf(paper)


@pytest.mark.parametrize("paper", list(PaperSize))
def test_the_pdf_is_one_landscape_page_the_sheets_size_titled_the_same_bytes_every_time(paper):
    """CA4. Probe C1-date: a creationDate passed -> the PDF carries /CreationDate and its bytes
    change from one call to the next."""
    first, second = paper_pdf(paper), paper_pdf(paper)
    assert first.startswith(b"%PDF-") and first == second
    assert len(re.findall(rb"/Type\s*/Page\b", first)) == 1
    [box] = re.findall(rb"/MediaBox\s*\[\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\]", first)
    left, bottom, right, top = (float(value) for value in box)
    width, height = POINTS[paper]
    assert abs(right - left - width) <= 0.5 and abs(top - bottom - height) <= 0.5
    info = PdfParser.PdfParser(buf=first).info
    assert PdfParser.decode_text(info[b"Title"]) == f"Photo Wall calibration target ({TITLES[paper]})"
    assert b"CreationDate" not in info and b"ModDate" not in info
