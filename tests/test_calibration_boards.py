"""Both calibration boards as OpenCV draws them (decision 0027; owner, 2026-10-14: "Make sure that
you are using standard libraries and not building the algorithm"): the generated rasters in
contracts/calibration_boards.py and the committed printable sheets (owner, 2026-10-14: "I don’t
want the assets replaced by every GHA run if they haven’t changed") match a fresh draw by
scripts/calibration_boards.py byte for byte, OpenCV's
board agrees with contracts/calibration_target.py's squares, and each raster paints every square
the layout leaves out as `square` says."""
from __future__ import annotations

import itertools
import math

import cv2
import numpy as np
import pytest

from central.displays.calibration_paper import PREVIEW_NAME, SHEETS_DIR, sheet_path
from contracts.calibration_target import (
    BOARD_MARKER_SHARE,
    PAPER_COLUMNS,
    PAPER_ROWS,
    QR_SQUARES,
    RULER_BAND,
    SCREEN_COLUMNS_MAX,
    SCREEN_ROWS,
    SQUARE_PX,
    PaperSize,
    Square,
    Surface,
    board_ids,
    board_raster,
    grey_code,
    qr_copies,
    ruler_steps,
    square,
)
from scripts.calibration_boards import (
    GENERATED,
    main,
    module_text,
    outputs,
    paper_raster,
    screen_raster,
)
from scripts.calibration_target.boards import charuco_board, marker_squares

SIZES = {Surface.SCREEN: (SCREEN_ROWS, SCREEN_COLUMNS_MAX), Surface.PAPER: (PAPER_ROWS, PAPER_COLUMNS)}
WHITE_VALUES = {Square.WHITE: 255, Square.BLACK: 0, Square.QR: 255}


def pixels(surface: Surface) -> np.ndarray:
    raster = board_raster(surface)
    return np.frombuffer(raster.pixels, np.uint8).reshape(raster.height, raster.width)


def centre(row: float, column: float) -> tuple[int, int]:
    """The raster's (y, x) pixel at a point in squares."""
    return math.floor(row * SQUARE_PX), math.floor(column * SQUARE_PX)


# -- 1. The generated file is a fresh draw ----------------------------------------------------------

def test_the_generated_rasters_are_a_fresh_draw():
    assert module_text(screen_raster(), paper_raster()) == GENERATED.read_text(encoding="utf-8")
    assert main(["--check"]) == 0
    assert board_raster(Surface.SCREEN) == screen_raster()
    assert board_raster(Surface.PAPER) == paper_raster()


def test_the_committed_sheets_are_a_fresh_draw():
    """Every PaperSize's PDF and the preview are committed and the same bytes as a fresh draw, so a
    CI run that draws them again changes nothing. Probe S1-date: a creationDate passed to the PDF
    writer -> the check fails (its bytes change with the time)."""
    files = outputs()
    assert {path for path in files if path.parent == SHEETS_DIR} == {
        *(sheet_path(paper) for paper in PaperSize), SHEETS_DIR / PREVIEW_NAME}
    for path, data in files.items():
        assert path.read_bytes() == data, path.name


