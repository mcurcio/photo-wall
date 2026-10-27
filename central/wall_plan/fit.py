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
Among the windows passing 3 and 4, `place` picks one: on a photo with faces, the window nearest
the one that keeps the faces' centre at its place in the photo (owner, 2026-10-25: "A lone face
should sit “in the same relative position within the image as the original (if possible), and
cropped only as much as needed to fit the scaled image into the frame."), then moved toward what
stands out by at most `interest_skew` of the gap and of the faces' room (owner, 2026-10-25: "It
could bias towards face centering but also allow a skew to include extra content"); on a photo
without faces, the one keeping the most interest. Windows are tried at offsets k /
`window_steps` for k = 0..`window_steps`, plus 0.5.

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
from typing import Final, Literal, NamedTuple

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


class JudgedInterest(NamedTuple):
    """The interest grid as the rule judges it, and whether it is even (no part stands out): the
    one home of "even", read by rule 4 and by `place` alike."""

    grid: InterestGrid
    even: bool


def interest_judged(key: KeyContent, settings: FitSettings) -> JudgedInterest:
    """The grid rule 4 judges: `key.interest`, or the uniform grid of its size, `even`, when the
    map's `spread` is under `settings.min_salience_spread` (no part stands out). Pure."""
    grid = key.interest
    if key.spread >= settings.min_salience_spread:
        return JudgedInterest(grid, False)
    return JudgedInterest(InterestGrid.uniform(grid.columns, grid.rows), True)


def interest_centre(grid: InterestGrid, along: Literal["x", "y"]) -> float:
    """The grid's centre of mass along `along`, a share of the photo (0..1): each column's (or
    row's) total weight at its middle. Pure."""
    rows, columns = _marginals(grid)
    totals = columns if along == "x" else rows
    return math.fsum(total * (index + 0.5) / len(totals) for index, total in enumerate(totals))


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
    interest = interest_judged(key, settings)
    allowed = settings.max_lost_interest_density * share + _SLACK
    passing = {offset: kept for offset, window in whole
               if 1 - (kept := interest_kept(interest.grid, window)) <= allowed}
    if not passing:
        return DoesNotFit(reason="interest_cut")
    offset = place(tuple(passing.items()), free_axis(width, height, aspect), faces, interest,
                   settings.interest_skew)
    return Fits(crop=PhotoCrop(offset=offset), crop_share=share, interest_kept=passing[offset])


def place(passing: Sequence[tuple[float, float]], axis: FreeAxis, faces: tuple[UnitBox, ...],
          interest: JudgedInterest, skew: float) -> float:
    """The offset of the window a photo takes among `passing`, its windows past rules 3 and 4 as
    (offset, interest kept), on its `axis`, with its judged `faces` (`faces_judged`: padded and
    clamped) and judged `interest` (`interest_judged`). The one home of placement:
    - no faces: the window keeping the most interest (rounded to 9 digits); ties go to the
      offset nearest 0.5, then the lower;
    - faces: first the window keeping the faces' centre at its place in the photo. F is the mean
      of the faces' centres along the axis weighted by their area (a share of the photo, so a
      bigger face pulls harder), and the window at offset F holds F at the same share of its own
      length (its first edge is F x (1 - length)). The passing offset nearest F, o0, is that
      window (`_nearest`). So a face too near the photo's edge for that window moves only as far
      as keeping it whole needs, and equal shapes (`axis.length` 1, every window the same) take
      the centred offset 0.5. The window stays the largest (owner, 2026-10-25, his pick
      "Nothing: least crop": no zoom).
    - then the skew (owner, 2026-10-25: "bias towards face centering but also allow a skew to
      include extra content"): with G the judged grid's centre of mass (`interest_centre`), the
      aim is o0 + `skew` x (G - F), in offset units, but the window's first edge moves at most
      `skew` x r, where r is the least room in o0's window between a judged face and the edge the
      faces move toward (so the offset moves at most `skew` x r / (1 - length)): every face keeps
      at least 1 - `skew` of the room o0 gave it. The window taken is the passing offset nearest
      that aim among those between o0 and the aim (never past it). `skew` 0, or an even grid
      (no part stands out, so it has no side), leaves o0.
    Pure; ValueError when `passing` is empty."""
    if not passing:
        raise ValueError("no passing window to place")
    if not faces:
        return max(passing, key=lambda pair: (round(pair[1], 9), -abs(pair[0] - 0.5),
                                              -pair[0]))[0]
    spans = [(face.left, face.right) if axis.along == "x" else (face.top, face.bottom)
             for face in faces]
    areas = [(face.right - face.left) * (face.bottom - face.top) for face in faces]
    centre = math.fsum(area * (start + end) / 2
                       for area, (start, end) in zip(areas, spans, strict=True)) / math.fsum(areas)
    offsets = [offset for offset, _ in passing]
    kept = _nearest(offsets, axis, centre)
    if skew <= 0 or interest.even or axis.length >= 1:
        return kept
    pull = skew * (interest_centre(interest.grid, axis.along) - centre)
    if pull == 0:
        return kept
    first = axis.start(kept)
    room = (min(start for start, _ in spans) - first if pull > 0
            else first + axis.length - max(end for _, end in spans))
    aim = kept + math.copysign(min(abs(pull), skew * max(room, 0.0) / (1 - axis.length)), pull)
    low, high = min(kept, aim), max(kept, aim)
    return _nearest([offset for offset in offsets if low - _SLACK <= offset <= high + _SLACK],
                    axis, aim)


def _nearest(offsets: Sequence[float], axis: FreeAxis, aim: float) -> float:
    """The offset whose window's first edge is nearest the window at `aim`'s, rounded to 9
    digits; ties go to the offset nearest 0.5, then the lower."""
    return min(offsets, key=lambda offset: (round(abs(axis.start(offset) - axis.start(aim)), 9),
                                            abs(offset - 0.5), offset))


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
