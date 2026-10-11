"""The photo fit rule (roadmap 5a design rule 2; run ledger .claude/runs/photo-5a.md, slice R1):
whether a photo fills a Frame with its key content kept, and the crop that does it. One home: the
Planner, the authoring check, the candidates route and the fit analysis all ask here; the worker
only measures and the Player only draws.

Owner, 2026-10-11: "a photo either fills the frame or it doesn't show (it's incompatible with that
frame)"; "It's about the ratio, not strict labels"; "Smart crop sounds reasonable". So there are
no landscape or portrait labels, no mats and no pairs: a photo fits a Frame (a `FitTarget`) when
some window with the Frame profile's aspect, slid along the photo's free axis
(contracts/photo_crop.py):
1. removes no more than the crop limit of the photo's area (`crop_share`; the backstop),
2. holds at least the Frame output in the original's pixels (`contracts.frame_fit.window_holds`
   against `FitTarget.output_px`; owner, 2026-10-11: compatibility "measuring against the Frame
   output and not the Display"); skipped for a Frame whose Output never reported a size
   (a guess would refuse photos on a guess; the rendition choice, which only sizes a fetch,
   guesses the profile's pixels instead: media/library_client.py `rendition_needs`),
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
Among the windows passing 3 and 4, the one keeping the most interest wins; ties go to the window
nearest the centre, then the lower offset. Windows are tried at offsets k / `window_steps` for
k = 0..`window_steps`, plus 0.5.

Every tuning value is a `FitSettings` field (central/wall_plan/fit_settings.py; owner,
2026-10-11: "Agreed that these should be configurable, and not hardcoded."), passed in by the
caller; faces and spread are applied here, at judge time, so a change needs no new measurement.

Videos (design "Hand-placed, stacks, video"): a centred window, rule 1, and requirements :186's
floor instead of rule 2 (a Frame of `large_frame_inches` or more refuses a video whose shorter
side is under `large_frame_video_short_side`); a Frame whose profile has `video` off shows none.
Key content is ignored.
"""
from __future__ import annotations

import math
from functools import lru_cache
from typing import Final, Literal

from central.wall_plan.fit_settings import FitSettings
from central.wall_plan.key_content import InterestGrid, KeyContent, UnitBox
from contracts.frame_fit import window_holds
from contracts.models import Fraction, FrameProfile, Model, PhotoCrop
from contracts.photo_crop import CENTRED, Window, crop_window

# Float slack for the rule's comparisons: a boundary case (4:3 on 16:9 at a 25 % limit, a photo of
# exactly the Frame's pixels) is judged as its exact arithmetic says.
_SLACK: Final = 1e-9

FitRefusal = Literal[
    "beyond_crop_limit",     # "doesn't fit this Frame's shape"
    "below_frame_pixels",    # "too small for this Frame"
    "face_cut",              # "a face would be cut"
    "interest_cut",          # "the interesting part would be cut"
    "no_video_here",         # "this Frame shows no videos"
]
MediaKind = Literal["image", "video"]


class FitTarget(Model):
    """A Frame as the photo fit rule judges it (follow-ups 2026-10-11, slice B1; owner,
    2026-10-11: compatibility "measuring against the Frame output and not the Display"): its
    profile (the shape `width_px / height_px`, `video`, `diagonal_inches`) and its Frame output
    (`central.installation_models.FrameOutput.output_px`; None until its Output reports a size,
    when rule 2 is skipped and the analysis says "judged by shape only"). Hashable: the rule's
    memo is keyed by it."""

    profile: FrameProfile
    output_px: tuple[int, int] | None = None


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


def crop_share(width: int, height: int, profile: FrameProfile) -> float:
    """The share of the photo's area the largest profile-aspect window removes: 1 - (the
    smaller of the two aspects / the larger). 0 for equal aspects. Pure; ValueError for a
    non-positive size."""
    if width <= 0 or height <= 0:
        raise ValueError("positive photo dimensions required")
    photo, frame = width / height, _aspect(profile)
    return 1 - min(photo, frame) / max(photo, frame)


def shape_fit(kind: MediaKind, width: int, height: int, target: FitTarget, crop_limit: float,
              settings: FitSettings) -> DoesNotFit | None:
    """The size-only part of the rule, usable before key content is measured: `no_video_here`
    (a video on a profile with `video` off), then `beyond_crop_limit` (rule 1), then
    `below_frame_pixels` (rule 2 for an image, against `target.output_px` and skipped when it is
    None; the :186 floor for a video). None: the photo passes on its size alone. `width` and
    `height` are the original's, upright. Pure."""
    profile = target.profile
    if kind == "video" and not profile.video:
        return DoesNotFit(reason="no_video_here")
    if crop_share(width, height, profile) > crop_limit + _SLACK:
        return DoesNotFit(reason="beyond_crop_limit")
    if kind == "video":
        large = profile.diagonal_inches >= settings.large_frame_inches
        if large and min(width, height) < settings.large_frame_video_short_side:
            return DoesNotFit(reason="below_frame_pixels")
        return None
    if target.output_px is not None and not window_holds(width, height, _aspect(profile),
                                                         target.output_px):
        return DoesNotFit(reason="below_frame_pixels")
    return None


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
    refused = shape_fit(kind, width, height, target, crop_limit, settings)
    if refused is not None:
        return refused
    profile = target.profile
    share = crop_share(width, height, profile)
    if kind == "video" or key is None:
        return Fits(crop=CENTRED, crop_share=share, interest_kept=1.0)
    aspect = _aspect(profile)
    faces = faces_judged(key, settings)
    windows = [(offset, crop_window(PhotoCrop(offset=offset), width, height, aspect))
               for offset in _offsets(settings.window_steps)]
    whole = [(offset, window) for offset, window in windows
             if all(_inside(face, window) for face in faces)]
    if not whole:
        return DoesNotFit(reason="face_cut")
    grid = interest_judged(key, settings)
    allowed = settings.max_lost_interest_density * share + _SLACK
    passing = [(kept, offset) for offset, window in whole
               if 1 - (kept := interest_kept(grid, window)) <= allowed]
    if not passing:
        return DoesNotFit(reason="interest_cut")
    kept, offset = max(passing, key=lambda pair: (round(pair[0], 9), -abs(pair[1] - 0.5),
                                                  -pair[1]))
    return Fits(crop=PhotoCrop(offset=offset), crop_share=share, interest_kept=kept)


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


def _aspect(profile: FrameProfile) -> float:
    return profile.width_px / profile.height_px


def _covered(low: float, high: float, cell_low: float, cell_high: float) -> float:
    """The fraction of [cell_low, cell_high] inside [low, high]."""
    return max(0.0, min(high, cell_high) - max(low, cell_low)) / (cell_high - cell_low)


def _inside(face: UnitBox, window: Window) -> bool:
    left, top, right, bottom = window
    return (face.left >= left - _SLACK and face.right <= right + _SLACK
            and face.top >= top - _SLACK and face.bottom <= bottom + _SLACK)
