"""Reading a located photo against its sent entry (decision 0027, Detection rows 5-9; owner,
2026-10-14: "Make sure that you are using standard libraries and not building the algorithm"). Pure.

Library steps: each surface rectified into its board's squares (cv2.warpPerspective), each square
the numpy median of its middle SAMPLE_SHARE (row 5), its noise scipy's median absolute deviation;
the lens falloff fitted by numpy.linalg.lstsq (row 8). The four bespoke rules, each because no
library models it:
- **Row 6, each white over its own blacks.** A white square less the median of its black
  neighbours; a spread across those blacks over EDGE_TOLERANCE of the surface's white means a
  reflection edge crosses it (REFLECTION_EDGE). Reflections only add light and are uneven (owner,
  2026-10-13: "what if the reflection is only in the middle?"); this is his reflection plan, which
  no library knows. A white too little over its blacks, or far under its surface's whites, is
  covered; one over CLIP_SHARE clipped is CLIPPED. Every drop is reported with its place.
- **Row 7, the ink's level.** (paper black - the screen blacks near it) / (paper white - the same),
  one value per photo: the ink need not be black (owner, 2026-10-14: "My black ink is running low
  and printing gray-ish"), and no library knows the print. The paper's white above its own blacks
  is divided by (1 - ink): the bare paper's white.
- **Row 8, lens falloff.** One radial falloff 1 - k r^2 (the DNG standard's FixVignetteRadial
  form) shared by both surfaces' whites: no library estimates a lens's falloff from one photo
  without a profile. Without it the whites paired by nearness carry the lens's falloff between them.
- **Row 9, pairing and vote.** Each paper white against the median of the NEAREST screen whites;
  ratios beyond WHITE_TOLERANCE of the median are dropped with their places. The tolerance is
  physical and the places are what the reading reports; a statistical clip (sigma, MAD) would widen
  with the very reflections it should drop.

The greys (ProRAW only) are each read over their own blacks against the screen whites nearest
them, their copies voted; the near-black rulers of the whole QR copies give the lowest code apart
from raw 0 (PLUGE). A uniform reflection over everything is unseen and harmless; a shadow over the
paper alone is unseen (decision 0027, Costs). One photo is read: the two targets are compared with
each other (owner, 2026-10-14: "The point of using two calibration targets is that we are comparing
them relatively.").
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from math import sqrt
from typing import Final

import cv2
import numpy as np
from scipy.stats import median_abs_deviation

from central.displays.views import PictureEntryView
from contracts.calibration_target import (
    NEAR_BLACK_STEPS,
    PAPER_COLUMNS,
    PAPER_ROWS,
    QR_SQUARES,
    SCREEN_COLUMNS_MAX,
    SCREEN_ROWS,
    QrCopy,
    Square,
    Surface,
    grey_code,
    qr_copies,
    ruler_steps,
    square,
)
from contracts.tuning import SIGNAL_LEVELS, TARGET_PATTERNS

from .locate import Board, Located, paper_squares, place, viewing_angle
from .model import (
    CLIP_SHARE,
    CONTRAST_FLOOR,
    COVERED_SHARE,
    DISTINCT_SIGMAS,
    EDGE_TOLERANCE,
    FALLOFF_KEEP,
    FALLOFF_MAX,
    GREY_TOLERANCE,
    NEAREST,
    RECTIFIED_PX,
    SAMPLE_SHARE,
    WHITE_TOLERANCE,
    Dropped,
    DropReason,
    PhotoKind,
    Place,
    Reading,
    Refusal,
    Refused,
    ToneStep,
    Value,
)

_MEDIAN_ERROR: Final = 1.2533       # a median's standard error over a mean's, normal noise (sqrt(pi / 2))
_GREY_WHITES: Final = 4             # the screen whites nearest a grey that it is read against
_MEASURED: Final = (Square.WHITE, Square.BLACK, Square.GREY)


@dataclass(frozen=True)
class _Read:
    """One square as read: its kind, the median of its middle per channel (linear), the share of
    its sampled pixels clipped, its middle in photo pixels and its place."""
    kind: Square
    rgb: np.ndarray
    clipped: float
    at: np.ndarray
    place: Place


@dataclass(frozen=True)
class _White:
    """A white kept by row 6: above its blacks, as read, and its blacks' median."""
    above: np.ndarray
    rgb: np.ndarray
    blacks: np.ndarray
    square: _Read


