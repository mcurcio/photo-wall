"""The photo fit rule (roadmap 5a design rule 2; run ledger .claude/runs/photo-5a.md, slice R1):
whether a photo fills a Frame with its key content kept, and the crop that does it. One home: the
Planner, the authoring check and the candidates route all ask here; the worker only measures and
the Player only draws.

Owner, 2026-10-11: "a photo either fills the frame or it doesn't show (it's incompatible with that
frame)"; "It's about the ratio, not strict labels"; "Smart crop sounds reasonable". So there are
no landscape or portrait labels, no mats and no pairs: a photo fits a Frame when some window with the
Frame profile's aspect, slid along the photo's free axis (contracts/photo_crop.py):
1. removes no more than the crop limit of the photo's area (`crop_share`; the backstop),
2. has at least the Frame's pixels in the original (`profile.width_px` x `profile.height_px`),
3. keeps every face whole (each `UnitBox` inside the window), and
4. cuts no key content: the share of the interest grid it removes (1 - `interest_kept`) is at
   most MAX_LOST_INTEREST_DENSITY x the share of the area it removes (`crop_share`). A side that
   holds no more than its share of the photo's interest may go (owner, 2026-10-11: "if the key
   content is on the left and the right is benign, then it would be reasonable to crop the
   picture so that the left edge is preserved"); one holding key content may not ("if it has key
   content on the sides then it can't be cropped to fit"). An even spread of interest passes.
Among the windows passing 3 and 4, the one keeping the most interest wins; ties go to the window
nearest the centre, then the lower offset. Windows are tried at offsets k / WINDOW_STEPS for
k = 0..WINDOW_STEPS, plus 0.5.

Videos (design "Hand-placed, stacks, video"): a centred window, rule 1, and requirements :186's
floor instead of rule 2 (a Frame of 40 inches or more refuses a video whose shorter side is under
1080 pixels); a Frame whose profile has `video` off shows none. Key content is ignored.
"""
from __future__ import annotations

from typing import Final, Literal

from central.wall_plan.key_content import InterestGrid, KeyContent, UnitBox
from contracts.models import Fraction, FrameProfile, Model, PhotoCrop
from contracts.photo_crop import CENTRED, Window, crop_window

# The Scene setting's default (design "Crop limit": 35 %, between 1:1 on 3:2, 33 %, and 4:5 on
# 5:4, 36 %). SceneSelection's default is this value.
DEFAULT_CROP_LIMIT: Final = 0.35
# Rule 4's limit: how much denser in interest than the photo's average the removed part may be.
# A placeholder to tune on the owner's library (run ledger finding F-2; the design's parked
# "enough interest, placeholder 90 %" refused every evenly spread 4:3 photo on a 16:9 Frame).
MAX_LOST_INTEREST_DENSITY: Final = 1.25
# How many steps the window is slid across its free axis.
WINDOW_STEPS: Final = 64
# requirements :186: on a Frame this large a video needs this shorter side.
LARGE_FRAME_INCHES: Final = 40
LARGE_FRAME_VIDEO_SHORT_SIDE: Final = 1080
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
    raise NotImplementedError


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


def shape_fit(kind: MediaKind, width: int, height: int, profile: FrameProfile,
              crop_limit: float) -> DoesNotFit | None:
    """The size-only part of the rule, usable before key content is measured: `no_video_here`
    (a video on a profile with `video` off), then `beyond_crop_limit` (rule 1), then
    `below_frame_pixels` (rule 2 for an image; the :186 floor for a video). None: the photo
    passes on its size alone. `width` and `height` are the original's, upright. Pure."""
    if kind == "video" and not profile.video:
        return DoesNotFit(reason="no_video_here")
    if crop_share(width, height, profile) > crop_limit + _SLACK:
        return DoesNotFit(reason="beyond_crop_limit")
    if kind == "video":
        large = profile.diagonal_inches >= LARGE_FRAME_INCHES
        if large and min(width, height) < LARGE_FRAME_VIDEO_SHORT_SIDE:
            return DoesNotFit(reason="below_frame_pixels")
        return None
    # The window keeps the Frame's aspect, so its pixels against the Frame's is one comparison.
    window_pixels = width * height * (1 - crop_share(width, height, profile))
    if window_pixels < profile.width_px * profile.height_px * (1 - _SLACK):
        return DoesNotFit(reason="below_frame_pixels")
    return None


