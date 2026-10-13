"""The paper calibration target as a PDF to print (decision 0026; owner, 2026-10-13: "photo-wall could
give me a printable asset"). Pure: no I/O; Pillow draws and writes it in memory.

The page is one landscape sheet (contracts/calibration_target.py SHEETS_MM) at PAPER_DPI, black ink
only (a one-bit image, so no printer dithers a grey), the paper target's content
(PAPER_TARGET_MM, drawn from contracts.calibration_target.paper_ink) centred on it, and three lines
of words in the content's free middle (CalibrationTarget.paper_words, where no patch is read): the
sheet, "Print at 100 % (actual size), black ink only", and the opening the screen target needs
(SCREEN_TARGET_MM in cm, either way round). The PDF's page is the sheet's size in points
(1/72 inch), so a print at actual size puts every mark where the layout says; a print scaled by
the printer is what the reader's `scale-mismatch` refusal catches.

Prior art: Pillow's PDF writer (a one-bit image is stored CCITT G4); OpenCV's ArUco boards print the
same way. Its route: central/calibration_target_routes.py.
"""
from __future__ import annotations

from typing import Final

from PIL import Image

from contracts.calibration_target import PaperSize

PAPER_DPI: Final = 600                     # the raster's dots per inch: a dot is about 0.04 mm
PDF_MEDIA_TYPE: Final = "application/pdf"


def paper_raster(paper: PaperSize) -> Image.Image:
    """The sheet `paper` as a mode "1" image at PAPER_DPI (width and height round(mm / 25.4 *
    PAPER_DPI)), white, with the paper target's content centred: each dot black where
    contracts.calibration_target.paper_ink is True at the dot's centre in the content's plane, and
    the words (Pillow's default font at a size legible in print) inside `paper_words`. Pure."""
    raise NotImplementedError("calibration targets: slice K2")


def paper_pdf(paper: PaperSize) -> bytes:
    """paper_raster(paper) as a one-page PDF whose page is the sheet's size (Pillow's PDF writer at
    resolution PAPER_DPI), titled "Photo Wall calibration target (<Letter|A4>)". The same bytes for
    one paper size every time (no creation date in the file). Pure."""
    raise NotImplementedError("calibration targets: slice K2")


__all__ = ["PAPER_DPI", "PDF_MEDIA_TYPE", "paper_pdf", "paper_raster"]
