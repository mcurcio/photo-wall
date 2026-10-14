"""The calibration boards' layout and code (contracts/calibration_target.py "v2", decision 0027):
which square is what on each board, where the QR copies and their rulers sit, how many columns an
opening shows, and the QR's text. Pure: OpenCV's own drawing is tests/test_calibration_boards.py."""
from __future__ import annotations

import itertools
import math

import pytest

from contracts.calibration_target import (
    GREY_CODES,
    NEAR_BLACK_STEPS,
    PAPER_COLUMNS,
    PAPER_ROWS,
    QR_PERIOD,
    QR_SQUARES,
    RULER_BAND,
    SCREEN_COLUMNS_MAX,
    SCREEN_ROWS,
    SQUARE_PX,
    QrCopy,
    Square,
    Surface,
    TargetCode,
    code_text,
    grey_code,
    parsed_code,
    qr_copies,
    ruler_steps,
    screen_columns,
    square,
)
from contracts.node_output import OUTPUT_IDS

PLAYER_ID = "p-" + "0123456789abcdef" * 2
SIZES = {Surface.SCREEN: (SCREEN_ROWS, SCREEN_COLUMNS_MAX), Surface.PAPER: (PAPER_ROWS, PAPER_COLUMNS)}


def squares(surface: Surface, rows: range, columns: range) -> list[Square]:
    return [square(surface, row, column) for row, column in itertools.product(rows, columns)]


def greys_in(columns: int) -> list[int]:
    return [grey_code(row, column) for column in range(columns) for row in range(SCREEN_ROWS)
            if square(Surface.SCREEN, row, column) is Square.GREY]


# -- 1. Squares ------------------------------------------------------------------------------------

@pytest.mark.parametrize("surface", list(Surface))
def test_black_squares_follow_opencvs_parity(surface):
    rows, columns = SIZES[surface]
    for row, column in itertools.product(range(rows), range(columns)):
        kind = square(surface, row, column)
        if (row + column) % 2 == 0:
            assert kind in (Square.BLACK, Square.QR, Square.RULER), (row, column)
        else:
            assert kind is not Square.BLACK, (row, column)


def test_the_paper_board_is_markers_whites_and_blacks_only():
    kinds = set(squares(Surface.PAPER, range(PAPER_ROWS), range(PAPER_COLUMNS)))
    assert kinds == {Square.MARKER, Square.WHITE, Square.BLACK}


@pytest.mark.parametrize("surface,row,column", [
    (Surface.SCREEN, -1, 0), (Surface.SCREEN, SCREEN_ROWS, 0), (Surface.SCREEN, 0, SCREEN_COLUMNS_MAX),
    (Surface.PAPER, 0, -1), (Surface.PAPER, PAPER_ROWS, 0), (Surface.PAPER, 0, PAPER_COLUMNS),
])
def test_square_refuses_a_place_off_the_board(surface, row, column):
    with pytest.raises(ValueError, match="square"):
        square(surface, row, column)


@pytest.mark.parametrize("place", [(0, 0), (2, 2), (1, 1), (4, 1), (-1, 0), (0, SCREEN_COLUMNS_MAX)])
def test_grey_code_refuses_a_square_that_is_not_grey(place):
    with pytest.raises(ValueError, match="grey_code"):
        grey_code(*place)


# -- 2. Coverage: any part of either board holds what the reader needs -----------------------------

def test_qr_copies_repeat_every_period():
    assert qr_copies(25) == tuple(QrCopy(*place) for place in ((1, 1), (5, 5), (1, 9), (5, 13), (1, 17), (5, 21)))
    assert len(qr_copies(10)) == 2
    assert qr_copies(4) == (QrCopy(1, 1),) and qr_copies(3) == ()


@pytest.mark.parametrize("columns", [0, SCREEN_COLUMNS_MAX + 1, -3])
def test_qr_copies_refuses_a_column_count_off_the_board(columns):
    with pytest.raises(ValueError, match="columns"):
        qr_copies(columns)


def test_qr_squares_and_rulers_are_where_the_copies_say():
    for copy in qr_copies(SCREEN_COLUMNS_MAX):
        columns = range(copy.column, copy.column + QR_SQUARES)
        assert set(squares(Surface.SCREEN, range(copy.row, copy.row + QR_SQUARES), columns)) == {Square.QR}
        assert set(squares(Surface.SCREEN, range(copy.row + QR_SQUARES, copy.row + QR_SQUARES + 1),
                           columns)) == {Square.RULER}
    every = squares(Surface.SCREEN, range(SCREEN_ROWS), range(SCREEN_COLUMNS_MAX))
    assert every.count(Square.QR) == len(qr_copies(SCREEN_COLUMNS_MAX)) * QR_SQUARES ** 2


