"""The photo fit rule (roadmap 5a design rule 2; run ledger .claude/runs/photo-5a.md, slice R1):
whether a photo fills a Frame with its key content kept, and the crop that does it. One home: the
Planner, the authoring check, the candidates route and the fit analysis all ask here; the worker
only measures and the Player only draws.

Owner, 2026-10-11: "a photo either fills the frame or it doesn't show (it's incompatible with that
frame)"; "It's about the ratio, not strict labels"; "Smart crop sounds reasonable". So there are
no landscape or portrait labels, no mats and no pairs: a photo fits a Frame (a `FitTarget`) when
some window with the Frame's aspect (its Frame output's shape, the Position's box), slid along the photo's free axis
(contracts/photo_crop.py):
1. removes no more than the crop limit of the photo's area (`crop_share`; the backstop),
2. holds at least the Frame output in the pixels of the largest copy Photo Wall can read
   (`contracts.frame_fit.window_holds` against `FitTarget.output_px`; owner, 2026-10-11:
   compatibility "measuring against the Frame output and not the Display"; follow-up S1: an
   unedited RAW photo with full-size copies off is judged at its preview's size, the readable
   size of central/photo_library/rendition.py `readable_size`, never its original's), and the
   copy that Frame needs is no longer than the Player shows (`FitSettings.max_still_edge`,
   "above_player_limit"); both skipped for an image whose readable size is not measured yet
   (`size_known` False: nothing is refused on a size no one measured),
3. keeps every face whole (`faces_judged`: each face of at least `min_face_share` of the photo's
   area, padded by `face_padding` of its own width and height, inside the window), and
4. cuts no key content: the share of the interest grid it removes (1 - `interest_kept`) is at
   most `max_lost_interest_density` x the share of the area it removes (`crop_share`); a grid
   whose saliency `spread` is under `min_salience_spread` is judged as even
   (`interest_judged`). A side that
   holds no more than its share of the photo's interest may go (owner, 2026-10-11: "if the key
   content is on the left and the right is benign, then it would be reasonable to crop the
   picture so that the left edge is preserved"); one holding key content may not ("if it has key
   content on the sides then it can't be cropped to fit"). An even spread of interest passes.
Among the windows passing 3 and 4, `place` picks one: on a photo with faces, the window nearest the
faces' window (the faces in it, its spare room shared before and after them by their size, so a
lone face is centred and a bigger face gets more room on its side); on a photo without faces, the
one keeping the most interest. Windows are tried at offsets k / `window_steps` for
k = 0..`window_steps`, plus 0.5.

Every tuning value is a `FitSettings` field (central/wall_plan/fit_settings.py; owner,
2026-10-11: "Agreed that these should be configurable, and not hardcoded."), passed in by the
caller; faces and spread are applied here, at judge time, so a change needs no new measurement.

Videos (design "Hand-placed, stacks, video"): a centred window, rule 1, and requirements :186's
floor instead of rule 2 (a large Frame, `large_frame`, refuses a video whose shorter side is
under `large_frame_video_short_side`); a Frame with "Shows videos" off shows none. Key content is
ignored.

A Frame whose screen size is not known has no shape (frame box run, fb-8): `fit_target` answers
None and nothing is judged for it, so it gets no photo.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from functools import lru_cache
from typing import Final, Literal

from central.installation_models import FrameOutput, Pixels, PositiveMm
from central.photo_library.rendition import FrameNeed, copy_edge
from central.wall_plan.fit_settings import FIT_SETTING_RANGES, FitSettings
from central.wall_plan.key_content import InterestGrid, KeyContent, UnitBox
from contracts.frame_fit import FreeAxis, free_axis, window_holds
from contracts.models import Fraction, Model, PhotoCrop
from contracts.photo_crop import CENTRED, Window, crop_window

# Float slack for the rule's comparisons: a boundary case (4:3 on 16:9 at a 25 % limit, a photo of
# exactly the Frame's pixels) is judged as its exact arithmetic says.
_SLACK: Final = 1e-9
MM_PER_INCH: Final = 25.4

FitRefusal = Literal[
    "beyond_crop_limit",     # "doesn't fit this Frame's shape"
    "below_frame_pixels",    # "too small for this Frame"
    "above_player_limit",    # "needs more detail than this Pi can draw"
    "face_cut",              # "a face would be cut"
    "interest_cut",          # "the interesting part would be cut"
    "no_video_here",         # "this Frame shows no videos"
]
MediaKind = Literal["image", "video"]


class FitTarget(Model):
    """A Frame as the photo fit rule judges it (follow-ups 2026-10-11, slice B1; frame box run
    fb-8; owner, 2026-10-11: compatibility "measuring against the Frame output and not the
    Display"): its Frame output (`output_px`, whose ratio is its shape), whether it shows videos
    (`video`, frames.video) and its physical size as it hangs (`size_mm`; None when its Display
    states none). Built only by `fit_target`. Hashable: the rule's memo is keyed by it."""

    output_px: tuple[Pixels, Pixels]
    video: bool
    size_mm: tuple[PositiveMm, PositiveMm] | None

    @property
    def aspect(self) -> float:
        """output_px[0] / output_px[1]: the Frame's shape (contracts/frame_fit.py
        `Placement.aspect` of the same sides)."""
        return self.output_px[0] / self.output_px[1]


def fit_target(frame: FrameOutput) -> FitTarget | None:
    """The FitTarget of `frame`; None when its `output_px` is None (no known screen: no shape).
    The one place a Frame becomes what the fit rule judges: the Planner, authoring, the source
    preview and the fit analysis ask here. Pure."""
    if frame.output_px is None:
        return None
    return FitTarget(output_px=frame.output_px, video=frame.video, size_mm=frame.size_mm)


def large_frame(target: FitTarget, settings: FitSettings) -> bool:
    """Whether `target` is a large Frame for videos (requirements :186): True when its size is
    not known (so "A 720p video must never be assigned to a 40-inch Frame" holds on a Display
    that states no size), else when its diagonal, `hypot(*size_mm) / 25.4` inches, is at least
    `settings.large_frame_inches`. The one home of "a large Frame". Pure."""
    if target.size_mm is None:
        return True
    return math.hypot(*target.size_mm) / MM_PER_INCH >= settings.large_frame_inches


def band(aspect: float, crop_limit: float) -> tuple[float, float]:
    """The photo ratios (width / height) rule 1 accepts on a Frame of `aspect` at `crop_limit`:
    (aspect x (1 - crop_limit), aspect / (1 - crop_limit)), from `crop_share`'s formula. The one
    home of a Frame's ratio band: the analysis's Frame bands and its ratio histogram ask here
    (slice B2). Pure; ValueError unless `aspect` > 0 and 0 < `crop_limit` < 1."""
    if not aspect > 0 or not 0 < crop_limit < 1:
        raise ValueError("a positive aspect and a crop limit inside (0, 1) are required")
    keep = 1 - crop_limit
    return aspect * keep, aspect / keep


class Fits(Model):
    fits: Literal[True] = True
    crop: PhotoCrop
    crop_share: Fraction
    interest_kept: Fraction


class DoesNotFit(Model):
    fits: Literal[False] = False
    reason: FitRefusal


FitVerdict = Fits | DoesNotFit


def crop_share(width: int, height: int, aspect: float) -> float:
    """The share of the photo's area the largest window of the Frame's `aspect` (width / height)
    removes: 1 - the window's length on its free axis (contracts/frame_fit.py `free_axis`), which
    is the smaller of the two aspects / the larger. 0 for equal aspects. Pure; ValueError for a
    non-positive size or aspect."""
    return 1 - free_axis(width, height, aspect).length


def shape_fit(kind: MediaKind, width: int, height: int, target: FitTarget, crop_limit: float,
              settings: FitSettings, *, size_known: bool) -> DoesNotFit | None:
    """The size-only part of the rule, usable before key content is measured: `no_video_here`
    (a video on a Frame with `video` off), then `beyond_crop_limit` (rule 1), then
    `below_frame_pixels` (rule 2 for an image, against `target.output_px`; the :186 floor for a
    video on a `large_frame`), then `above_player_limit` (an image whose copy for this Frame,
    `copy_edge`, is longer than `settings.max_still_edge`); an image's two size tests are skipped
    when `size_known` is False (its readable size is not measured yet). None: the photo passes on
    its size alone. `width` and `height` are the size it is judged at, upright: an image's
    readable size (`central.catalog.judged_size`), a video's original. Pure."""
    if kind == "video" and not target.video:
        return DoesNotFit(reason="no_video_here")
    if crop_share(width, height, target.aspect) > crop_limit + _SLACK:
        return DoesNotFit(reason="beyond_crop_limit")
    if kind == "video":
        if large_frame(target, settings) and min(width, height) < settings.large_frame_video_short_side:
            return DoesNotFit(reason="below_frame_pixels")
        return None
    if not size_known:
        return None
    if not window_holds(width, height, target.aspect, target.output_px):
        return DoesNotFit(reason="below_frame_pixels")
    need = FrameNeed(aspect=target.aspect, width=target.output_px[0], height=target.output_px[1])
    if copy_edge((width, height), need) > settings.max_still_edge:
        return DoesNotFit(reason="above_player_limit")
    return None


def fitting_outputs(kind: MediaKind, width: int, height: int, frames: Sequence[FrameOutput],
                    settings: FitSettings) -> tuple[FrameOutput, ...]:
    """The Frames a photo judged at `width` x `height` could fill under any Scene: those whose
    `shape_fit` passes at the widest crop limit `FIT_SETTING_RANGES` allows. A superset of the
    Frames any Scene places it on (a Scene's own crop limit, faces and interest only narrow it;
    a Frame with no `fit_target` is never one),
    so the one copy sized for them (central/photo_library/rendition.py `wall_need`) holds every
    Frame it is shown on; it may be a step larger than the Scenes playing now need. A video
    fills none here: its copy's size is the recipe's, never the wall's. Pure."""
    if kind == "video":
        return ()
    widest = FIT_SETTING_RANGES["crop_limit"][1]
    return tuple(frame for frame in frames
                 if (target := fit_target(frame)) is not None
                 and shape_fit(kind, width, height, target, widest, settings, size_known=True) is None)


def interest_kept(grid: InterestGrid, window: Window) -> float:
    """The share of the grid's weight inside `window` (UV fractions), each cell counted by the
    fraction of its area the window covers. Pure. A crop window spans the photo's whole width or
    whole height (contracts/photo_crop.py), so the common case sums the grid's row or column
    totals along the cut axis only (the same arithmetic, one axis's coverage being 1)."""
    left, top, right, bottom = window
    whole_width, whole_height = left <= 0 and right >= 1, top <= 0 and bottom >= 1
    if whole_width or whole_height:
        rows, columns = _marginals(grid)
        if whole_width:
            kept = sum(total * _covered(top, bottom, row / grid.rows, (row + 1) / grid.rows)
                       for row, total in enumerate(rows))
        else:
            kept = sum(total * _covered(left, right, column / grid.columns,
                                        (column + 1) / grid.columns)
                       for column, total in enumerate(columns))
        return min(1.0, max(0.0, kept))
    kept = 0.0
    for row in range(grid.rows):
        tall = _covered(top, bottom, row / grid.rows, (row + 1) / grid.rows)
        if tall <= 0:
            continue
        for column in range(grid.columns):
            wide = _covered(left, right, column / grid.columns, (column + 1) / grid.columns)
            kept += grid.weights[row * grid.columns + column] * wide * tall
    return min(1.0, max(0.0, kept))


def faces_judged(key: KeyContent, settings: FitSettings) -> tuple[UnitBox, ...]:
    """The faces rule 3 keeps whole: each of `key.faces` (Immich's boxes, unpadded) whose area is
    at least `settings.min_face_share` of the photo's (a stranger in the background does not
    decide a crop), padded by `settings.face_padding` of its own width and height on each side
    and clamped to the photo. Pure."""
    faces: list[UnitBox] = []
    pad = settings.face_padding
    for face in key.faces:
        width, height = face.right - face.left, face.bottom - face.top
        if width * height < settings.min_face_share:
            continue
        faces.append(UnitBox(left=max(0.0, face.left - pad * width),
                             top=max(0.0, face.top - pad * height),
                             right=min(1.0, face.right + pad * width),
                             bottom=min(1.0, face.bottom + pad * height)))
    return tuple(faces)


def interest_judged(key: KeyContent, settings: FitSettings) -> InterestGrid:
    """The grid rule 4 judges: `key.interest`, or the uniform grid of its size when the saliency
    map's `spread` is under `settings.min_salience_spread` (no part stands out). Pure."""
    grid = key.interest
    if key.spread >= settings.min_salience_spread:
        return grid
    cells = grid.columns * grid.rows
    return InterestGrid(columns=grid.columns, rows=grid.rows, weights=(1.0 / cells,) * cells)


def judge(kind: MediaKind, width: int, height: int, key: KeyContent | None, target: FitTarget,
          crop_limit: float, settings: FitSettings) -> FitVerdict:
    """The whole rule for one photo on one Frame: `shape_fit` first, then (an image) rules 3 and
    4 over the slid windows, `face_cut` when no window keeps every face whole, else
    `interest_cut` when none passes rule 4; a video gets the centred window.
    `Fits.crop_share` is `crop_share(...)`; `Fits.interest_kept` the chosen window's (1.0 for a
    video). Pure; ValueError for an image judged with `key` None (an image is judged only once
    measured) or a crop limit outside (0, 1)."""
    if not 0 < crop_limit < 1:
        raise ValueError("a crop limit inside (0, 1) is required")
    if kind == "image" and key is None:
        raise ValueError("an image is judged only once its key content is measured")
    refused = shape_fit(kind, width, height, target, crop_limit, settings, size_known=True)
    if refused is not None:
        return refused
    share = crop_share(width, height, target.aspect)
    if kind == "video" or key is None:
        return Fits(crop=CENTRED, crop_share=share, interest_kept=1.0)
    aspect = target.aspect
    faces = faces_judged(key, settings)
    windows = [(offset, crop_window(PhotoCrop(offset=offset), width, height, aspect))
               for offset in _offsets(settings.window_steps)]
    whole = [(offset, window) for offset, window in windows
             if all(_inside(face, window) for face in faces)]
    if not whole:
        return DoesNotFit(reason="face_cut")
    grid = interest_judged(key, settings)
    allowed = settings.max_lost_interest_density * share + _SLACK
    passing = {offset: kept for offset, window in whole
               if 1 - (kept := interest_kept(grid, window)) <= allowed}
    if not passing:
        return DoesNotFit(reason="interest_cut")
    offset = place(tuple(passing.items()), free_axis(width, height, aspect), faces)
    return Fits(crop=PhotoCrop(offset=offset), crop_share=share, interest_kept=passing[offset])


def place(passing: Sequence[tuple[float, float]], axis: FreeAxis,
          faces: tuple[UnitBox, ...]) -> float:
    """The offset of the window a photo takes among `passing`, its windows past rules 3 and 4 as
    (offset, interest kept), on its `axis`, with its judged `faces` (`faces_judged`: padded and
    clamped). The one home of placement:
    - no faces: the window keeping the most interest (rounded to 9 digits); ties go to the
      offset nearest 0.5, then the lower;
    - faces: the faces' window. The faces span [lo, hi] along the axis, its spare room is
      length - (hi - lo), and F is the mean of their centres weighted by area, t = (F - lo) /
      (hi - lo); the faces' window is centred at hi - length / 2 + t x room, so the room before
      the faces and after them stand (1 - t) : t (a lone face is centred, a bigger face gets more
      room on its side). The passing offset whose window centre is nearest it wins, distances
      rounded to 9 digits; ties go to the offset nearest 0.5, then the lower. So an aim beyond
      the photo's windows takes the window at that edge, and equal shapes (`axis.length` 1,
      every window the same) the centred offset 0.5. The interest does not move it (the owner's
      pick of 2026-10-25, "faces only", a revisable design choice: saliency scores texture such
      as a floor or foliage, which pulled windows off the faces).
    Pure; ValueError when `passing` is empty."""
    if not passing:
        raise ValueError("no passing window to place")
    if not faces:
        return max(passing, key=lambda pair: (round(pair[1], 9), -abs(pair[0] - 0.5),
                                              -pair[0]))[0]
    spans = [(face.left, face.right) if axis.along == "x" else (face.top, face.bottom)
             for face in faces]
    low, high = min(start for start, _ in spans), max(end for _, end in spans)
    areas = [(face.right - face.left) * (face.bottom - face.top) for face in faces]
    centre = math.fsum(area * (start + end) / 2
                       for area, (start, end) in zip(areas, spans, strict=True)) / math.fsum(areas)
    share = (centre - low) / (high - low)
    half = axis.length / 2
    aim = high - half + share * (axis.length - (high - low))
    return min((offset for offset, _ in passing),
               key=lambda offset: (round(abs(axis.centre(offset) - aim), 9), abs(offset - 0.5),
                                   offset))


@lru_cache(maxsize=4096)
def _marginals(grid: InterestGrid) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """The grid's weight per row and per column (the grid is frozen, so the memo holds)."""
    rows = tuple(math.fsum(grid.weights[row * grid.columns:(row + 1) * grid.columns])
                 for row in range(grid.rows))
    columns = tuple(math.fsum(grid.weights[column::grid.columns])
                    for column in range(grid.columns))
    return rows, columns


@lru_cache(maxsize=16)
def _offsets(steps: int) -> tuple[float, ...]:
    """The window offsets tried: k / steps for k = 0..steps, plus 0.5."""
    return tuple(sorted({k / steps for k in range(steps + 1)} | {0.5}))


def _covered(low: float, high: float, cell_low: float, cell_high: float) -> float:
    """The fraction of [cell_low, cell_high] inside [low, high]."""
    return max(0.0, min(high, cell_high) - max(low, cell_low)) / (cell_high - cell_low)


def _inside(face: UnitBox, window: Window) -> bool:
    left, top, right, bottom = window
    return (face.left >= left - _SLACK and face.right <= right + _SLACK
            and face.top >= top - _SLACK and face.bottom <= bottom + _SLACK)
