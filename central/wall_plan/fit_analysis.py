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

import math
from typing import TYPE_CHECKING, Final, Literal, Self

from pydantic import Field, model_validator

from central.photo_library.model import DraftRef
from central.wall_plan.fit import (
    FitRefusal,
    Fits,
    FitTarget,
    MediaKind,
    band,
    crop_share,
    judge,
    shape_fit,
)
from central.wall_plan.fit_settings import FIT_SETTING_RANGES, FitSettings
from central.wall_plan.key_content import KeyContent
from contracts.models import (
    Calibration,
    FrameProfile,
    Identifier,
    Instant,
    Model,
    PhotoCrop,
    frame_output,
)
from contracts.photo_crop import CENTRED

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
    settings = request.settings or saved
    crop_limit = settings.crop_limit if request.crop_limit is None else request.crop_limit
    outputs = {output.frame_id: output for output in reader.frame_outputs()}
    judged = sorted(set(outputs if request.frames is None else request.frames))
    drafts = {draft.frame_id: draft for draft in request.frame_drafts}
    if any(frame_id not in outputs for frame_id in (*judged, *drafts)):
        raise AnalysisRefused("frame_unknown", 404)
    if any(frame_id not in judged for frame_id in drafts):
        raise AnalysisRefused("draft_frame_not_judged", 422)
    sizes = reader.selection_sizes(request.selection)
    samples = _Samples(reader, request.selection, request.seed, request.tier == "full")
    frames: list[FrameFit] = []
    taken: set[str] = set()
    for frame_id in judged:
        output, draft = outputs[frame_id], drafts.get(frame_id)
        target = FitTarget(profile=output.profile, output_px=output.output_px) if draft is None \
            else FitTarget(profile=draft.profile,
                           output_px=frame_output(draft.profile, draft.calibration,
                                                  output.display_px))
        frame = _frame_fit(frame_id, target, output.display_px, draft is not None, sizes,
                           crop_limit, settings, samples, taken)
        if frame.lead is not None:
            taken.add(frame.lead.asset_id)
        frames.append(frame)
    return FitAnalysis(
        tier=request.tier, crop_limit=crop_limit, settings=settings,
        photos=sum(group.photos for group in sizes.groups if group.kind == "image"),
        videos=sum(group.photos for group in sizes.groups if group.kind == "video"),
        waiting=sizes.waiting, left_out=sizes.left_out, reading=sizes.reading,
        bins=_bins(sizes.groups), frames=tuple(frames), read_at=sizes.read_at)


# The refusals each tier counts, in the fit rule's order (central/wall_plan/fit.py).
_SHAPE_REFUSALS: Final[tuple[FitRefusal, ...]] = ("no_video_here", "beyond_crop_limit",
                                                  "below_frame_pixels")
_CONTENT_REFUSALS: Final[tuple[FitRefusal, ...]] = ("face_cut", "interest_cut")
_Size = tuple[MediaKind, int, int]


class _Samples:
    """The sample reads of one analysis, each asked at most once for its sizes (two Frames that
    refuse the same sizes share one read); none at tier "shape"."""

    def __init__(self, reader: FitAnalysisReader, selection: FitSelection, seed: int,
                 full: bool) -> None:
        self.reader, self.selection, self.seed, self.full = reader, selection, seed, full
        self._read: dict[tuple[frozenset[_Size], int, bool], tuple[SamplePhoto, ...]] = {}

    def of(self, sizes: frozenset[_Size], *, limit: int,
           measured_only: bool) -> tuple[SamplePhoto, ...]:
        if not self.full or not sizes:
            return ()
        key = (sizes, limit, measured_only)
        if key not in self._read:
            self._read[key] = self.reader.sample(self.selection, sizes, seed=self.seed,
                                                 limit=limit, measured_only=measured_only)
        return self._read[key]


