"""Both calibration boards as OpenCV objects (decision 0027): the one place a cv2.aruco.CharucoBoard
is built from contracts/calibration_target.py's constants, for the reader (locate) and the build
script that draws the rasters (scripts/calibration_boards.py). Pure: no I/O.

Prior art: OpenCV's ChArUco board (cv2.aruco.CharucoBoard), its predefined dictionary and its own
square order (getObjPoints), which this module reads rather than restating.
"""
from __future__ import annotations

from collections.abc import Mapping

import cv2

from contracts.calibration_target import Surface


def charuco_board(surface: Surface) -> cv2.aruco.CharucoBoard:
    """`surface`'s whole board, built once (cached): cv2.aruco.CharucoBoard((columns, rows), 1.0,
    BOARD_MARKER_SHARE, getPredefinedDictionary(BOARD_DICTIONARY), int32 board_ids(surface)), its
    columns SCREEN_COLUMNS_MAX or PAPER_COLUMNS and its rows SCREEN_ROWS or PAPER_ROWS; square
    length 1, so its object points are in board squares. Non-legacy pattern (OpenCV's default):
    the top-left square black."""
    ...


def marker_squares(surface: Surface) -> Mapping[int, tuple[int, int]]:
    """id -> (row, column) for each square of `surface`'s board drawn WITH its marker
    (contracts.calibration_target.square is MARKER), read from charuco_board(surface).
    getObjPoints (each marker's centre, floored); the board's other ids are left out of the
    drawing, so a detection of one is a misread. Built once (cached). Pure."""
    ...


__all__ = ["charuco_board", "marker_squares"]