def interest_kept(grid: InterestGrid, window: Window) -> float:
    """The share of the grid's weight inside `window` (UV fractions), each cell counted by the
    fraction of its area the window covers. Pure."""
    left, top, right, bottom = window
    kept = 0.0
    for row in range(grid.rows):
        tall = _covered(top, bottom, row / grid.rows, (row + 1) / grid.rows)
        if tall <= 0:
            continue
        for column in range(grid.columns):
            wide = _covered(left, right, column / grid.columns, (column + 1) / grid.columns)
            kept += grid.weights[row * grid.columns + column] * wide * tall
    return min(1.0, max(0.0, kept))


def judge(kind: MediaKind, width: int, height: int, key: KeyContent | None,
          profile: FrameProfile, crop_limit: float) -> FitVerdict:
    """The whole rule for one photo on one Frame profile: `shape_fit` first, then (an image)
    rules 3 and 4 over the slid windows, `face_cut` when no window keeps every face whole, else
    `interest_cut` when none passes rule 4; a video gets the centred window.
    `Fits.crop_share` is `crop_share(...)`; `Fits.interest_kept` the chosen window's (1.0 for a
    video). Pure; ValueError for an image judged with `key` None (an image is judged only once
    measured) or a crop limit outside (0, 1)."""
    if not 0 < crop_limit < 1:
        raise ValueError("a crop limit inside (0, 1) is required")
    if kind == "image" and key is None:
        raise ValueError("an image is judged only once its key content is measured")
    refused = shape_fit(kind, width, height, profile, crop_limit)
    if refused is not None:
        return refused
    share = crop_share(width, height, profile)
    if kind == "video" or key is None:
        return Fits(crop=CENTRED, crop_share=share, interest_kept=1.0)
    aspect = _aspect(profile)
    windows = [(offset, crop_window(PhotoCrop(offset=offset), width, height, aspect))
               for offset in _OFFSETS]
    whole = [(offset, window) for offset, window in windows
             if all(_inside(face, window) for face in key.faces)]
    if not whole:
        return DoesNotFit(reason="face_cut")
    allowed = MAX_LOST_INTEREST_DENSITY * share + _SLACK
    passing = [(kept, offset) for offset, window in whole
               if 1 - (kept := interest_kept(key.interest, window)) <= allowed]
    if not passing:
        return DoesNotFit(reason="interest_cut")
    kept, offset = max(passing, key=lambda pair: (round(pair[0], 9), -abs(pair[1] - 0.5),
                                                  -pair[1]))
    return Fits(crop=PhotoCrop(offset=offset), crop_share=share, interest_kept=kept)


_OFFSETS: Final = tuple(sorted({k / WINDOW_STEPS for k in range(WINDOW_STEPS + 1)} | {0.5}))


def _aspect(profile: FrameProfile) -> float:
    return profile.width_px / profile.height_px


def _covered(low: float, high: float, cell_low: float, cell_high: float) -> float:
    """The fraction of [cell_low, cell_high] inside [low, high]."""
    return max(0.0, min(high, cell_high) - max(low, cell_low)) / (cell_high - cell_low)


def _inside(face: UnitBox, window: Window) -> bool:
    left, top, right, bottom = window
    return (face.left >= left - _SLACK and face.right <= right + _SLACK
            and face.top >= top - _SLACK and face.bottom <= bottom + _SLACK)