def _frame_fit(frame_id: str, target: FitTarget, display_px: tuple[int, int] | None,
               draft: bool, sizes: SelectionSizes, crop_limit: float, settings: FitSettings,
               samples: _Samples, taken: set[str]) -> FrameFit:
    """One Frame's answer: the shape tier over every group, then (tier "full") the content tier
    over a seeded sample of the measured photos that pass it."""
    passing: set[_Size] = set()
    refused: dict[FitRefusal, set[_Size]] = {reason: set() for reason in _SHAPE_REFUSALS}
    counts: dict[FitRefusal, int] = dict.fromkeys(_SHAPE_REFUSALS, 0)
    fit_by_shape = measured = 0
    for group in sizes.groups:
        size = (group.kind, group.width, group.height)
        refusal = shape_fit(group.kind, group.width, group.height, target, crop_limit, settings)
        if refusal is None:
            passing.add(size)
            fit_by_shape += group.photos
            measured += group.measured
        else:
            refused[refusal.reason].add(size)
            counts[refusal.reason] += group.photos
    by_shape = tuple(RefusalCount(
        reason=reason, photos=counts[reason],
        samples=tuple(_sample(photo, None) for photo in samples.of(
            frozenset(refused[reason]), limit=SAMPLES, measured_only=False)))
        for reason in _SHAPE_REFUSALS)
    judged = samples.of(frozenset(passing), limit=CONTENT_SAMPLE, measured_only=True)
    fitting: list[FitSample] = []
    by_content: dict[FitRefusal, list[FitSample]] = {reason: [] for reason in _CONTENT_REFUSALS}
    for photo in judged:
        verdict = judge(photo.kind, photo.width, photo.height, photo.key, target, crop_limit,
                        settings)
        if isinstance(verdict, Fits):
            fitting.append(_sample(photo, verdict.crop))
        else:   # shape and size passed, so the refusal is a content one
            by_content[verdict.reason].append(_sample(photo, None))
    lead = next((sample for sample in fitting if sample.asset_id not in taken), None)
    return FrameFit(
        frame_id=frame_id, aspect=target.profile.width_px / target.profile.height_px,
        output_px=target.output_px, display_px=display_px, draft=draft,
        band=band(target.profile.width_px / target.profile.height_px, crop_limit),
        photos=sum(group.photos for group in sizes.groups), fit_by_shape=fit_by_shape,
        refused_by_shape=by_shape, measured=measured,
        nearest_crop_limit=None if fit_by_shape else _nearest_crop_limit(
            sizes.groups, target, settings),
        checked=len(judged), fit_checked=len(fitting),
        refused_by_content=tuple(RefusalCount(reason=reason, photos=len(found),
                                              samples=tuple(found[:SAMPLES]))
                                 for reason, found in by_content.items()),
        lead=lead, fitting=tuple(fitting[:SAMPLES]))


def _nearest_crop_limit(groups: tuple[SizeGroup, ...], target: FitTarget,
                        settings: FitSettings) -> float | None:
    """The smallest crop limit within FIT_SETTING_RANGES at which some group passes `shape_fit`
    (each group's `crop_share`, raised to the range's floor); None when none does."""
    low, high = FIT_SETTING_RANGES["crop_limit"]
    found = [limit for group in groups
             if (limit := max(low, crop_share(group.width, group.height, target.profile))) <= high
             and shape_fit(group.kind, group.width, group.height, target, limit,
                           settings) is None]
    return min(found, default=None)


def _sample(photo: SamplePhoto, crop: PhotoCrop | None) -> FitSample:
    """A sample photo as the answer draws it: the fit rule's crop, or centred for a refusal."""
    return FitSample(asset_id=photo.asset_id, kind=photo.kind, width=photo.width,
                     height=photo.height, crop=CENTRED if crop is None else crop,
                     thumbnail=f"/v1/operator/library/thumbnails/{photo.asset_id}")


def _bins(groups: tuple[SizeGroup, ...]) -> tuple[RatioBin, ...]:
    """The Shape map: RATIO_BIN_COUNT log-spaced bins of width / height over RATIO_RANGE, the
    first and last also holding every ratio beyond them."""
    low, high = RATIO_RANGE
    span = math.log(high / low)
    edges = [low * math.exp(span * index / RATIO_BIN_COUNT)
             for index in range(RATIO_BIN_COUNT + 1)]
    photos, videos = [0] * RATIO_BIN_COUNT, [0] * RATIO_BIN_COUNT
    for group in groups:
        place = math.floor(RATIO_BIN_COUNT * math.log(group.width / group.height / low) / span)
        index = min(RATIO_BIN_COUNT - 1, max(0, place))
        (photos if group.kind == "image" else videos)[index] += group.photos
    return tuple(RatioBin(low=edges[index], high=edges[index + 1], photos=photos[index],
                          videos=videos[index]) for index in range(RATIO_BIN_COUNT))


__all__ = [
    "CONTENT_SAMPLE", "MAX_DRAFTS", "MAX_SOURCES", "RATIO_BIN_COUNT", "RATIO_RANGE", "SAMPLES",
    "AnalysisRefused", "AnalysisTier", "FitAnalysis", "FitAnalysisRequest", "FitSample",
    "FitSelection", "FrameDraft", "FrameFit", "RatioBin", "RefusalCount", "SamplePhoto",
    "SelectionSizes", "SizeGroup", "analyse",
]
