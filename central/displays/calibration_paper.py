"""The paper calibration target as a PDF to print (decisions 0026, 0027; owner, 2026-10-13: "photo-wall
could give me a printable asset"). Pure: no I/O; Pillow scales the board and writes the PDF in memory.

v2: the sheet carries the paper ChArUco board OpenCV drew at build time
(contracts.calibration_target.board_raster(Surface.PAPER)), each square PAPER_SQUARE_MM at 100 %,
centred on one landscape sheet (SHEETS_MM) at PAPER_DPI. Nothing on it assumes black ink or a scale
(owner, 2026-10-14: "My black ink is running low and printing gray-ish"): the reader measures the
board in squares from its own markers, so any printer, any ink and any print scale reads, and a
piece cut from it serves a screen smaller than the sheet. No words on the sheet: the instructions'
one home is the Picture tab.

Prior art: OpenCV's ChArUco board (CharucoBoard.generateImage), printed as OpenCV's own calibration
boards are; Pillow's PDF writer (a one-bit image is stored CCITT G4). Its route:
central/calibration_target_routes.py.
"""
from __future__ import annotations

import io
from typing import Final

from PIL import Image

from contracts.calibration_target import (
    PAPER_COLUMNS,
    PAPER_ROWS,
    SHEETS_MM,
    PaperSize,
    Surface,
    board_raster,
)

PAPER_DPI: Final = 600          # the raster's dots per inch: a dot is about 0.04 mm
PAPER_SQUARE_MM: Final = 15.0   # a board square at 100 %: the board 255 x 195 mm, inside both sheets less
                                # PRINT_MARGIN_MM; any print scale reads (squares, never millimetres)
PDF_MEDIA_TYPE: Final = "application/pdf"

_MM_PER_INCH: Final = 25.4
_SHEET_NAMES: Final = {PaperSize.LETTER: "Letter", PaperSize.A4: "A4"}
_INK_BELOW: Final = 128         # an 'L' board pixel below this is ink


def paper_raster(paper: PaperSize) -> Image.Image:
    """The sheet `paper` as a mode "1" image at PAPER_DPI (width and height round(mm / 25.4 *
    PAPER_DPI)), white, with the paper board (board_raster(Surface.PAPER)) scaled by nearest
    neighbour to PAPER_COLUMNS x PAPER_ROWS squares of PAPER_SQUARE_MM, thresholded at 128 and
    pasted centred. Pure."""
    sheet_w, sheet_h = SHEETS_MM[paper]
    sheet = Image.new("1", (_dots(sheet_w), _dots(sheet_h)), 1)
    raster = board_raster(Surface.PAPER)
    board = Image.frombytes("L", (raster.width, raster.height), raster.pixels).resize(
        (_dots(PAPER_COLUMNS * PAPER_SQUARE_MM), _dots(PAPER_ROWS * PAPER_SQUARE_MM)),
        Image.Resampling.NEAREST,
    ).point(lambda value: 255 if value >= _INK_BELOW else 0, mode="1")
    sheet.paste(board, ((sheet.width - board.width) // 2, (sheet.height - board.height) // 2))
    return sheet


def paper_pdf(paper: PaperSize) -> bytes:
    """paper_raster(paper) as a one-page PDF whose page is the sheet's size (Pillow's PDF writer at
    resolution PAPER_DPI), titled "Photo Wall calibration target (<Letter|A4>)". The same bytes for
    one paper size every time (no creation or modification date in the file). Pure."""
    out = io.BytesIO()
    paper_raster(paper).save(out, "PDF", resolution=PAPER_DPI,
                             title=f"Photo Wall calibration target ({_SHEET_NAMES[paper]})",
                             creationDate=None, modDate=None)
    return out.getvalue()


def _dots(mm: float) -> int:
    return round(mm / _MM_PER_INCH * PAPER_DPI)


__all__ = ["PAPER_DPI", "PAPER_SQUARE_MM", "PDF_MEDIA_TYPE", "paper_pdf", "paper_raster"]
