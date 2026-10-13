"""The paper calibration target to print (decision 0026; owner, 2026-10-13: "photo-wall could give me
a printable asset"): central/displays/calibration_paper.py.

(1) to scale: the raster is the sheet at PAPER_DPI and OpenCV's ArUco detector finds the paper
markers where the layout puts them, the content centred; (2) black ink only, where the layout says;
(3) the PDF prints at actual size, the same bytes every time.
"""
from __future__ import annotations

import math
import re

import cv2
import numpy as np
import pytest

from central.displays.calibration_paper import PAPER_DPI, paper_pdf, paper_raster
from contracts.calibration_target import (
    MARKER_BITS,
    MARKER_DICTIONARY,
    MARKER_MODULES,
    PAPER_MARKER_IDS,
    PAPER_TARGET_MM,
    SHEETS_MM,
    PaperSize,
    Rect,
    Role,
    Surface,
    calibration_target,
)

MM_PER_DOT = 25.4 / PAPER_DPI
POINTS = {PaperSize.LETTER: (792.0, 612.0), PaperSize.A4: (841.89, 595.28)}


@pytest.fixture(scope="module", params=list(PaperSize))
def sheet(request):
    """(paper, the raster as a bool array True where black, the content's offset on the sheet in mm)."""
    paper = request.param
    image = paper_raster(paper)
    assert image.mode == "1"                                           # probe K2-grey
    sheet_w, sheet_h = SHEETS_MM[paper]
    offset = ((sheet_w - PAPER_TARGET_MM[0]) / 2, (sheet_h - PAPER_TARGET_MM[1]) / 2)
    return paper, image, ~np.array(image, dtype=bool), offset


def _dot(mm: float) -> int:
    """The dot whose area holds `mm` (its centre's neighbourhood)."""
    return math.floor(mm / MM_PER_DOT)


def _black_at(black: np.ndarray, offset, x_mm: float, y_mm: float) -> bool:
    return bool(black[_dot(y_mm + offset[1]), _dot(x_mm + offset[0])])


def _paper_patches():
    return [patch for group in calibration_target().groups for patch in group.patches
            if patch.surface is Surface.PAPER]


def _paper_markers():
    return [marker for marker in calibration_target().markers if marker.surface is Surface.PAPER]


def test_the_raster_is_the_sheet_at_its_dpi_and_the_markers_sit_where_the_layout_says(sheet):
    """(1) Acceptance 1. Mutation probe K2-offset: the content at the sheet's corner -> the markers
    are found 7 to 10 mm off."""
    paper, image, _black, offset = sheet
    assert image.size == tuple(round(mm / 25.4 * PAPER_DPI) for mm in SHEETS_MM[paper])
    detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, MARKER_DICTIONARY)))
    corners, ids, _rejected = detector.detectMarkers(np.array(image.convert("L")))
    assert ids is not None
    found = {int(marker_id): quad.reshape(4, 2) * MM_PER_DOT for marker_id, quad in zip(ids.ravel(), corners,
                                                                                         strict=True)}
    assert sorted(found) == sorted(PAPER_MARKER_IDS)
    for marker in _paper_markers():
        rect = marker.rect
        x0, y0 = rect.x_mm + offset[0], rect.y_mm + offset[1]
        x1, y1 = x0 + rect.width_mm, y0 + rect.height_mm
        expected = np.array([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])      # ArUco's corner order
        assert np.abs(found[marker.marker_id] - expected).max() <= 0.5, marker.marker_id


def _module_centres(rect: Rect):
    side = rect.width_mm / MARKER_MODULES
    for row in range(MARKER_MODULES):
        for column in range(MARKER_MODULES):
            yield row, column, rect.x_mm + (column + 0.5) * side, rect.y_mm + (row + 0.5) * side


def test_black_ink_only_where_the_layout_says(sheet):
    """(2) Acceptance 2."""
    _paper, _image, black, offset = sheet
    for patch in _paper_patches():
        if patch.role in (Role.BLACK, Role.WHITE):
            assert _black_at(black, offset, *patch.rect.centre()) is (patch.role is Role.BLACK), patch
    for marker in _paper_markers():
        bits = MARKER_BITS[marker.marker_id]
        for row, column, x_mm, y_mm in _module_centres(marker.rect):
            border = row in (0, MARKER_MODULES - 1) or column in (0, MARKER_MODULES - 1)
            wanted = border or bits[row - 1][column - 1] == "0"
            assert _black_at(black, offset, x_mm, y_mm) is wanted, (marker.marker_id, row, column)

    # Nothing black outside the content; inside it only on the layout's ink or among the words.
    allowed = np.zeros_like(black)
    target = calibration_target()
    inked = [patch.rect for patch in _paper_patches() if patch.role is Role.BLACK]
    for rect in (*inked, *(marker.rect for marker in _paper_markers()), target.paper_words):
        top, left = _dot(rect.y_mm + offset[1]) - 1, _dot(rect.x_mm + offset[0]) - 1
        bottom, right = _dot(rect.y_mm + rect.height_mm + offset[1]) + 2, _dot(rect.x_mm + rect.width_mm + offset[0]) + 2
        allowed[top:bottom, left:right] = True
    assert not (black & ~allowed).any()
    words = target.paper_words
    in_words = black[_dot(words.y_mm + offset[1]):_dot(words.y_mm + words.height_mm + offset[1]),
                     _dot(words.x_mm + offset[0]):_dot(words.x_mm + words.width_mm + offset[0])]
    assert in_words.any()                                              # the words are printed


@pytest.mark.parametrize("paper", list(PaperSize))
def test_the_pdf_is_one_page_the_sheets_size_the_same_bytes_every_time(paper):
    """(3) Acceptance 3. Mutation probe K2-dpi: saved at resolution 300 -> the MediaBox doubles."""
    first, second = paper_pdf(paper), paper_pdf(paper)
    assert first.startswith(b"%PDF-") and first == second
    assert len(re.findall(rb"/Type\s*/Page\b", first)) == 1
    [box] = re.findall(rb"/MediaBox\s*\[\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\]", first)
    left, bottom, right, top = (float(value) for value in box)
    width, height = POINTS[paper]
    assert abs(right - left - width) <= 0.5 and abs(top - bottom - height) <= 0.5
    assert b"/CCITTFaxDecode" in first                                # one bit a dot: no JPEG greys
    assert b"/CreationDate" not in first