Key = tuple[int, int]


def read_photo(located: Located, entry: PictureEntryView) -> Reading | Refused:
    """The Reading of a located photo of the sent entry its code names (the caller fetched `entry`
    for located.code). Refused NOT_A_TARGET when entry.test_pattern is not in contracts/tuning.py
    TARGET_PATTERNS; WHITE_UNREAD when a surface keeps under 2 whites after row 6, or under 2 of the
    paired whites agree (`where`: the dropped places; the detail counts them by reason). Pure."""
    if entry.test_pattern not in TARGET_PATTERNS:
        shown = "no test pattern" if entry.test_pattern is None else f"the {entry.test_pattern.value} pattern"
        return Refused(Refusal.NOT_A_TARGET, (), f"the photo's picture change showed {shown}, not a target")
    photo = located.photo
    height, width = photo.pixels.shape[:2]
    screen = _screen_squares(located)
    paper = _paper_squares(located)
    screen_whites, screen_drops = _whites(screen)
    paper_whites, paper_drops = _whites(paper)
    dropped = screen_drops + paper_drops
    for surface, whites in ((Surface.SCREEN, screen_whites), (Surface.PAPER, paper_whites)):
        if len(whites) < 2:
            mine = [d for d in dropped if d.where.surface is surface]
            return Refused(Refusal.WHITE_UNREAD, tuple(d.where for d in mine),
                           f"{len(whites)} {surface.value} whites read; {_counts(mine)}: move what covers, "
                           "reflects or shades there, or take the photo a little darker if whites clipped")

    screen_at = np.array([w.square.at for w in screen_whites.values()])
    screen_above = np.array([w.above for w in screen_whites.values()])
    paper_at = np.array([w.square.at for w in paper_whites.values()])
    ink = _ink(paper_whites, screen, located)
    paper_above = np.array([w.above for w in paper_whites.values()]) / (1 - ink)
    falloff = _falloff(screen_at, screen_above, paper_at, paper_above, (width, height))
    screen_flat = screen_above / _lens(falloff, screen_at, (width, height))[:, None]
    paper_flat = paper_above / _lens(falloff, paper_at, (width, height))[:, None]

    ratios = []
    for at, value, white in zip(paper_at, paper_flat, paper_whites.values(), strict=True):
        near = np.argsort(np.linalg.norm(screen_at - at, axis=1))[:NEAREST]
        ratios.append((np.median(screen_flat[near], axis=0) / value, white.square.place))
    white, drops = _vote("white", ratios, _white_close)
    dropped += drops
    if white is None:
        mine = [d for d in dropped if d.value == "white"]
        return Refused(Refusal.WHITE_UNREAD, tuple(d.where for d in mine),
                       f"no 2 of {len(ratios)} paper whites agree with the screen near them; {_counts(mine)}: "
                       "move what reflects or shades there")
    tone: tuple[ToneStep, ...] = ()
    lowest = None
    if photo.kind is PhotoKind.RAW:
        tone, drops = _tone(screen, screen_whites, screen_flat, falloff, (width, height))
        dropped += drops
        lowest = _lowest_distinct(located, screen)
    angle = viewing_angle(located.screen, (width, height), photo.focal_px, screen_at)
    return Reading(entry=entry, stage=entry.test_pattern, kind=photo.kind, white=white, tone=tone,
                   lowest_distinct=lowest, ink=float(ink), falloff=falloff, angle=angle, dropped=tuple(dropped))


# -- Row 5: squares ----------------------------------------------------------------------------------


def _rectified(image: np.ndarray, to_photo: np.ndarray, top: float, left: float, size: tuple[int, int],
               scale: tuple[float, float], interpolation: int = cv2.INTER_LINEAR) -> np.ndarray:
    """The board region from (left, top), in squares, at `scale` pixels a square (x, y), `size`
    pixels (width, height): cv2.warpPerspective through the board's map."""
    to_board = np.array([[1 / scale[0], 0, left + 0.5 / scale[0]], [0, 1 / scale[1], top + 0.5 / scale[1]],
                         [0, 0, 1]])
    return cv2.warpPerspective(image, to_photo @ to_board, size, flags=interpolation | cv2.WARP_INVERSE_MAP,
                               borderMode=cv2.BORDER_CONSTANT, borderValue=0)


