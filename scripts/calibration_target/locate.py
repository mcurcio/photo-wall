"""Finding both targets in a photo (decision 0026). Pure: OpenCV computes, nothing is read or written.

Prior art: OpenCV's ArUco detector (cv2.aruco.ArucoDetector, contracts/calibration_target.py
MARKER_DICTIONARY) for each surface's plane, its QR detector (cv2.QRCodeDetector) for the code, a
homography per surface from every corner of its markers found (cv2.findHomography). The screen and
the paper lie on the panel under one glass, so both planes are near one; the scale check compares
the paper's printed size, measured in the screen target's millimetres, with the layout's.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from itertools import combinations
from typing import Final

import cv2
import numpy as np

from contracts.calibration_target import (
    MARKER_DICTIONARY,
    PAPER_MARKER_IDS,
    PAPER_TARGET_MM,
    SCREEN_MARKER_IDS,
    SHEETS_MM,
    Marker,
    Patch,
    Rect,
    Surface,
    TargetCode,
    calibration_target,
    parsed_code,
)

from .model import (
    MARKERS_NEEDED,
    PATCH_SHARE,
    SCALE_TOLERANCE,
    Photo,
    PhotoKind,
    Place,
    Refusal,
    Refused,
)

QR_PIXELS_PER_MM: Final = 12.0      # the QR's square is rectified at this scale before it is decoded
QR_BORDER_MM: Final = 4.0           # and with this much of the surround round it
VIEW_WHITE_PERCENTILE: Final = 99.5  # a RAW photo is shown to the detectors with this percentile at white


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
    surface's four markers are found (`where`: the missing markers' places); OUTSIDE_PHOTO when a
    patch's sampled middle (`patch_quad`) or the QR's square does not lie wholly inside the photo
    (`where`: their places; three markers can be found with the fourth corner cut off); CODE_UNREADABLE
    when the QR's square (CalibrationTarget.qr through `screen`) yields no text or text
    contracts.calibration_target.parsed_code refuses; SCALE_MISMATCH when the paper markers'
    spacing, mapped into the screen's mm, differs from the layout's by more than SCALE_TOLERANCE
    (the detail names the scale found, e.g. "printed at 97 %"); PAPER_MISPLACED when the larger of
    both sheets (SHEETS_MM, each way), centred on the paper content as printed, would overlap a
    screen patch (`where`: those patches' places; the detail says which way the sheet is off the
    dark box's centre). A NORMAL photo is located as a RAW one. Pure."""
    view = _view(photo)
    found = _markers_found(view)
    target = calibration_target()
    missing = tuple(marker for marker in target.markers if marker.marker_id not in found)
    by_surface = {surface: [m for m in target.markers if m.surface is surface and m.marker_id in found]
                  for surface in Surface}
    short = tuple(m for m in missing if len(by_surface[m.surface]) < MARKERS_NEEDED)
    if short:
        names = ", ".join(sorted({m.surface.value for m in short}))
        return Refused(Refusal.TARGET_NOT_FOUND, tuple(_place(m) for m in short),
                       f"too few markers found on the {names} target")
    screen = _homography(by_surface[Surface.SCREEN], found)
    paper = _homography(by_surface[Surface.PAPER], found)
    outside = _outside(photo, screen, paper)
    if outside:
        names = ", ".join(sorted({place.surface.value for place in outside}))
        return Refused(Refusal.OUTSIDE_PHOTO, outside,
                       f"part of the {names} target lies outside the photo: step back or centre "
                       "the Frame in the photo")
    qr_place = Place(Surface.SCREEN, *target.qr.centre())
    code = parsed_code(_qr_text(view, screen, target.qr) or "")
    if code is None:
        return Refused(Refusal.CODE_UNREADABLE, (qr_place,), "the QR on the screen target was not read")
    scale = _paper_scale(screen, paper, by_surface[Surface.PAPER])
    if abs(scale - 1) > SCALE_TOLERANCE:
        return Refused(Refusal.SCALE_MISMATCH, (),
                       f"printed at {100 * scale:.0f} %: print the paper target at 100 %")
    misplaced = _paper_misplaced(screen, paper)
    if misplaced is not None:
        return misplaced
    return Located(photo=photo, screen=screen, paper=paper, code=code)