@pytest.mark.parametrize("stale_name", ["calibration_boards.py", "photo-wall-calibration-target-4x6.pdf"])
def test_the_check_names_the_command_when_a_file_is_stale(stale_name, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("scripts.calibration_boards.GENERATED", tmp_path / "calibration_boards.py")
    monkeypatch.setattr("scripts.calibration_boards.SHEETS_DIR", tmp_path / "sheets")
    assert main([]) == 0 and main(["--check"]) == 0
    stale = next(path for path in outputs() if path.name == stale_name)
    stale.write_bytes(b"# stale\n")
    assert main(["--check"]) == 1
    err = capsys.readouterr().err
    assert stale_name in err and "python -m scripts.calibration_boards" in err
    assert main([]) == 0 and main(["--check"]) == 0


def test_the_generated_module_is_stdlib_data_under_a_hundred_columns():
    text = GENERATED.read_text(encoding="utf-8")
    imports = [line for line in text.splitlines() if line.startswith(("import ", "from "))]
    assert imports == ["import base64", "import zlib", "from typing import Final"]
    assert "never edit by hand" in text and "python -m scripts.calibration_boards" in text
    assert max(len(line) for line in text.splitlines()) < 100


# -- 2. OpenCV's board agrees with the layout ---------------------------------------------------------

@pytest.mark.parametrize("surface", list(Surface))
def test_opencvs_markers_sit_on_the_layouts_white_squares(surface):
    board = charuco_board(surface)
    rows, columns = SIZES[surface]
    assert board.getChessboardSize() == (columns, rows)
    held = set()
    for quad in board.getObjPoints():
        column, row = (math.floor(value) for value in np.asarray(quad)[:, :2].mean(axis=0))
        assert square(surface, row, column) is not Square.BLACK, (row, column)
        held.add((row, column))
    blacks = {(row, column) for row, column in itertools.product(range(rows), range(columns))
              if square(surface, row, column) is Square.BLACK}
    assert not held & blacks
    assert list(board.getIds().ravel()) == list(board_ids(surface))


def test_board_ids_are_the_two_ranges():
    assert board_ids(Surface.SCREEN) == range(0, 125)
    assert board_ids(Surface.PAPER) == range(125, 235)


@pytest.mark.parametrize("surface", list(Surface))
def test_marker_squares_are_the_layouts_marker_squares(surface):
    places = marker_squares(surface)
    assert set(places) <= set(board_ids(surface))
    rows, columns = SIZES[surface]
    markers = {(row, column) for row, column in itertools.product(range(rows), range(columns))
               if square(surface, row, column) is Square.MARKER}
    assert set(places.values()) == markers
    assert all(square(surface, *place) is Square.MARKER for place in places.values())


# -- 3. Each raster paints the layout ---------------------------------------------------------------

@pytest.mark.parametrize("surface", list(Surface))
def test_each_square_centre_is_its_kinds_value(surface):
    image = pixels(surface)
    rows, columns = SIZES[surface]
    assert image.shape == (rows * SQUARE_PX, columns * SQUARE_PX)
    for row, column in itertools.product(range(rows), range(columns)):
        kind = square(surface, row, column)
        value = image[centre(row + .5, column + .5)]
        if kind is Square.GREY:
            assert value == grey_code(row, column), (row, column)
        elif kind in WHITE_VALUES:
            assert value == WHITE_VALUES[kind], (row, column, kind)


@pytest.mark.parametrize("surface", list(Surface))
def test_each_marker_is_opencvs_in_whole_pixel_modules(surface):
    """A marker fills BOARD_MARKER_SHARE of its square in whole raster pixels a module, so the
    Player's nearest-pixel scaling never splits one: OpenCV's own marker image, white round it."""
    image = pixels(surface)
    dictionary = charuco_board(surface).getDictionary()
    side = round(BOARD_MARKER_SHARE * SQUARE_PX)
    margin = (SQUARE_PX - side) // 2
    assert side % (dictionary.markerSize + 2) == 0 and 2 * margin + side == SQUARE_PX
    for marker_id, (row, column) in marker_squares(surface).items():
        cell = image[row * SQUARE_PX:(row + 1) * SQUARE_PX, column * SQUARE_PX:(column + 1) * SQUARE_PX]
        marker = cv2.aruco.generateImageMarker(dictionary, marker_id, side, borderBits=1)
        assert np.array_equal(cell[margin:margin + side, margin:margin + side], marker), marker_id
        ring = cell.copy()
        ring[margin:margin + side, margin:margin + side] = 255
        assert (ring == 255).all(), marker_id


def test_each_ruler_step_is_its_code_between_raw_black_bars():
    image = pixels(Surface.SCREEN)
    for copy in qr_copies(SCREEN_COLUMNS_MAX):
        ruler_row = copy.row + QR_SQUARES
        for step in ruler_steps(copy):
            middle = (step.left + step.right) / 2
            assert image[centre((step.top + step.bottom) / 2, middle)] == step.code
            assert image[centre(ruler_row + RULER_BAND[0] / 2, middle)] == 0
            assert image[centre(ruler_row + (1 + RULER_BAND[1]) / 2, middle)] == 0


def test_the_paper_is_ink_or_bare_paper():
    assert set(np.unique(pixels(Surface.PAPER))) == {0, 255}