@pytest.mark.parametrize("start", range(SCREEN_COLUMNS_MAX - QR_PERIOD + 1))
def test_every_period_of_columns_holds_a_whole_copy_and_seven_markers(start):
    window = range(start, start + QR_PERIOD)
    assert any(copy.column >= start and copy.column + QR_SQUARES <= start + QR_PERIOD
               for copy in qr_copies(SCREEN_COLUMNS_MAX))
    assert squares(Surface.SCREEN, range(SCREEN_ROWS), window).count(Square.MARKER) >= 7


def test_every_grey_is_on_a_narrow_board():
    assert all(greys_in(13).count(code) >= 2 for code in GREY_CODES)
    assert all(greys_in(10).count(code) >= 1 for code in GREY_CODES)


@pytest.mark.parametrize("row,column", itertools.product(range(PAPER_ROWS - 3), range(PAPER_COLUMNS - 3)))
def test_every_four_by_four_piece_of_paper_holds_markers_and_whites(row, column):
    piece = squares(Surface.PAPER, range(row, row + 4), range(column, column + 4))
    assert piece.count(Square.MARKER) >= 4 and piece.count(Square.WHITE) >= 4


def test_a_ruler_is_every_near_black_step_in_whole_pixels():
    copy = QrCopy(5, 13)
    steps = ruler_steps(copy)
    assert tuple(step.code for step in steps) == NEAR_BLACK_STEPS
    assert steps[0].left == copy.column and steps[-1].right == copy.column + QR_SQUARES
    for step, after in itertools.pairwise(steps):
        assert step.right == after.left
    for step in steps:
        assert (step.top, step.bottom) == (copy.row + QR_SQUARES + RULER_BAND[0],
                                           copy.row + QR_SQUARES + RULER_BAND[1])
        for edge in (step.left, step.right, step.top, step.bottom):
            assert (edge * SQUARE_PX).is_integer()


# -- 3. Columns an opening shows --------------------------------------------------------------------

@pytest.mark.parametrize("aspect,columns", [(16 / 9, 17), (4 / 3, 13), (1, 10), (3, 25), (1.3, 13)])
def test_screen_columns_fill_the_long_side(aspect, columns):
    assert screen_columns(aspect) == columns


@pytest.mark.parametrize("aspect", [0.9, math.nan, math.inf, 0])
def test_screen_columns_refuses_an_aspect_under_one(aspect):
    with pytest.raises(ValueError, match="aspect"):
        screen_columns(aspect)


# -- 4. The code -------------------------------------------------------------------------------------

@pytest.mark.parametrize("output_id", OUTPUT_IDS)
@pytest.mark.parametrize("change", [1, 2**63 - 1])
def test_code_text_round_trips(output_id, change):
    code = TargetCode(PLAYER_ID, output_id, change)
    text = code_text(code)
    assert text == f"PWCT2 {PLAYER_ID} {output_id} {change}"
    assert parsed_code(text) == code


@pytest.mark.parametrize("text", [
    f"PWCT1 {PLAYER_ID} HDMI-A-1 3", f"PWCT2 {PLAYER_ID} HDMI-A-1", f"PWCT2 {PLAYER_ID} HDMI-A-1 3 4",
    f"PWCT2 {PLAYER_ID} HDMI-A-1 0", f"PWCT2 {PLAYER_ID} HDMI-A-1 01", f"PWCT2 {PLAYER_ID} HDMI-A-1 -1",
    "PWCT2 -bad HDMI-A-1 3", f"PWCT2 {PLAYER_ID} HDMI A-1 3", f"PWCT2  {PLAYER_ID} HDMI-A-1 3",
    f"PWCT2 {PLAYER_ID} HDMI-A-1 {2**63}", f"PWCT2 {PLAYER_ID} HDMI-A-1 3 ", "", "https://example.com",
])
def test_parsed_code_refuses_anything_else(text):
    assert parsed_code(text) is None


@pytest.mark.parametrize("fields", [
    (PLAYER_ID, "HDMI-A-1", 0), (PLAYER_ID, "HDMI-A-1", True), (PLAYER_ID, "HDMI-A-1", -1),
    (PLAYER_ID, "HDMI-A-1", 2**63), ("bad id", "HDMI-A-1", 1), (PLAYER_ID, "", 1), (PLAYER_ID, "HDMI-A-1", 1.0),
])
def test_target_code_refuses_a_bad_field(fields):
    with pytest.raises(ValueError, match="target_code"):
        TargetCode(*fields)