def patch_quad(homography: np.ndarray, patch: Patch) -> np.ndarray:
    """The four corners (pixels, 4 x 2) of the middle PATCH_SHARE of `patch` each way, through
    `homography` (its surface's plane to the photo): what the reader samples. Pure."""
    rect = patch.rect
    inset_x, inset_y = (1 - PATCH_SHARE) / 2 * rect.width_mm, (1 - PATCH_SHARE) / 2 * rect.height_mm
    x0, y0 = rect.x_mm + inset_x, rect.y_mm + inset_y
    x1, y1 = rect.x_mm + rect.width_mm - inset_x, rect.y_mm + rect.height_mm - inset_y
    return cv2.perspectiveTransform(np.array([[[x0, y0], [x1, y0], [x1, y1], [x0, y1]]], np.float64),
                                    homography)[0]


def _outside(photo: Photo, screen: np.ndarray, paper: np.ndarray) -> tuple[Place, ...]:
    """The places of the patches whose sampled middle, and of the QR's square, not wholly inside
    the photo."""
    height, width = photo.pixels.shape[:2]

    def out(quad: np.ndarray) -> bool:
        return bool(np.any(quad < 0) or np.any(quad[:, 0] > width) or np.any(quad[:, 1] > height))

    target = calibration_target()
    places = [Place(patch.surface, *patch.rect.centre()) for group in target.groups for patch in group.patches
              if out(patch_quad(screen if patch.surface is Surface.SCREEN else paper, patch))]
    if out(cv2.perspectiveTransform(_corners_mm(target.qr)[None], screen)[0]):
        places.append(Place(Surface.SCREEN, *target.qr.centre()))
    return tuple(places)


def _paper_misplaced(screen: np.ndarray, paper: np.ndarray) -> Refused | None:
    """PAPER_MISPLACED when the sheet as laid would cover a screen patch (`locate`), else None."""
    target = calibration_target()
    to_screen = np.linalg.inv(screen) @ paper           # the paper content's plane -> the screen's
    sheet_w, sheet_h = (max(size[axis] for size in SHEETS_MM.values()) for axis in (0, 1))
    left, top = (PAPER_TARGET_MM[0] - sheet_w) / 2, (PAPER_TARGET_MM[1] - sheet_h) / 2
    corners = np.array([[[left, top], [left + sheet_w, top], [left + sheet_w, top + sheet_h],
                         [left, top + sheet_h]]], np.float64)
    sheet = cv2.perspectiveTransform(corners, to_screen)[0].astype(np.float32)
    covered = tuple(Place(Surface.SCREEN, *patch.rect.centre())
                    for group in target.groups for patch in group.patches
                    if patch.surface is Surface.SCREEN
                    and cv2.intersectConvexConvex(sheet, _corners_mm(patch.rect).astype(np.float32))[0] > 0)
    if not covered:
        return None
    centre = cv2.perspectiveTransform(np.array([[[PAPER_TARGET_MM[0] / 2, PAPER_TARGET_MM[1] / 2]]]),
                                      to_screen)[0][0]
    dx, dy = (float(d) for d in centre - np.array(target.dark_box.centre()))
    off = " and ".join(f"{abs(d):.0f} mm {word[d > 0]}" for d, word in
                       ((dx, ("left", "right")), (dy, ("high", "low"))) if abs(d) >= 0.5) or "turned"
    return Refused(Refusal.PAPER_MISPLACED, covered,
                   f"the sheet lies {off} of the dark box's centre, over the screen target's "
                   "patches: lay it centred in the dark box, square to it")


