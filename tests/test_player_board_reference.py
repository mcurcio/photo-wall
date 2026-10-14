"""The screen board in the Player's scalar reference (player/geometry.py `board_draw`,
`board_point`, `board_signal`, decision 0027): SCREEN_ROWS squares across the opening's short
side, the shown columns centred along its long side, a quarter turn clockwise in a portrait
opening; each point the generated raster or a QR copy's module. And the seam the calibration
tracer rides on: a board the Player draws in a 5-inch opening is found by OpenCV's ChArUco
detector and its QR decodes. Pure: the shader is held to this by
tests/native_renderer_pixel_harness.py."""
from __future__ import annotations

import itertools
import math

import cv2
import numpy as np
import pytest

from contracts.calibration_target import (
    QR_QUIET_MODULES,
    QR_SQUARES,
    SCREEN_ROWS,
    SQUARE_PX,
    Square,
    Surface,
    TargetCode,
    board_raster,
    code_text,
    grey_code,
    qr_copies,
    square,
)
from contracts.models import CoverFit, FrameProfile
from player.calibration_target import target_modules
from player.geometry import BoardDraw, board_draw, board_point, board_signal
from scripts.calibration_target.boards import charuco_board, marker_squares

CODE = TargetCode("p-" + "0123456789abcdef" * 2, "HDMI-A-1", 3)
MODULES = target_modules(CODE)
WIDE = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=27)
FIVE_INCH = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=5)
LANDSCAPE = board_draw(CoverFit(), WIDE, MODULES)
PORTRAIT = board_draw(CoverFit(rotation=90), WIDE, MODULES)


def raster_at(column: float, row: float) -> float:
    raster = board_raster(Surface.SCREEN)
    return raster.pixels[math.floor(row * SQUARE_PX) * raster.width + math.floor(column * SQUARE_PX)] / 255


def drawn(draw: BoardDraw, px: int) -> np.ndarray:
    """The board as the Player draws it, sampled at `px` pixels a square, one square of the
    Player's black round it."""
    width, height = (draw.columns + 2) * px, (SCREEN_ROWS + 2) * px
    image = np.zeros((height, width), np.uint8)
    for y, x in itertools.product(range(height), range(width)):
        image[y, x] = round(255 * board_signal((x + .5) / px - 1, (y + .5) / px - 1, draw))
    return image


# -- 1. The opening's board ----------------------------------------------------------------------

def test_board_draw_takes_the_openings_aspect_and_turn():
    assert LANDSCAPE.aspect == pytest.approx(16 / 9) and not LANDSCAPE.portrait
    assert PORTRAIT.aspect == pytest.approx(16 / 9) and PORTRAIT.portrait
    assert LANDSCAPE.columns == PORTRAIT.columns == 17
    assert LANDSCAPE.modules == MODULES


@pytest.mark.parametrize("draw", [LANDSCAPE, PORTRAIT])
def test_the_openings_centre_is_the_boards_centre(draw):
    assert board_point(.5, .5, draw) == pytest.approx((draw.columns / 2, SCREEN_ROWS / 2))


def test_a_landscape_board_runs_along_u():
    along = LANDSCAPE.aspect * SCREEN_ROWS
    offset = (along - LANDSCAPE.columns) / 2
    assert board_point(0, 0, LANDSCAPE) == pytest.approx((-offset, 0))
    assert board_point(1, 1, LANDSCAPE) == pytest.approx((along - offset, SCREEN_ROWS))


def test_a_portrait_board_has_its_top_at_the_openings_right():
    for v in (0, .3, 1):
        assert board_point(1, v, PORTRAIT)[1] == pytest.approx(0)
        assert board_point(0, v, PORTRAIT)[1] == pytest.approx(SCREEN_ROWS)
    assert board_point(.5, 0, PORTRAIT)[0] < board_point(.5, 1, PORTRAIT)[0]


# -- 2. What each point draws ----------------------------------------------------------------------

@pytest.mark.parametrize("point", [(-.01, 5), (17, 5), (3, -.01), (3, SCREEN_ROWS), (30, 30)])
def test_outside_the_board_is_the_players_black(point):
    assert board_signal(*point, LANDSCAPE) == 0.0


def test_each_square_draws_its_kind():
    for row, column in itertools.product(range(SCREEN_ROWS), range(LANDSCAPE.columns)):
        kind = square(Surface.SCREEN, row, column)
        middle = (column + .5, row + .5)
        if kind is Square.WHITE:
            assert board_signal(*middle, LANDSCAPE) == 1.0
        elif kind is Square.GREY:
            assert board_signal(*middle, LANDSCAPE) == grey_code(row, column) / 255
        elif kind is Square.MARKER:
            for dx, dy in itertools.product(np.arange(0, 1, 1 / SQUARE_PX), repeat=2):
                point = (column + dx + .01, row + dy + .01)
                assert board_signal(*point, LANDSCAPE) == raster_at(*point)


