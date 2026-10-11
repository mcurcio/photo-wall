"""The photo fit rule (roadmap 5a design rule 2; run ledger .claude/runs/photo-5a.md, slice R1):
whether a photo fills a Frame with its key content kept, and the crop that does it. One home: the
Planner, the authoring check and the candidates route all ask here; the worker only measures and
the Player only draws.

Owner, 2026-10-11: "a photo either fills the frame or it doesn't show (it's incompatible with that
frame)"; "It's about the ratio, not strict labels"; "Smart crop sounds reasonable". So there are
no orientation labels, no mats and no pairs: a photo fits a Frame when some window with the
Frame profile's aspect, slid along the photo's free axis (contracts/photo_crop.py):
1. removes no more than the crop limit of the photo's area (`crop_share`; the backstop),
2. has at least the Frame's pixels in the original (`profile.width_px` x `profile.height_px`),
3. keeps every face whole (each `UnitBox` inside the window), and
4. keeps at least ENOUGH_INTEREST of the interest grid (`interest_kept`).
Among the windows passing 3 and 4, the one keeping the most interest wins; ties go to the window
nearest the centre, then the lower offset. Windows are tried at offsets k / WINDOW_STEPS for
k = 0..WINDOW_STEPS, plus 0.5.

Videos (design "Hand-placed, stacks, video"): a centred window, rule 1, and requirements :186's
floor instead of rule 2 (a Frame of 40 inches or more refuses a video whose shorter side is under
1080 pixels); a Frame whose profile has `video` off shows none. Key content is ignored.
"""
from __future__ import annotations

from typing import Final, Literal

from central.wall_plan.key_content import InterestGrid, KeyContent
from contracts.models import Fraction, FrameProfile, Model, PhotoCrop
from contracts.photo_crop import Window

# The Scene setting's default (design "Crop limit": 35 %, between 1:1 on 3:2, 33 %, and 4:5 on
# 5:4, 36 %). SceneSelection's default is this value.
DEFAULT_CROP_LIMIT: Final = 0.35
# Rule 4's threshold (design parked note: placeholder 90 %).
ENOUGH_INTEREST: Final = 0.90
# How many steps the window is slid across its free axis.
WINDOW_STEPS: Final = 64
# requirements :186: on a Frame this large a video needs this shorter side.
LARGE_FRAME_INCHES: Final = 40
LARGE_FRAME_VIDEO_SHORT_SIDE: Final = 1080

FitRefusal = Literal[
    "beyond_crop_limit",     # "doesn't fit this Frame's shape"
    "below_frame_pixels",    # "too small for this Frame"
    "face_cut",              # "a face would be cut"
    "too_little_interest",   # "the interesting part would be cut"
    "no_video_here",         # "this Frame shows no videos"
]
MediaKind = Literal["image", "video"]


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
    raise NotImplementedError


def shape_fit(kind: MediaKind, width: int, height: int, profile: FrameProfile,
              crop_limit: float) -> DoesNotFit | None:
    """The size-only part of the rule, usable before key content is measured: `no_video_here`
    (a video on a profile with `video` off), then `beyond_crop_limit` (rule 1), then
    `below_frame_pixels` (rule 2 for an image; the :186 floor for a video). None: the photo
    passes on its size alone. `width` and `height` are the original's, upright. Pure."""
    raise NotImplementedError


def interest_kept(grid: InterestGrid, window: Window) -> float:
    """The share of the grid's weight inside `window` (UV fractions), each cell counted by the
    fraction of its area the window covers. Pure."""
    raise NotImplementedError


def judge(kind: MediaKind, width: int, height: int, key: KeyContent | None,
          profile: FrameProfile, crop_limit: float) -> FitVerdict:
    """The whole rule for one photo on one Frame profile: `shape_fit` first, then (an image)
    rules 3 and 4 over the slid windows, `face_cut` when no window keeps every face whole, else
    `too_little_interest` when none keeps ENOUGH_INTEREST; a video gets the centred window.
    `Fits.crop_share` is `crop_share(...)`; `Fits.interest_kept` the chosen window's (1.0 for a
    video). Pure; ValueError for an image judged with `key` None (an image is judged only once
    measured) or a crop limit outside (0, 1)."""
    raise NotImplementedError