def _middle(rectified: np.ndarray, rows: int, columns: int) -> np.ndarray:
    """Each square's middle SAMPLE_SHARE each way: rows x columns x samples (x channels)."""
    inset = int(round(RECTIFIED_PX * (1 - SAMPLE_SHARE) / 2))
    blocks = rectified.reshape(rows, RECTIFIED_PX, columns, RECTIFIED_PX, *rectified.shape[2:])
    middle = blocks[:, inset:RECTIFIED_PX - inset, :, inset:RECTIFIED_PX - inset]
    middle = np.swapaxes(middle, 1, 2)
    return middle.reshape(rows, columns, -1, *rectified.shape[2:])


def _squares(located: Located, board: Board, rows: range, columns: range,
             skip: Callable[[int, int], bool]) -> dict[Key, _Read]:
    """The measuring squares (WHITE, BLACK, GREY) of `board` in rows x columns lying wholly in the
    photo, less those `skip` names."""
    photo = located.photo
    if not len(rows) or not len(columns):
        return {}
    size = (len(columns) * RECTIFIED_PX, len(rows) * RECTIFIED_PX)
    scale = (RECTIFIED_PX, RECTIFIED_PX)
    pixels = _middle(_rectified(photo.pixels, board.to_photo, rows.start, columns.start, size, scale),
                     len(rows), len(columns))
    inside = _middle(_rectified(np.ones(photo.pixels.shape[:2], np.uint8), board.to_photo, rows.start,
                                columns.start, size, scale, cv2.INTER_NEAREST), len(rows), len(columns)).all(axis=2)
    clipped = _middle(_rectified(photo.clipped.astype(np.uint8), board.to_photo, rows.start, columns.start,
                                 size, scale, cv2.INTER_NEAREST), len(rows), len(columns)).mean(axis=2)
    medians = np.median(pixels, axis=2)
    out = {}
    for i, row in enumerate(rows):
        for j, column in enumerate(columns):
            kind = square(board.surface, row, column)
            if kind not in _MEASURED or not inside[i, j] or skip(row, column):
                continue
            where = place(board.surface, row, column, board.to_photo, photo)
            at = np.array([where.x * photo.pixels.shape[1], where.y * photo.pixels.shape[0]])
            out[(row, column)] = _Read(kind, medians[i, j].astype(np.float64), float(clipped[i, j]), at, where)
    return out


def _screen_squares(located: Located) -> dict[Key, _Read]:
    """The screen's squares in the columns its found points span (one beyond each side), less those
    the paper outline (with its card) overlaps."""
    xs = located.screen.object_points[:, 0]
    columns = range(max(0, int(np.floor(xs.min())) - 1), min(SCREEN_COLUMNS_MAX, int(np.ceil(xs.max())) + 1))
    outline = located.paper_outline.astype(np.float32)

    def under_paper(row: int, column: int) -> bool:
        quad = np.array([[column, row], [column + 1, row], [column + 1, row + 1], [column, row + 1]], np.float32)
        return cv2.intersectConvexConvex(quad, outline)[0] > 0

    return _squares(located, located.screen, range(SCREEN_ROWS), columns, under_paper)


def _paper_squares(located: Located) -> dict[Key, _Read]:
    top, left, bottom, right = paper_squares(located.paper)
    return _squares(located, located.paper, range(max(0, top), min(PAPER_ROWS, bottom)),
                    range(max(0, left), min(PAPER_COLUMNS, right)), lambda row, column: False)


# -- Row 6: each white over its own blacks ---------------------------------------------------------


def _blacks(squares: dict[Key, _Read], row: int, column: int) -> list[np.ndarray]:
    """The read BLACK squares beside (row, column), up, down, left and right."""
    near = ((row - 1, column), (row + 1, column), (row, column - 1), (row, column + 1))
    return [squares[k].rgb for k in near if k in squares and squares[k].kind is Square.BLACK]