def _view(photo: Photo) -> np.ndarray:
    """The photo as 8-bit grey for the detectors: a RAW one encoded as sRGB with its brightest
    VIEW_WHITE_PERCENTILE at white; a NORMAL one as it is."""
    grey = photo.pixels.mean(axis=2)
    if photo.kind is PhotoKind.RAW:
        top = float(np.percentile(grey, VIEW_WHITE_PERCENTILE)) or 1.0
        light = np.clip(grey / top, 0.0, 1.0)
        grey = np.where(light <= 0.0031308, 12.92 * light, 1.055 * light ** (1 / 2.4) - 0.055)
    return np.clip(grey * 255 + 0.5, 0, 255).astype(np.uint8)


@cache
def _detector() -> cv2.aruco.ArucoDetector:
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, MARKER_DICTIONARY))
    return cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())


def _markers_found(view: np.ndarray) -> dict[int, np.ndarray]:
    """Each wanted marker id found exactly once -> its four corners in pixels (TL, TR, BR, BL)."""
    corners, ids, _ = _detector().detectMarkers(view)
    wanted = set(SCREEN_MARKER_IDS) | set(PAPER_MARKER_IDS)
    seen: dict[int, list[np.ndarray]] = {}
    for quad, marker_id in zip(corners, () if ids is None else ids.flatten(), strict=True):
        if int(marker_id) in wanted:
            seen.setdefault(int(marker_id), []).append(quad.reshape(4, 2))
    return {marker_id: quads[0] for marker_id, quads in seen.items() if len(quads) == 1}


def _corners_mm(rect: Rect) -> np.ndarray:
    x, y, w, h = rect.x_mm, rect.y_mm, rect.width_mm, rect.height_mm
    return np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], np.float64)


def _homography(markers: list[Marker], found: dict[int, np.ndarray]) -> np.ndarray:
    plane = np.concatenate([_corners_mm(m.rect) for m in markers])
    pixels = np.concatenate([found[m.marker_id] for m in markers]).astype(np.float64)
    homography, _ = cv2.findHomography(plane, pixels, 0)
    return homography


def _place(marker: Marker) -> Place:
    return Place(marker.surface, *marker.rect.centre())


def _qr_text(view: np.ndarray, screen: np.ndarray, qr: Rect) -> str | None:
    """The QR's square rectified through `screen` (with a border of the surround) and decoded."""
    border = QR_BORDER_MM
    size = int(round((qr.width_mm + 2 * border) * QR_PIXELS_PER_MM))
    to_plane = np.array([[1 / QR_PIXELS_PER_MM, 0, qr.x_mm - border],
                         [0, 1 / QR_PIXELS_PER_MM, qr.y_mm - border], [0, 0, 1]])
    square = cv2.warpPerspective(view, screen @ to_plane, (size, size),
                                 flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)
    # Stretch the square to its own black and white: the screen's dark modules glow a little.
    low, high = np.percentile(square, (2, 98))
    if high - low < 1:
        return None
    stretched = np.clip((square.astype(np.float32) - low) * (255 / (high - low)), 0, 255).astype(np.uint8)
    text, _, _ = cv2.QRCodeDetector().detectAndDecode(stretched)
    return text or None


def _paper_scale(screen: np.ndarray, paper: np.ndarray, markers: list[Marker]) -> float:
    """The paper markers' spacing in the screen's mm over the layout's (the median over pairs)."""
    centres = np.array([m.rect.centre() for m in markers], np.float64)
    pixels = cv2.perspectiveTransform(centres.reshape(-1, 1, 2), paper)
    in_screen = cv2.perspectiveTransform(pixels, np.linalg.inv(screen)).reshape(-1, 2)
    ratios = [np.linalg.norm(in_screen[i] - in_screen[j]) / np.linalg.norm(centres[i] - centres[j])
              for i, j in combinations(range(len(markers)), 2)]
    return float(np.median(ratios))


__all__ = ["Located", "locate"]
