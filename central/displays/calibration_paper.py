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

import io
import math
from collections.abc import Iterable
from functools import cache
from typing import Final

from PIL import Image, ImageDraw, ImageFont

from contracts.calibration_target import (
    MARKER_MODULES,
    PAPER_TARGET_MM,
    SCREEN_TARGET_MM,
    SHEETS_MM,
    PaperSize,
    Rect,
    Surface,
    calibration_target,
    paper_ink,
)

PAPER_DPI: Final = 600                     # the raster's dots per inch: a dot is about 0.04 mm
PDF_MEDIA_TYPE: Final = "application/pdf"

_MM_PER_INCH: Final = 25.4
_SHEET_NAMES: Final = {PaperSize.LETTER: "Letter", PaperSize.A4: "A4"}
_LINE_SPACING: Final = 1.3                          # a line's height, in font sizes
_MOST_FONT_PX: Final = round(14 / 72 * PAPER_DPI)   # 14 pt: the words never grow past it
_LEAST_FONT_PX: Final = round(7 / 72 * PAPER_DPI)   # 7 pt: below it the words are not legible in print
_WHITE: Final = 1
_BLACK: Final = 0


def paper_raster(paper: PaperSize) -> Image.Image:
    """The sheet `paper` as a mode "1" image at PAPER_DPI (width and height round(mm / 25.4 *
    PAPER_DPI)), white, with the paper target's content centred: each dot black where
    contracts.calibration_target.paper_ink is True at the dot's centre in the content's plane, and
    the words (Pillow's default font at a size legible in print) inside `paper_words`. Pure.

    paper_ink is asked once per cell between the layout's paper edges (the content's sides, its
    patches' sides and its markers' module lines), the only places its answer can change, and the
    dots whose centres lie in the cell take that answer."""
    sheet_w, sheet_h = SHEETS_MM[paper]
    image = Image.new("1", (_dots(sheet_w), _dots(sheet_h)), _WHITE)
    offset = ((sheet_w - PAPER_TARGET_MM[0]) / 2, (sheet_h - PAPER_TARGET_MM[1]) / 2)
    draw = ImageDraw.Draw(image)
    xs, ys = _edges()
    for x0, x1 in zip(xs, xs[1:], strict=False):
        first_x, end_x = _dot_span(x0 + offset[0], x1 + offset[0])
        for y0, y1 in zip(ys, ys[1:], strict=False):
            first_y, end_y = _dot_span(y0 + offset[1], y1 + offset[1])
            if first_x < end_x and first_y < end_y and paper_ink((x0 + x1) / 2, (y0 + y1) / 2):
                draw.rectangle((first_x, first_y, end_x - 1, end_y - 1), fill=_BLACK)
    _draw_words(draw, _words(paper), calibration_target().paper_words, offset)
    return image


def paper_pdf(paper: PaperSize) -> bytes:
    """paper_raster(paper) as a one-page PDF whose page is the sheet's size (Pillow's PDF writer at
    resolution PAPER_DPI), titled "Photo Wall calibration target (<Letter|A4>)". The same bytes for
    one paper size every time (no creation date in the file). Pure."""
    out = io.BytesIO()
    paper_raster(paper).save(out, "PDF", resolution=PAPER_DPI,
                             title=f"Photo Wall calibration target ({_SHEET_NAMES[paper]})",
                             creationDate=None, modDate=None)
    return out.getvalue()


def _words(paper: PaperSize) -> tuple[str, ...]:
    """The three lines the sheet carries, written from the constants."""
    width_cm, height_cm = (f"{mm / 10:g}" for mm in SCREEN_TARGET_MM)
    return (f"Photo Wall calibration target: {_SHEET_NAMES[paper]} sheet, landscape",
            "Print at 100 % (actual size), black ink only",
            f"The Frame's opening must be at least {width_cm} x {height_cm} cm (either way round)")


@cache
def _edges() -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Every x and every y (mm, the content's plane) where the paper's ink can change."""
    target = calibration_target()
    xs: set[float] = {0.0, PAPER_TARGET_MM[0]}
    ys: set[float] = {0.0, PAPER_TARGET_MM[1]}
    for rect in (patch.rect for group in target.groups for patch in group.patches if patch.surface is Surface.PAPER):
        xs.update((rect.x_mm, rect.x_mm + rect.width_mm))
        ys.update((rect.y_mm, rect.y_mm + rect.height_mm))
    for marker in target.markers:
        if marker.surface is Surface.PAPER:
            xs.update(_module_lines(marker.rect.x_mm, marker.rect.width_mm))
            ys.update(_module_lines(marker.rect.y_mm, marker.rect.height_mm))
    return tuple(sorted(xs)), tuple(sorted(ys))


def _module_lines(start_mm: float, side_mm: float) -> Iterable[float]:
    return (start_mm + side_mm * k / MARKER_MODULES for k in range(MARKER_MODULES + 1))


def _dots(mm: float) -> int:
    return round(mm / _MM_PER_INCH * PAPER_DPI)


def _dot_span(start_mm: float, end_mm: float) -> tuple[int, int]:
    """The dots [first, end) of the sheet whose centres lie in [start_mm, end_mm)."""
    return _first_dot_from(start_mm), _first_dot_from(end_mm)


def _first_dot_from(mm: float) -> int:
    """The first dot whose centre ((dot + 0.5) / PAPER_DPI inches) lies at or after `mm`."""
    return math.ceil(mm / _MM_PER_INCH * PAPER_DPI - 0.5)


def _draw_words(draw: ImageDraw.ImageDraw, lines: tuple[str, ...], box: Rect, offset: tuple[float, float]) -> None:
    """`lines` centred in `box` (the content's plane), all at one size, the largest from
    _MOST_FONT_PX down that fits. ValueError("paper_words") when even _LEAST_FONT_PX does not fit
    (the layout's free middle is too small for the words)."""
    left, top = _dots(box.x_mm + offset[0]), _dots(box.y_mm + offset[1])
    right, bottom = _dots(box.x_mm + box.width_mm + offset[0]), _dots(box.y_mm + box.height_mm + offset[1])
    for size in range(_MOST_FONT_PX, _LEAST_FONT_PX - 1, -2):
        font = ImageFont.load_default(size)
        boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
        line_px = math.ceil(size * _LINE_SPACING)
        if max(b[2] - b[0] for b in boxes) <= right - left and line_px * len(lines) <= bottom - top:
            break
    else:
        raise ValueError("paper_words")
    y = top + (bottom - top - line_px * len(lines)) // 2
    for line, (x0, _y0, x1, _y1) in zip(lines, boxes, strict=True):
        draw.text((left + (right - left - (x1 - x0)) // 2 - x0, y), line, font=font, fill=_BLACK)
        y += line_px


__all__ = ["PAPER_DPI", "PDF_MEDIA_TYPE", "paper_pdf", "paper_raster"]