def _whites(squares: dict[Key, _Read]) -> tuple[dict[Key, _White], list[Dropped]]:
    """Row 6 on one surface: the whites kept, and the dropped with their reasons."""
    found, dropped = {}, []

    def drop(read: _Read, why: DropReason) -> None:
        dropped.append(Dropped("white", read.place, why))

    for key, read in squares.items():
        if read.kind is not Square.WHITE:
            continue
        blacks = _blacks(squares, *key)
        if len(blacks) < 2:
            drop(read, DropReason.TOO_FEW_BLACKS)
            continue
        black = np.median(blacks, axis=0)
        if read.rgb.mean() < CONTRAST_FLOOR * black.mean():
            drop(read, DropReason.COVERED)
            continue
        found[key] = (read, black, np.array(blacks).mean(axis=1))
    if not found:
        return {}, dropped
    surface_white = float(np.median([read.rgb.mean() for read, _, _ in found.values()]))
    kept = {}
    for key, (read, black, levels) in found.items():
        if float(np.ptp(levels)) > EDGE_TOLERANCE * surface_white:
            drop(read, DropReason.REFLECTION_EDGE)
        elif read.clipped > CLIP_SHARE:
            drop(read, DropReason.CLIPPED)
        elif read.rgb.mean() < COVERED_SHARE * surface_white:
            drop(read, DropReason.COVERED)
        else:
            kept[key] = _White(above=read.rgb - black, rgb=read.rgb, blacks=black, square=read)
    return kept, dropped


def _counts(dropped: Sequence[Dropped]) -> str:
    counts: dict[str, int] = {}
    for item in dropped:
        counts[item.why.value] = counts.get(item.why.value, 0) + 1
    return ", ".join(f"{n} {why}" for why, n in sorted(counts.items())) or "none dropped"


# -- Row 7: the ink's level ------------------------------------------------------------------------


def _ink(paper_whites: dict[Key, _White], screen: dict[Key, _Read], located: Located) -> float:
    """The print's ink as a share of bare paper white: per paper white, (its blacks - the screen's
    NEAREST blacks near it) / (it - the same), the median over those whose screen blacks are flat
    (within EDGE_TOLERANCE of the paper white) when at least 3, else over all."""
    blacks = [read for read in screen.values() if read.kind is Square.BLACK]
    if not blacks:
        return 0.0
    black_at = np.array([read.at for read in blacks])
    black_rgb = np.array([read.rgb for read in blacks])
    paper_white = float(np.median([w.rgb.mean() for w in paper_whites.values()]))
    shares, flat = [], []
    for white in paper_whites.values():
        near = black_rgb[np.argsort(np.linalg.norm(black_at - white.square.at, axis=1))[:NEAREST]]
        screen_black = np.median(near, axis=0)
        shares.append(float(((white.blacks - screen_black) / (white.rgb - screen_black)).mean()))
        flat.append(float(np.ptp(near.mean(axis=1))) <= EDGE_TOLERANCE * paper_white)
    shares_array, flat_array = np.array(shares), np.array(flat)
    return float(np.median(shares_array[flat_array] if flat_array.sum() >= 3 else shares_array))


# -- Row 8: lens falloff ---------------------------------------------------------------------------


