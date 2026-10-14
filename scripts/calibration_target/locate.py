"""Finding both boards in a photo (decision 0027, Detection rows 2-4; owner, 2026-10-14: "Make sure that
you are using standard libraries and not building the algorithm"). Pure: OpenCV computes, nothing is
read or written.

Every step is a library call:
- each board's squares: OpenCV's ChArUco detector (cv2.aruco.CharucoDetector), which finds the
  markers and interpolates the chessboard's corners across left-out, covered and cut-off squares,
  then the board's own matchImagePoints (row 2);
- each surface's map: cv2.findHomography with USAC_MAGSAC, so misread corners drop out (row 3);
- the code: each QR copy rectified (cv2.warpPerspective), stretched (cv2.normalize) and decoded
  (cv2.QRCodeDetector) (row 4);
- the viewing angle: cv2.solvePnP (IPPE, the planar solver) with the EXIF focal length, or
  cv2.calibrateCamera estimating the focal length from the one view (design-v2, "Angle");
- the detectors' view: colour-science's sRGB encoding.

Any part of either board locates itself (each marker names its square), so the photo's framing,
the paper's piece, print scale, place and turn never matter; the refusals left are the real
failures (design-v2, "Remaining refusals").
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Final

import cv2
import numpy as np

from contracts.calibration_target import (
    BOARD_MARKER_SHARE,
    QR_SQUARES,
    SCREEN_COLUMNS_MAX,
    SCREEN_ROWS,
    Surface,
    TargetCode,
    parsed_code,
    qr_copies,
)

from .boards import charuco_board, marker_squares
from .model import (
    CHARUCO_CORNERS_MIN,
    HOMOGRAPHY_PX,
    MIN_PAPER_MARKERS,
    MIN_SCREEN_MARKERS,
    PAPER_REACH_SQUARES,
    QR_MARGIN_SQUARES,
    QR_PX_PER_SQUARE,
    SMALL_SQUARE_PX,
    VIEW_WHITE_PERCENTILE,
    Angle,
    FocalSource,
    Photo,
    PhotoKind,
    Place,
    Refusal,
    Refused,
)

with warnings.catch_warnings():
    # colour-science warns at import that matplotlib (its plotting, unused here) is missing.
    warnings.filterwarnings("ignore", message='"Matplotlib" related API')
    import colour

_FOCAL_RANGE: Final = (0.3, 3.0)    # an estimated focal length outside this many photo widths is not trusted
_FOCAL_FALLBACK: Final = 0.667      # and replaced by a phone's main camera's (about 26 mm equivalent), in widths
_MARKER_MIN_PERIMETER: Final = 0.008  # DetectorParameters.minMarkerPerimeterRate: a small piece's markers count


@dataclass(frozen=True, eq=False)
class Board:
    """One board found in a photo: `to_photo` maps its points (column, row), in squares, to photo
    pixels (3 x 3); `object_points` (N x 3, z 0) and `image_points` (N x 2) the homography's
    inliers; `markers` the drawn marker ids found; `square_px` the median found square's side in
    photo pixels."""
    surface: Surface
    to_photo: np.ndarray
    object_points: np.ndarray
    image_points: np.ndarray
    markers: tuple[int, ...]
    square_px: float


@dataclass(frozen=True, eq=False)
class Located:
    """A photo with both boards and the code found. `paper_outline`: the found paper squares'
    bounding box grown by one square, as 4 x 2 points (column, row) in screen board squares."""
    photo: Photo
    screen: Board
    paper: Board
    code: TargetCode
    paper_outline: np.ndarray


def detector_view(photo: Photo) -> np.ndarray:
    """The photo as 8-bit grey for the detectors: the mean channel, sRGB-encoded
    (colour.cctf_encoding); a RAW photo first scaled so its VIEW_WHITE_PERCENTILE is 1. Pure."""
    grey = photo.pixels.mean(axis=2)
    if photo.kind is PhotoKind.RAW:
        top = float(np.percentile(grey, VIEW_WHITE_PERCENTILE)) or 1.0
        grey = grey / top
    encoded = colour.cctf_encoding(np.clip(grey, 0.0, 1.0), function="sRGB")
    return np.clip(encoded * 255 + 0.5, 0, 255).astype(np.uint8)


def find_board(view: np.ndarray, surface: Surface) -> Board | None:
    """`surface`'s board in `view`: OpenCV's ChArUco detector, only the drawn marker ids kept
    (a left-out id is a misread); the ChArUco corners when at least CHARUCO_CORNERS_MIN, else the
    markers' corners, matched to the board's points; their homography by USAC_MAGSAC. None with no
    drawn marker or no homography. Pure."""
    board = charuco_board(surface)
    corners, corner_ids, marker_corners, marker_ids = _detector(surface).detectBoard(view)
    drawn = marker_squares(surface)
    keep = [] if marker_ids is None else [k for k, i in enumerate(marker_ids.ravel()) if int(i) in drawn]
    if not keep:
        return None
    if corner_ids is not None and len(corner_ids) >= CHARUCO_CORNERS_MIN:
        objects, images = board.matchImagePoints(corners, corner_ids)
    else:
        markers = cv2.aruco.Board(board.getObjPoints(), board.getDictionary(), board.getIds())
        objects, images = markers.matchImagePoints([marker_corners[k] for k in keep], marker_ids[keep])
    objects, images = objects.reshape(-1, 3).astype(np.float64), images.reshape(-1, 2).astype(np.float64)
    if len(objects) < 4:
        return None
    to_photo, inliers = cv2.findHomography(objects[:, :2], images, cv2.USAC_MAGSAC, HOMOGRAPHY_PX)
    if to_photo is None:
        return None
    inliers = inliers.ravel().astype(bool)
    sides = [np.linalg.norm(np.roll(quad.reshape(4, 2), 1, axis=0) - quad.reshape(4, 2), axis=1).mean()
             for quad in (marker_corners[k] for k in keep)]
    return Board(surface=surface, to_photo=to_photo, object_points=objects[inliers], image_points=images[inliers],
                 markers=tuple(sorted(int(marker_ids.ravel()[k]) for k in keep)),
                 square_px=float(np.median(sides)) / BOARD_MARKER_SHARE)


def locate(photo: Photo) -> Located | Refused:
    """Both boards and the code in `photo`, refused in this order: TARGET_NOT_FOUND when the screen
    board has under MIN_SCREEN_MARKERS markers found or the paper's under MIN_PAPER_MARKERS (the
    detail: come closer or print larger, with the square size when one was found under
    SMALL_SQUARE_PX); CODE_UNREADABLE when no QR copy within the found screen columns (-4/+5)
    decodes to a code (`where`: the copies tried); PAPER_OFF_SCREEN when the paper outline's centre
    lies outside the screen board's rows or more than PAPER_REACH_SQUARES columns beyond its found
    markers' (`where`: the paper's middle). Pure."""
    view = detector_view(photo)
    screen = find_board(view, Surface.SCREEN)
    paper = find_board(view, Surface.PAPER)
    small = _small(screen, paper)
    if screen is None or len(screen.markers) < MIN_SCREEN_MARKERS:
        return Refused(Refusal.TARGET_NOT_FOUND, (),
                       f"{photo.name}: the screen target was not found: hold the camera so that some of "
                       f"the screen target is in the photo, closer if it is small{small}")
    if paper is None or len(paper.markers) < MIN_PAPER_MARKERS:
        return Refused(Refusal.TARGET_NOT_FOUND, (),
                       f"{photo.name}: the paper target was not found: come closer or print it larger{small}")
    columns = [marker_squares(Surface.SCREEN)[i][1] for i in screen.markers]
    code, tried = _code(view, screen, min(columns) - 4, max(columns) + 5)
    if code is None:
        return Refused(Refusal.CODE_UNREADABLE, tried,
                       "no QR on the screen target was read: let one whole QR into the photo, in focus")
    outline = _paper_outline(screen, paper)
    column, row = outline.mean(axis=0)
    if not (0 <= row <= SCREEN_ROWS) or not (min(columns) - PAPER_REACH_SQUARES <= column
                                             <= max(columns) + PAPER_REACH_SQUARES):
        middle = np.linalg.inv(paper.to_photo) @ screen.to_photo @ np.array([column, row, 1.0])
        return Refused(Refusal.PAPER_OFF_SCREEN,
                       (place(Surface.PAPER, middle[1] / middle[2], middle[0] / middle[2], paper.to_photo, photo),),
                       "the paper lies off the screen target: lay it on the screen (under the glass if there is one)")
    return Located(photo=photo, screen=screen, paper=paper, code=code, paper_outline=outline)


def viewing_angle(board: Board, size: tuple[int, int], focal_px: float | None, at: np.ndarray) -> Angle:
    """The median angle, in degrees, between `board`'s normal and the camera's rays to `at` (N x 2
    photo pixels), its pose from cv2.solvePnP (IPPE) with K of `focal_px` (the EXIF focal length;
    principal point the photo's centre); without it, the focal length cv2.calibrateCamera
    estimates from the one view with everything else fixed, one outside _FOCAL_RANGE widths
    replaced by _FOCAL_FALLBACK widths (ESTIMATED). `size`: (width, height). Pure."""
    width, height = size
    focal, source = (focal_px, FocalSource.EXIF) if focal_px else (_estimated_focal(board, size), FocalSource.ESTIMATED)
    camera = np.array([[focal, 0, width / 2], [0, focal, height / 2], [0, 0, 1]])
    _, rotation, _ = cv2.solvePnP(board.object_points, board.image_points, camera, None, flags=cv2.SOLVEPNP_IPPE)
    normal = cv2.Rodrigues(rotation)[0][:, 2]
    rays = np.linalg.inv(camera) @ np.vstack([np.asarray(at, np.float64).T, np.ones(len(at))])
    rays /= np.linalg.norm(rays, axis=0)
    return Angle(degrees=float(np.median(np.degrees(np.arccos(np.clip(np.abs(normal @ rays), 0, 1))))),
                 focal=source)


def place(surface: Surface, row: float, column: float, to_photo: np.ndarray, photo: Photo) -> Place:
    """The Place of the point (column + 0.5, row + 0.5) of `surface`'s board: a square's middle
    when (row, column) is a square's. Pure."""
    point = to_photo @ np.array([column + 0.5, row + 0.5, 1.0])
    height, width = photo.pixels.shape[:2]
    return Place(surface=surface, row=float(row), column=float(column),
                 x=float(point[0] / point[2] / width), y=float(point[1] / point[2] / height))


def _estimated_focal(board: Board, size: tuple[int, int]) -> float:
    width, height = size
    guess = np.array([[_FOCAL_FALLBACK * width, 0, width / 2], [0, _FOCAL_FALLBACK * width, height / 2], [0, 0, 1]])
    flags = (cv2.CALIB_USE_INTRINSIC_GUESS | cv2.CALIB_FIX_PRINCIPAL_POINT | cv2.CALIB_FIX_ASPECT_RATIO
             | cv2.CALIB_ZERO_TANGENT_DIST | cv2.CALIB_FIX_K1 | cv2.CALIB_FIX_K2 | cv2.CALIB_FIX_K3)
    try:
        _, found, _, _, _ = cv2.calibrateCamera([board.object_points.astype(np.float32)],
                                                [board.image_points.astype(np.float32)], size, guess,
                                                np.zeros(5), flags=flags)
    except cv2.error:
        return _FOCAL_FALLBACK * width
    focal = float(found[0, 0])
    low, high = _FOCAL_RANGE
    return focal if low * width < focal < high * width else _FOCAL_FALLBACK * width


_DETECTORS: dict[Surface, cv2.aruco.CharucoDetector] = {}


def _detector(surface: Surface) -> cv2.aruco.CharucoDetector:
    """OpenCV's ChArUco detector for `surface`'s board: subpixel corners, small markers kept, and
    minMarkers 1 (a left-out square leaves one marker beside some corners)."""
    if surface not in _DETECTORS:
        markers = cv2.aruco.DetectorParameters()
        markers.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        markers.minMarkerPerimeterRate = _MARKER_MIN_PERIMETER
        charuco = cv2.aruco.CharucoParameters()
        charuco.minMarkers = 1
        _DETECTORS[surface] = cv2.aruco.CharucoDetector(charuco_board(surface), charuco, markers)
    return _DETECTORS[surface]


def _small(*boards: Board | None) -> str:
    """The detail's note on a found board whose squares are under SMALL_SQUARE_PX, else ''."""
    for board in boards:
        if board is not None and board.square_px < SMALL_SQUARE_PX:
            return (f" (the {board.surface.value} target's squares span {board.square_px:.0f} px in the photo; "
                    f"about {SMALL_SQUARE_PX:.0f} are needed)")
    return ""


def _code(view: np.ndarray, screen: Board, low: int, high: int) -> tuple[TargetCode | None, tuple[Place, ...]]:
    """The first QR copy of qr_copies(SCREEN_COLUMNS_MAX) in the columns [low, high) that decodes to a
    code, and the places of the copies tried."""
    margin, scale = QR_MARGIN_SQUARES, QR_PX_PER_SQUARE
    side = int(round((QR_SQUARES + 2 * margin) * scale))
    detector = cv2.QRCodeDetector()
    tried = []
    height, width = view.shape[:2]
    for copy in qr_copies(SCREEN_COLUMNS_MAX):
        if copy.column + QR_SQUARES <= low or copy.column >= high:
            continue
        to_plane = np.array([[1 / scale, 0, copy.column - margin], [0, 1 / scale, copy.row - margin], [0, 0, 1]])
        square = cv2.warpPerspective(view, screen.to_photo @ to_plane, (side, side),
                                     flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)
        square = cv2.normalize(square, None, 0, 255, cv2.NORM_MINMAX)
        try:
            text, _, _ = detector.detectAndDecode(square)
        except cv2.error:
            text = ""
        code = parsed_code(text) if text else None
        if code is not None:
            return code, ()
        middle = (QR_SQUARES - 1) / 2
        point = screen.to_photo @ np.array([copy.column + middle + 0.5, copy.row + middle + 0.5, 1.0])
        x, y = point[0] / point[2], point[1] / point[2]
        if 0 <= x < width and 0 <= y < height:
            tried.append(Place(Surface.SCREEN, copy.row + middle, copy.column + middle, x / width, y / height))
    return None, tuple(tried)


def paper_squares(paper: Board) -> tuple[int, int, int, int]:
    """The found paper markers' squares' bounding box grown by one square: (top, left, bottom,
    right), the bottom and right exclusive, in paper board squares (not clipped to the board). Pure."""
    squares = np.array([marker_squares(Surface.PAPER)[i] for i in paper.markers], np.int64)
    (top, left), (bottom, right) = squares.min(axis=0) - 1, squares.max(axis=0) + 2
    return int(top), int(left), int(bottom), int(right)


def _paper_outline(screen: Board, paper: Board) -> np.ndarray:
    """paper_squares' box as 4 x 2 points in screen board squares."""
    top, left, bottom, right = paper_squares(paper)
    corners = np.array([[[left, top], [right, top], [right, bottom], [left, bottom]]], np.float64)
    to_screen = np.linalg.inv(screen.to_photo) @ paper.to_photo
    return cv2.perspectiveTransform(corners, to_screen)[0]


__all__ = ["Board", "Located", "detector_view", "find_board", "locate", "paper_squares", "place", "viewing_angle"]
