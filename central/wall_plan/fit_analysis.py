"""How a selection of photos fits the wall's Frames: the one analysis read (follow-ups
2026-10-11, slice B2; run ledger .claude/runs/photo-5a.md "Follow-ups (owner 2026-10-11)").

Owner, 2026-10-11: "the real solution should be more visual and allow me to preview the content
selections and transient Frame compatibility. ALSO make sure that compatibility is measuring
against the Frame output and not the Display"; "Note that this preview idea could apply at a few
different moments. Like the source creation time, but also frame config, show authoring, etc."

So it is ONE read with every input saved or a draft: the selection (saved Sources and unsaved
Photo source drafts), the Frames (saved, or a draft profile and calibration for a Frame), the
crop limit and the tuning values. Every console moment (Photo source editor, Frame page, Scene
editor, Show now, Settings › Photo fit, Sources list) asks here and nowhere else.

It is judged by the fit rule's one home (central/wall_plan/fit.py), in two tiers (plan D8):
- shape and size, complete: `shape_fit` once per (kind, width, height) group of the selection's
  in-pool photos and per Frame; the ratio histogram and each Frame's `band` from the same
  arithmetic. The only tier at `tier="shape"` (the console's slider drags);
- faces and subject, sampled: per Frame up to CONTENT_SAMPLE measured photos that pass shape and
  size, in a seeded order, judged by `judge`.
Samples: per Frame up to SAMPLES photos that fit (with their crops) and up to SAMPLES per
refusal; one lead sample per Frame, never the same photo for two Frames (the wall plan's rule).

Pure over its port (`central.wall_plan.ports.FitAnalysisReader`); budget: 50,000 photos and 8
Frames within 1.5 s on the lane's machine, gated at 5 s by a test.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal, Self

from pydantic import Field, model_validator

from central.photo_library.model import DraftRef
from central.wall_plan.fit import FitRefusal, MediaKind
from central.wall_plan.fit_settings import FIT_SETTING_RANGES, FitSettings
from central.wall_plan.key_content import KeyContent
from contracts.models import Calibration, FrameProfile, Identifier, Instant, Model, PhotoCrop

if TYPE_CHECKING:
    from central.wall_plan.ports import FitAnalysisReader

# Per Frame, the most measured photos the content tier judges (F-FU-8: "checked N of M").
CONTENT_SAMPLE: Final = 64
# Per Frame, the most samples shown that fit, and per refusal reason.
SAMPLES: Final = 6
# The ratio histogram's bins: log-spaced photo ratios (width / height) from 1:4 to 4:1, so 9:16
# and 16:9 sit symmetric about 1:1; the first and last bins also hold everything beyond.
RATIO_BIN_COUNT: Final = 33
RATIO_RANGE: Final = (0.25, 4.0)
MAX_SOURCES: Final = 32
MAX_DRAFTS: Final = 8

AnalysisTier = Literal["shape", "full"]


class AnalysisRefused(Exception):
    """A request naming what does not exist: `code` and `status` answer {"error": code}.
    Codes: "source_unknown" (404), "draft_unknown" (404), "frame_unknown" (404)."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code, self.status = code, status


class FitSelection(Model):
    """Which photos: the union of saved Source revisions' and unsaved drafts' in-pool photos,
    each photo counted once. Invariant (validator): at least one Source or draft."""

    sources: tuple[Identifier, ...] = Field(default=(), max_length=MAX_SOURCES)
    drafts: tuple[DraftRef, ...] = Field(default=(), max_length=MAX_DRAFTS)

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if not self.sources and not self.drafts:
            raise ValueError("a selection names at least one Source or draft")
        return self


class FrameDraft(Model):
    """A saved Frame as it would be configured (the Frame config moment): its draft profile and
    Position, judged with the size its Output last reported. The console sends both, its saved
    values for the one it did not change."""

    frame_id: Identifier
    profile: FrameProfile
    calibration: Calibration


class FitAnalysisRequest(Model):
    """POST /v1/operator/photo-fit/analysis. `frames` None: every Frame of the wall, by id.
    A `frame_drafts` entry replaces that Frame's saved profile and Position (its id must be
    judged). `crop_limit` None: `settings.crop_limit`. `settings` None: the saved settings.
    `seed` reorders the samples (Shuffle)."""

    selection: FitSelection
    frames: tuple[Identifier, ...] | None = Field(default=None, max_length=64)
    frame_drafts: tuple[FrameDraft, ...] = Field(default=(), max_length=8)
    crop_limit: float | None = Field(default=None, ge=FIT_SETTING_RANGES["crop_limit"][0],
                                     le=FIT_SETTING_RANGES["crop_limit"][1], allow_inf_nan=False)
    settings: FitSettings | None = None
    tier: AnalysisTier = "full"
    seed: int = Field(default=0, ge=0, le=2**31 - 1)


