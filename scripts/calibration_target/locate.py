"""Finding both targets in a photo (decision 0026). Pure: OpenCV computes, nothing is read or written.

Prior art: OpenCV's ArUco detector (cv2.aruco.ArucoDetector, contracts/calibration_target.py
MARKER_DICTIONARY) for each surface's plane, its QR detector (cv2.QRCodeDetector) for the code, a
homography per surface from every corner of its markers found (cv2.findHomography). The screen and
the paper lie on the panel under one glass, so both planes are near one; the scale check compares
the paper's printed size, measured in the screen target's millimetres, with the layout's.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from contracts.calibration_target import TargetCode

from .model import Photo, Refused


@dataclass(frozen=True, eq=False)
class Located:
    """A photo with both targets found: `screen` maps the screen target's plane (mm) to the
    photo's pixels, `paper` the paper content's plane (mm) to them (3 x 3 homographies); `code` the
    QR's code."""
    photo: Photo
    screen: np.ndarray
    paper: np.ndarray
    code: TargetCode


def locate(photo: Photo) -> Located | Refused:
    """Both targets in `photo`. Refused TARGET_NOT_FOUND when fewer than MARKERS_NEEDED of a
    surface's four markers are found (`where`: the missing markers' places); CODE_UNREADABLE when
    the QR's square (CalibrationTarget.qr through `screen`) yields no text or text
    contracts.calibration_target.parsed_code refuses; SCALE_MISMATCH when the paper markers'
    spacing, mapped into the screen's mm, differs from the layout's by more than SCALE_TOLERANCE
    (the detail names the scale found, e.g. "printed at 97 %"). A NORMAL photo is located as a
    RAW one. Pure."""
    ...


__all__ = ["Located", "locate"]
