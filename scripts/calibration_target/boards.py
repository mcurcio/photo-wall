"""Both calibration boards as OpenCV objects (decision 0027): the one place a cv2.aruco.CharucoBoard
is built from contracts/calibration_target.py's constants, for the reader (locate) and the build
script that draws the rasters (scripts/calibration_boards.py). Pure: no I/O.

Prior art: OpenCV's ChArUco board (cv2.aruco.CharucoBoard), its predefined dictionary and its own
square order (getObjPoints), which this module reads rather than restating.
"""
from __future__ import annotations

import functools
import math
from collections.abc import Mapping
from types import MappingProxyType

import cv2
import numpy as np

from contracts.calibration_target import (
    BOARD_DICTIONARY,
    BOARD_MARKER_SHARE,
    PAPER_COLUMNS,
    PAPER_ROWS,
    SCREEN_COLUMNS_MAX,
    SCREEN_ROWS,
    Square,
    Surface,
    board_ids,
    square,
)


@functools.cache
def charuco_board(surface: Surface) -> cv2.aruco.CharucoBoard:
    """`surface`'s whole board, built once (cached): cv2.aruco.CharucoBoard((columns, rows), 1.0,
    BOARD_MARKER_SHARE, getPredefinedDictionary(BOARD_DICTIONARY), int32 board_ids(surface)), its
    columns SCREEN_COLUMNS_MAX or PAPER_COLUMNS and its rows SCREEN_ROWS or PAPER_ROWS; square
    length 1, so its object points are in board squares. Non-legacy pattern (OpenCV's default):
    the top-left square black."""
    size = (SCREEN_COLUMNS_MAX, SCREEN_ROWS) if surface is Surface.SCREEN else (PAPER_COLUMNS, PAPER_ROWS)
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, BOARD_DICTIONARY))
    ids = np.array(board_ids(surface), dtype=np.int32)
    return cv2.aruco.CharucoBoard(size, 1.0, BOARD_MARKER_SHARE, dictionary, ids)


@functools.cache
def marker_squares(surface: Surface) -> Mapping[int, tuple[int, int]]:
    """id -> (row, column) for each square of `surface`'s board drawn WITH its marker
    (contracts.calibration_target.square is MARKER), read from charuco_board(surface).
    getObjPoints (each marker's centre, floored); the board's other ids are left out of the
    drawing, so a detection of one is a misread. Built once (cached). Pure."""
    board = charuco_board(surface)
    places = {}
    for marker_id, corners in zip(board.getIds().ravel(), board.getObjPoints(), strict=True):
        column, row = (math.floor(value) for value in corners[:, :2].mean(axis=0))
        if square(surface, row, column) is Square.MARKER:
            places[int(marker_id)] = (row, column)
    return MappingProxyType(places)


__all__ = ["charuco_board", "marker_squares"]