# --- The reader's answers (central/infra/fit_analysis_store.py) ---------------------------------

class SizeGroup(Model):
    """The selection's in-pool photos of one kind and size (upright, edited): how many, and how
    many have key content measured at the current analysis version."""

    kind: MediaKind
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    photos: int = Field(ge=1)
    measured: int = Field(ge=0)


class SelectionSizes(Model):
    """The grouped read of a selection (one database read). `reading`: some pool of the
    selection has a check running or has never completed one (the console says "so far")."""

    groups: tuple[SizeGroup, ...]
    waiting: int = Field(ge=0)
    left_out: int = Field(ge=0)
    reading: bool
    read_at: Instant


class SamplePhoto(Model):
    """One photo of the selection as a sample read returns it; `key` None: not measured."""

    asset_id: Identifier
    kind: MediaKind
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    key: KeyContent | None = None


# --- The answer ---------------------------------------------------------------------------------

class RatioBin(Model):
    """One bar of the Shape map: photos whose ratio is in [low, high)."""

    low: float = Field(gt=0)
    high: float = Field(gt=0)
    photos: int = Field(ge=0)
    videos: int = Field(ge=0)


class FitSample(Model):
    """A photo drawn on a Frame: `crop` is the window the Frame would show (the fit rule's for a
    photo that fits; centred for a refused one). `thumbnail` is Central's thumbnail route for it
    (`/v1/operator/library/thumbnails/{asset_id}`); no Immich id (R22)."""

    asset_id: Identifier
    kind: MediaKind
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    crop: PhotoCrop
    thumbnail: str


class RefusalCount(Model):
    reason: FitRefusal
    photos: int = Field(ge=0)
    samples: tuple[FitSample, ...] = Field(default=(), max_length=SAMPLES)


class FrameFit(Model):
    """One Frame's answer. Shape tier (complete): `fit_by_shape` of `photos` pass `shape_fit`;
    `refused_by_shape` holds no_video_here, beyond_crop_limit and below_frame_pixels in the
    rule's order (0 included); `measured` of `fit_by_shape` have key content;
    `nearest_crop_limit`: when `fit_by_shape` is 0, the smallest crop limit within
    FIT_SETTING_RANGES at which some photo of the selection would pass `shape_fit` (the console's
    "Raise the crop limit to N %"), else None. Content tier
    (sampled; all 0 and empty at tier "shape"): `checked` photos judged by `judge`, `fit_checked`
    of them fit, `refused_by_content` holds face_cut and interest_cut. `output_px` None: judged
    by shape only (its Output has not reported a size). `draft`: judged as a FrameDraft."""

    frame_id: Identifier
    aspect: float = Field(gt=0)
    output_px: tuple[int, int] | None
    display_px: tuple[int, int] | None
    draft: bool
    band: tuple[float, float]
    photos: int = Field(ge=0)
    fit_by_shape: int = Field(ge=0)
    refused_by_shape: tuple[RefusalCount, ...]
    measured: int = Field(ge=0)
    nearest_crop_limit: float | None = None
    checked: int = Field(ge=0, le=CONTENT_SAMPLE)
    fit_checked: int = Field(ge=0)
    refused_by_content: tuple[RefusalCount, ...]
    lead: FitSample | None
    fitting: tuple[FitSample, ...] = Field(default=(), max_length=SAMPLES)


class FitAnalysis(Model):
    """The answer: the selection's counts, its Shape map, one FrameFit per judged Frame (in frame
    id order), and the crop limit and settings it was judged at."""

    tier: AnalysisTier
    crop_limit: float
    settings: FitSettings
    photos: int = Field(ge=0)
    videos: int = Field(ge=0)
    waiting: int = Field(ge=0)
    left_out: int = Field(ge=0)
    reading: bool
    bins: tuple[RatioBin, ...]
    frames: tuple[FrameFit, ...]
    read_at: Instant


def analyse(request: FitAnalysisRequest, reader: FitAnalysisReader,
            saved: FitSettings) -> FitAnalysis:
    """The analysis (module docstring). Reads: one `selection_sizes`, one `frame_outputs`, and
    at tier "full" at most one `sample` call per judged Frame for the content tier plus one per
    refusal reason that has photos. AnalysisRefused for an unknown frame id in `frames` or
    `frame_drafts` ("frame_unknown"); the reader raises it for an unknown Source or draft."""
    raise NotImplementedError


__all__ = [
    "CONTENT_SAMPLE", "MAX_DRAFTS", "MAX_SOURCES", "RATIO_BIN_COUNT", "RATIO_RANGE", "SAMPLES",
    "AnalysisRefused", "AnalysisTier", "FitAnalysis", "FitAnalysisRequest", "FitSample",
    "FitSelection", "FrameDraft", "FrameFit", "RatioBin", "RefusalCount", "SamplePhoto",
    "SelectionSizes", "SizeGroup", "analyse",
]