def module_centres(copy, side: int):
    """(column, row) in squares of every module centre of a copy's QR, quiet zone included, with
    its index (r, c) from the modules' top-left (negative or beyond in the quiet zone)."""
    pitch = QR_SQUARES / side
    for r, c in itertools.product(range(side), repeat=2):
        yield (copy.column + (c + .5) * pitch, copy.row + (r + .5) * pitch), (r - QR_QUIET_MODULES,
                                                                               c - QR_QUIET_MODULES)


def test_every_copy_draws_the_codes_modules_in_a_light_quiet_zone():
    side = len(MODULES) + 2 * QR_QUIET_MODULES
    for copy in qr_copies(LANDSCAPE.columns):
        for point, (r, c) in module_centres(copy, side):
            dark = 0 <= r < len(MODULES) and 0 <= c < len(MODULES) and MODULES[r][c]
            assert board_signal(*point, LANDSCAPE) == (0.0 if dark else 1.0), (copy, r, c)


def test_without_a_code_every_qr_square_draws_light():
    blank = BoardDraw(LANDSCAPE.aspect, False, None)
    side = len(MODULES) + 2 * QR_QUIET_MODULES
    for copy in qr_copies(blank.columns):
        assert {board_signal(*point, blank) for point, _ in module_centres(copy, side)} == {1.0}


def test_a_copy_cut_by_the_openings_edge_draws_the_raster():
    narrow = BoardDraw(1.0, False, MODULES)
    assert narrow.columns == 10
    for row in range(1, 1 + QR_SQUARES):
        for dx in np.arange(0, 1, .1):
            assert board_signal(9 + dx, row + .5, narrow) == 1.0 == raster_at(9 + dx, row + .5)


# -- 3. The tracer's seam: OpenCV finds what the Player draws ----------------------------------------

def test_the_drawn_board_is_found_and_its_code_read():
    draw = board_draw(CoverFit(), FIVE_INCH, MODULES)
    assert draw.columns == 17
    parameters = cv2.aruco.CharucoParameters()
    parameters.minMarkers = 1
    detector = cv2.aruco.CharucoDetector(charuco_board(Surface.SCREEN), parameters)
    corners, corner_ids, _, marker_ids = detector.detectBoard(drawn(draw, 12))
    shown = {marker for marker, (_, column) in marker_squares(Surface.SCREEN).items() if column < draw.columns}
    assert marker_ids is not None and shown <= set(marker_ids.ravel())
    assert corner_ids is not None and len(corner_ids) >= 8

    # A QR copy's 41 modules need about 2 pixels each, so the code is read from the same board
    # sampled at 36 px a square (12 px gives under one pixel a module: .claude/errata.md).
    px, out = 36, 120
    image = drawn(draw, px)
    reader = cv2.QRCodeDetector()
    texts = set()
    for copy in qr_copies(draw.columns):
        left, top, size = (copy.column + .75) * px, (copy.row + .75) * px, (QR_SQUARES + .5) * px
        source = np.float32([[left, top], [left + size, top], [left + size, top + size], [left, top + size]])
        side = (QR_SQUARES + .5) * out
        target = np.float32([[0, 0], [side, 0], [side, side], [0, side]])
        warped = cv2.warpPerspective(image, cv2.getPerspectiveTransform(source, target), (int(side), int(side)))
        texts.add(reader.detectAndDecode(cv2.normalize(warped, None, 0, 255, cv2.NORM_MINMAX))[0])
    assert code_text(CODE) in texts


def test_a_small_piece_of_the_paper_is_found():
    raster = board_raster(Surface.PAPER)
    paper = np.frombuffer(raster.pixels, np.uint8).reshape(raster.height, raster.width)
    piece = paper[4 * SQUARE_PX:8 * SQUARE_PX, 6 * SQUARE_PX:10 * SQUARE_PX]
    piece = cv2.resize(piece, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
    image = cv2.copyMakeBorder(piece, 32, 32, 32, 32, cv2.BORDER_CONSTANT, value=255)
    parameters = cv2.aruco.CharucoParameters()
    parameters.minMarkers = 1
    _, _, _, marker_ids = cv2.aruco.CharucoDetector(charuco_board(Surface.PAPER), parameters).detectBoard(image)
    assert marker_ids is not None and set(marker_ids.ravel()) <= set(marker_squares(Surface.PAPER))