def _radius2(at: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """r^2 at photo points, r over half the photo's width from its middle."""
    middle = np.array(size, np.float64) / 2
    return (((at - middle) / middle[0]) ** 2).sum(axis=1)


def _lens(falloff: float, at: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    return 1 - falloff * _radius2(at, size)


def _falloff(screen_at: np.ndarray, screen: np.ndarray, paper_at: np.ndarray, paper: np.ndarray,
             size: tuple[int, int]) -> float:
    """k of 1 - k r^2 shared by both surfaces' whites: least squares on their log means with one
    level per surface (log(1 - k r^2) ~ -k r^2), a second pass keeping those within FALLOFF_KEEP,
    k clipped to [0, FALLOFF_MAX]."""
    logs = np.log(np.maximum(np.concatenate([screen.mean(axis=1), paper.mean(axis=1)]), 1e-12))
    r2 = np.concatenate([_radius2(screen_at, size), _radius2(paper_at, size)])
    n, m = len(screen_at), len(paper_at)
    design = np.column_stack([np.r_[np.ones(n), np.zeros(m)], np.r_[np.zeros(n), np.ones(m)], -r2])
    keep = np.ones(len(logs), bool)
    coefficients = np.zeros(3)
    for _ in range(2):
        if keep.sum() < 4:
            break
        coefficients, *_ = np.linalg.lstsq(design[keep], logs[keep], rcond=None)
        keep = np.abs(design @ coefficients - logs) < FALLOFF_KEEP
    return float(np.clip(coefficients[2], 0.0, FALLOFF_MAX))


# -- Row 9: the vote ----------------------------------------------------------------------------------


def _white_close(copy: np.ndarray, middle: np.ndarray) -> bool:
    return bool(np.all(np.abs(copy / middle - 1) <= WHITE_TOLERANCE))


def _grey_close(copy: np.ndarray, middle: np.ndarray) -> bool:
    absolute, relative = GREY_TOLERANCE
    return bool(np.all(np.abs(copy - middle) <= absolute + relative * np.abs(middle)))


def _vote(name: str, copies: Sequence[tuple[np.ndarray, Place]],
          close: Callable[[np.ndarray, np.ndarray], bool]) -> tuple[Value | None, list[Dropped]]:
    """The copies within `close` of the median of all of them make the Value (their median, spread
    and count); the rest are dropped with their places. None when fewer than two agree."""
    if len(copies) < 2:
        return None, [Dropped(name, where, DropReason.TOO_FEW_CLEAN_COPIES) for _, where in copies]
    middle = np.median(np.array([c for c, _ in copies]), axis=0)
    agree = [(c, where) for c, where in copies if close(c, middle)]
    if len(agree) < 2:
        why = DropReason.COPIES_DISAGREE if len(copies) - len(agree) >= 2 else DropReason.TOO_FEW_CLEAN_COPIES
        return None, [Dropped(name, where, why) for _, where in copies]
    drops = [Dropped(name, where, DropReason.COPY_UNLIKE_THE_REST) for c, where in copies if not close(c, middle)]
    values = np.array([c for c, _ in agree])
    value = np.median(values, axis=0)
    spread = float(np.max(np.abs(values / np.where(value == 0, 1, value) - 1)))
    return Value(value=tuple(float(v) for v in value), spread=spread, copies=len(agree)), drops


# -- The greys and the rulers (ProRAW) ---------------------------------------------------------------


def _tone(screen: dict[Key, _Read], whites: dict[Key, _White], whites_flat: np.ndarray, falloff: float,
          size: tuple[int, int]) -> tuple[tuple[ToneStep, ...], list[Dropped]]:
    """Each grey over its blacks against the _GREY_WHITES screen whites nearest it (both with the
    lens falloff taken out), its copies voted per code; the steps by signal, brightest first."""
    white_at = np.array([w.square.at for w in whites.values()])
    surface_white = float(np.median([w.rgb.mean() for w in whites.values()]))
    copies: dict[int, list[tuple[np.ndarray, Place]]] = {}
    dropped = []
    for (row, column), read in screen.items():
        if read.kind is not Square.GREY:
            continue
        code = grey_code(row, column)
        blacks = _blacks(screen, row, column)
        if len(blacks) < 2:
            dropped.append(Dropped(_grey_name(code), read.place, DropReason.TOO_FEW_BLACKS))
            continue
        if float(np.ptp(np.array(blacks).mean(axis=1))) > EDGE_TOLERANCE * surface_white:
            dropped.append(Dropped(_grey_name(code), read.place, DropReason.REFLECTION_EDGE))
            continue
        near = np.argsort(np.linalg.norm(white_at - read.at, axis=1))[:_GREY_WHITES]
        above = (read.rgb - np.median(blacks, axis=0)) / _lens(falloff, read.at[None], size)[0]
        copies.setdefault(code, []).append((above / np.median(whites_flat[near], axis=0), read.place))
    steps = []
    for code in sorted(copies, reverse=True):
        value, drops = _vote(_grey_name(code), copies[code], _grey_close)
        dropped += drops
        if value is not None:
            steps.append(ToneStep(signal=code / SIGNAL_LEVELS, share=value))
    return tuple(steps), dropped


def _grey_name(code: int) -> str:
    return f"grey {code}"


def _lowest_distinct(located: Located, screen: dict[Key, _Read]) -> int | None:
    """The lowest near-black code that, with every code above it, stands apart from the raw-0 bars
    above and below its step by DISTINCT_SIGMAS of their noise (the median over the whole copies'
    rulers). None when no copy's ruler is whole in the photo, or code 16 is not apart."""
    steps: dict[int, list[tuple[float, float]]] = {}
    for copy in _whole_rulers(located, screen):
        for code, difference, noise in _ruler(located, copy):
            steps.setdefault(code, []).append((difference, noise))
    lowest = None
    for code in sorted(steps, reverse=True):
        found = steps[code]
        if not float(np.median([d for d, _ in found])) > DISTINCT_SIGMAS * max(n for _, n in found) / sqrt(len(found)):
            break
        lowest = code
    return lowest


def _whole_rulers(located: Located, screen: dict[Key, _Read]) -> list[QrCopy]:
    """The copies whose ruler row lies wholly in the photo, in columns where screen squares were read,
    and clear of the paper outline."""
    photo = located.photo
    height, width = photo.pixels.shape[:2]
    outline = located.paper_outline.astype(np.float32)
    read_columns = {column for _, column in screen}
    whole = []
    for copy in qr_copies(SCREEN_COLUMNS_MAX):
        row = copy.row + QR_SQUARES
        columns = range(copy.column, copy.column + QR_SQUARES)
        if not set(columns) <= read_columns:
            continue
        corners = np.array([[copy.column, row], [copy.column + QR_SQUARES, row],
                            [copy.column + QR_SQUARES, row + 1], [copy.column, row + 1]], np.float64)
        points = cv2.perspectiveTransform(corners[None], located.screen.to_photo)[0]
        if np.any(points < 0) or np.any(points[:, 0] >= width) or np.any(points[:, 1] >= height):
            continue
        if cv2.intersectConvexConvex(corners.astype(np.float32), outline)[0] > 0:
            continue
        whole.append(copy)
    return whole


def _ruler(located: Located, copy: QrCopy) -> list[tuple[int, float, float]]:
    """Each step of the copy's ruler: (code, its light less the bars' above and below, that
    difference's noise), the ruler row rectified at RECTIFIED_PX a step."""
    photo, board = located.photo, located.screen
    row = copy.row + QR_SQUARES
    per_square = RECTIFIED_PX * len(NEAR_BLACK_STEPS) / QR_SQUARES
    size = (len(NEAR_BLACK_STEPS) * RECTIFIED_PX, int(round(per_square)))
    grey = _rectified(photo.pixels, board.to_photo, row, copy.column, size, (per_square, per_square)).mean(axis=2)
    photo_per_pixel = (board.square_px / per_square) ** 2       # photo pixels behind one rectified pixel

    def region(left: float, right: float, top: float, bottom: float) -> tuple[float, float]:
        inset_x, inset_y = (1 - SAMPLE_SHARE) / 2 * (right - left), (1 - SAMPLE_SHARE) / 2 * (bottom - top)
        x0, x1 = (int(round((v - copy.column) * per_square)) for v in (left + inset_x, right - inset_x))
        y0, y1 = (int(round((v - row) * per_square)) for v in (top + inset_y, bottom - inset_y))
        values = grey[y0:max(y1, y0 + 1), x0:max(x1, x0 + 1)].ravel()
        independent = max(1.0, min(len(values), len(values) * photo_per_pixel))
        sigma = float(median_abs_deviation(values, scale="normal"))
        return float(np.median(values)), _MEDIAN_ERROR * sigma / sqrt(independent)

    out = []
    for step in ruler_steps(copy):
        light, noise = region(step.left, step.right, step.top, step.bottom)
        above, above_noise = region(step.left, step.right, row, step.top)
        below, below_noise = region(step.left, step.right, step.bottom, row + 1)
        out.append((step.code, light - (above + below) / 2, sqrt(noise ** 2 + (above_noise ** 2 + below_noise ** 2) / 4)))
    return out


__all__ = ["read_photo"]
