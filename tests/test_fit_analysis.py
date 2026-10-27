"""The fit analysis, one read for every moment (follow-ups 2026-10-11, slice B2, cases 1-7; run
ledger .claude/runs/photo-5a.md). Owner, 2026-10-11: "the real solution should be more visual and
allow me to preview the content selections and transient Frame compatibility"; "this preview
idea could apply at a few different moments".

`analyse` runs over an in-test reader that records its calls (the tiers' read bounds are proven
by the call log); the fit rule itself is the oracle."""
from __future__ import annotations

import hashlib
import math
from collections import Counter

import pytest
from support.frame_outputs import frame_output_of
from support.key_content_builders import banded_grid, key_content

from central.installation_models import FrameOutput
from central.wall_plan.fit import band, crop_share, fit_target, judge, shape_fit
from central.wall_plan.fit_analysis import (
    CONTENT_SAMPLE,
    RATIO_BIN_COUNT,
    AnalysisRefused,
    FitAnalysisRequest,
    FitSelection,
    FrameDraft,
    SamplePhoto,
    SelectionSizes,
    SizeGroup,
    analyse,
)
from central.wall_plan.fit_settings import FitSettings
from central.wall_plan.key_content import UnitBox
from contracts.models import Calibration, PositionBox

DISPLAY = (3840, 2160)
# Each Display's size across, in inches.
WIDE = 55
TALL = 32
FOUR_THREE = 20
HOUSE = FitSettings()
SELECTION = FitSelection(sources=("family:1",))


def output(frame_id: str, inches: float, display=DISPLAY) -> FrameOutput:
    """A Frame on the whole screen of a Display of `display` pixels, `inches` across: its Frame
    output is the display's pixels."""
    return frame_output_of(frame_id, display, inches=inches)


class Reader:
    """FitAnalysisReader over a list of photos, recording every call."""

    def __init__(self, photos: list[SamplePhoto], outputs: tuple[FrameOutput, ...],
                 display_mm: dict[str, tuple[int, int] | None] | None = None) -> None:
        self.photos, self.outputs, self.calls = photos, outputs, []
        self.display_mm = display_mm or {}

    def selection_sizes(self, selection: FitSelection) -> SelectionSizes:
        self.calls.append(("selection_sizes",))
        photos = Counter((p.kind, p.width, p.height) for p in self.photos)
        measured = Counter((p.kind, p.width, p.height) for p in self.photos if p.key is not None)
        return SelectionSizes(groups=tuple(SizeGroup(kind=kind, width=width, height=height,
                                                     photos=n, measured=measured[(kind, width, height)])
                                           for (kind, width, height), n in sorted(photos.items())),
                              waiting=2, left_out=1, reading=False, read_at=1000)

    def sample(self, selection, sizes, *, seed, limit, measured_only):
        self.calls.append(("sample", frozenset(sizes), limit, measured_only))
        found = [p for p in self.photos if (p.kind, p.width, p.height) in sizes
                 and (p.key is not None or not measured_only)]
        found.sort(key=lambda p: hashlib.md5(f"{p.asset_id}:{seed}".encode()).hexdigest())
        return tuple(found[:limit])

    def frame_outputs(self):
        self.calls.append(("frame_outputs",))
        return self.outputs

    def display_sizes(self):
        self.calls.append(("display_sizes",))
        return dict(self.display_mm)


def photo(number: int, width: int, height: int, *, measured: bool = True, kind="image",
          grid=None) -> SamplePhoto:
    return SamplePhoto(asset_id=f"asset-{number:04d}", kind=kind, width=width, height=height,
                       key=key_content(width, height, grid=grid) if measured and kind == "image"
                       else None)


MIX = ((4000, 3000), (3000, 4000), (6000, 4000), (3840, 2160), (2160, 3840), (1600, 900),
       (4000, 4000), (1080, 1920), (8000, 2000), (640, 480))


def mixed(count: int = 200) -> list[SamplePhoto]:
    return [photo(n, *MIX[n % len(MIX)], measured=n % 3 != 0) for n in range(count)]


def request(**fields) -> FitAnalysisRequest:
    return FitAnalysisRequest(selection=SELECTION, **fields)


def three_frames() -> tuple[FrameOutput, ...]:
    return (output("hall", WIDE), output("stairs", TALL, (1080, 1920)),
            output("study", FOUR_THREE, (1440, 1080)))


def test_the_shape_tier_is_the_fit_rule_photo_by_photo() -> None:
    """Case 1: each Frame's fit_by_shape and refused_by_shape counts equal shape_fit summed over
    the individual photos."""
    photos, frames = mixed(), three_frames()
    answer = analyse(request(tier="shape"), Reader(photos, frames), HOUSE)
    assert [frame.frame_id for frame in answer.frames] == ["hall", "stairs", "study"]
    for frame, saved in zip(answer.frames, frames, strict=True):
        target = fit_target(saved)
        verdicts = [shape_fit(p.kind, p.width, p.height, target, HOUSE.crop_limit, HOUSE,
                              size_known=True) for p in photos]
        assert frame.fit_by_shape == sum(v is None for v in verdicts)
        refused = Counter(v.reason for v in verdicts if v is not None)
        assert {count.reason: count.photos for count in frame.refused_by_shape} == {
            reason: refused.get(reason, 0)
            for reason in ("no_video_here", "beyond_crop_limit", "below_frame_pixels")}
        assert frame.measured == sum(v is None and p.key is not None
                                     for v, p in zip(verdicts, photos, strict=True))
        assert frame.photos == len(photos)
        assert frame.output_px == saved.output_px and frame.draft is False
        assert frame.band == band(saved.output_px[0] / saved.output_px[1], .35)
    assert (answer.photos, answer.videos, answer.waiting, answer.left_out) == (200, 0, 2, 1)


def test_the_shape_map_bins_every_photo_by_its_ratio() -> None:
    """Case 2: twelve 3:2 photos land in the bin holding 1.5; the bins hold every photo and
    video of the selection."""
    photos = [photo(n, 6000, 4000) for n in range(12)]
    photos += [photo(100 + n, 1920, 1080, kind="video") for n in range(3)]
    photos += [photo(200, 100, 1000), photo(201, 1000, 100)]          # beyond the range
    answer = analyse(request(tier="shape"), Reader(photos, three_frames()), HOUSE)
    assert len(answer.bins) == RATIO_BIN_COUNT
    holding = next(b for b in answer.bins if b.low <= 1.5 < b.high)
    assert holding.photos == 12
    assert sum(b.photos + b.videos for b in answer.bins) == answer.photos + answer.videos == 17
    assert answer.bins[0].photos == 1 and answer.bins[-1].photos == 1
    assert math.isclose(answer.bins[0].low, .25) and math.isclose(answer.bins[-1].high, 4)


def test_a_frame_draft_is_judged_with_its_own_position() -> None:
    """Case 3: a mat opening of 40 % x 40 % on the hall's Display needs 1,536 x 864, so the
    1,600 x 900 photos fit the draft; the saved Frames' answers are unchanged and only the draft
    says so."""
    photos, frames = mixed(), three_frames()
    saved = analyse(request(tier="shape"), Reader(photos, frames), HOUSE)
    mat = Calibration(fit=PositionBox(left=.3, top=.3, width=.4, height=.4))      # a centred 40 % x 40 % opening
    drafted = analyse(request(tier="shape", frame_drafts=(
        FrameDraft(frame_id="hall", calibration=mat),)),
        Reader(photos, frames), HOUSE)
    hall, draft = saved.frames[0], drafted.frames[0]
    small = sum((p.width, p.height) == (1600, 900) for p in photos)
    assert draft.output_px == (1536, 864) and draft.draft is True
    assert draft.fit_by_shape == hall.fit_by_shape + small
    assert drafted.frames[1:] == saved.frames[1:]
    assert all(not frame.draft for frame in drafted.frames[1:])


def test_draft_settings_judge_the_whole_answer() -> None:
    """Case 4: draft settings with a 20 % crop limit narrow every band and lower the fits; the
    answer carries the draft settings."""
    photos = mixed()
    house = analyse(request(tier="shape"), Reader(photos, three_frames()), HOUSE)
    narrow = FitSettings(crop_limit=.2)
    drafted = analyse(request(tier="shape", settings=narrow), Reader(photos, three_frames()),
                      HOUSE)
    assert drafted.settings == narrow and drafted.crop_limit == .2
    for before, after in zip(house.frames, drafted.frames, strict=True):
        assert after.band[0] > before.band[0] and after.band[1] < before.band[1]
        assert after.fit_by_shape <= before.fit_by_shape
    assert sum(f.fit_by_shape for f in drafted.frames) < sum(f.fit_by_shape for f in house.frames)
    # The request's own crop limit wins over the settings'.
    explicit = analyse(request(tier="shape", crop_limit=.5), Reader(photos, three_frames()), narrow)
    assert explicit.crop_limit == .5 and explicit.settings == narrow


def test_draft_settings_judge_the_content_tier_too() -> None:
    """Draft settings reach the content tier, not only the shape tier: a face filling 70 % of a
    4:3 photo's height fits a 16:9 Frame's 75 % window unpadded, but not padded by the house's
    20 %. Mutation probe: judge the sample with the house settings -> the draft still reads
    face_cut."""
    face = UnitBox(left=.35, top=.05, right=.65, bottom=.75)
    photos = [SamplePhoto(asset_id=f"asset-{n:04d}", kind="image", width=4000, height=3000,
                          key=key_content(4000, 3000, faces=(face,))) for n in range(20)]
    frames = (output("hall", WIDE),)
    house = analyse(request(), Reader(photos, frames), HOUSE).frames[0]
    unpadded = FitSettings(face_padding=0.0)
    drafted = analyse(request(settings=unpadded), Reader(photos, frames), HOUSE).frames[0]
    assert (house.checked, house.fit_checked) == (20, 0)
    assert {c.reason: c.photos for c in house.refused_by_content} == {"face_cut": 20,
                                                                     "interest_cut": 0}
    assert (drafted.checked, drafted.fit_checked) == (20, 20)
    assert not any(c.photos for c in drafted.refused_by_content)


def test_draft_settings_move_the_video_floor() -> None:
    """A 720p video is under the house floor (1,080 px short side on a Frame of 40 inches or
    more) on the 55-inch hall, and over a drafted 720 px floor. Mutation probe: run the shape
    tier with the house settings -> the draft still refuses it."""
    videos = [photo(n, 1280, 720, kind="video") for n in range(5)]
    frames = (output("hall", WIDE),)
    house = analyse(request(tier="shape"), Reader(videos, frames), HOUSE).frames[0]
    drafted = analyse(request(tier="shape",
                              settings=FitSettings(large_frame_video_short_side=720)),
                      Reader(videos, frames), HOUSE).frames[0]
    assert house.fit_by_shape == 0
    assert {c.reason: c.photos for c in house.refused_by_shape}["below_frame_pixels"] == 5
    assert drafted.fit_by_shape == 5


def test_the_content_tier_judges_a_seeded_sample() -> None:
    """Case 5: checked is at most CONTENT_SAMPLE; fit_checked and the content refusals add up
    to it; the same seed gives the same samples, another seed others. Each verdict is judge's."""
    grid = banded_grid(rows=12, top=.25, bottom=.25, weight=.9)
    photos = [photo(n, 4000, 3000, grid=grid if n % 4 == 0 else None) for n in range(300)]
    reader = Reader(photos, three_frames())
    answer = analyse(request(seed=1), reader, HOUSE)
    hall = answer.frames[0]
    assert hall.checked == CONTENT_SAMPLE
    assert hall.fit_checked + sum(c.photos for c in hall.refused_by_content) == hall.checked
    assert {c.reason for c in hall.refused_by_content} == {"face_cut", "interest_cut"}
    assert next(c for c in hall.refused_by_content if c.reason == "interest_cut").photos > 0
    target = fit_target(three_frames()[0])
    for sample in hall.fitting:
        verdict = judge("image", 4000, 3000, next(p for p in photos
                                                  if p.asset_id == sample.asset_id).key,
                        target, .35, HOUSE)
        assert verdict.crop == sample.crop
    again = analyse(request(seed=1), Reader(photos, three_frames()), HOUSE)
    other = analyse(request(seed=2), Reader(photos, three_frames()), HOUSE)
    assert again.frames[0].fitting == hall.fitting
    assert other.frames[0].fitting != hall.fitting
    assert all("immich" not in sample.thumbnail for sample in hall.fitting)


def test_leads_are_distinct_across_frames() -> None:
    """Case 6: each Frame leads with a photo no earlier Frame took."""
    photos = [photo(n, 4000, 3000) for n in range(20)]
    frames = tuple(output(f"frame-{n}", FOUR_THREE, (1440, 1080)) for n in range(5))
    answer = analyse(request(), Reader(photos, frames), HOUSE)
    leads = [frame.lead.asset_id for frame in answer.frames]
    assert len(set(leads)) == len(frames)


def test_the_shape_tier_reads_no_sample_and_names_the_nearest_crop_limit() -> None:
    """Case 7: tier "shape" makes no sample call and checks nothing; a 9:16 Frame with only 4:3
    photos fits none at 35 % and names the crop limit at which they would (their crop share, in
    range); a Frame they fit names none."""
    photos = [photo(n, 4000, 3000) for n in range(30)]
    frames = (output("stairs", TALL, (1080, 1920)), output("study", FOUR_THREE, (1440, 1080)))
    reader = Reader(photos, frames)
    answer = analyse(request(tier="shape"), reader, HOUSE)
    assert {call[0] for call in reader.calls} == {"selection_sizes", "frame_outputs"}
    stairs, study = answer.frames
    assert all(frame.checked == 0 and frame.lead is None for frame in answer.frames)
    assert stairs.fit_by_shape == 0
    share = crop_share(4000, 3000, 1080 / 1920)
    assert .05 <= share <= .6
    assert stairs.nearest_crop_limit == pytest.approx(share)
    assert study.fit_by_shape == 30 and study.nearest_crop_limit is None
    # Beyond the range there is no crop limit to offer.
    wide_only = [photo(n, 8000, 2000) for n in range(3)]
    assert analyse(request(tier="shape"), Reader(wide_only, frames[:1]),
                   HOUSE).frames[0].nearest_crop_limit is None


def test_the_full_tier_reads_at_most_one_sample_per_frame_and_refusal() -> None:
    photos, frames = mixed(), three_frames()
    reader = Reader(photos, frames)
    answer = analyse(request(), reader, HOUSE)
    samples = [call for call in reader.calls if call[0] == "sample"]
    refusals = sum(1 for frame in answer.frames for count in frame.refused_by_shape if count.photos)
    assert len(samples) <= len(frames) + refusals
    assert all(call[2] <= CONTENT_SAMPLE for call in samples)
    for frame in answer.frames:
        for count in frame.refused_by_shape:
            assert len(count.samples) <= 6 and (count.photos > 0 or not count.samples)
            assert all(sample.crop.offset == .5 for sample in count.samples)


def test_unknown_or_unjudged_frames_are_refused() -> None:
    reader = Reader(mixed(), three_frames())
    with pytest.raises(AnalysisRefused) as unknown:
        analyse(request(frames=("hall", "attic")), reader, HOUSE)
    assert (unknown.value.code, unknown.value.status) == ("frame_unknown", 404)
    draft = FrameDraft(frame_id="study", calibration=Calibration())
    with pytest.raises(AnalysisRefused) as unjudged:
        analyse(request(frames=("hall",), frame_drafts=(draft,)), reader, HOUSE)
    assert unjudged.value.status == 422
    only = analyse(request(frames=("study", "hall"), tier="shape"), reader, HOUSE)
    assert [frame.frame_id for frame in only.frames] == ["hall", "study"]


def test_a_frame_with_no_known_screen_is_listed_unsized_and_not_judged() -> None:
    """Frame box run fb-10: a Frame whose screen size is not known has no Frame output, so no
    shape; it is listed in `unsized` and is nowhere in `frames`, also when a draft names it."""
    unsized = frame_output_of("attic", None)
    reader = Reader(mixed(), (*three_frames(), unsized))
    answer = analyse(request(tier="shape"), reader, HOUSE)
    assert [frame.frame_id for frame in answer.frames] == ["hall", "stairs", "study"]
    assert answer.unsized == ("attic",)
    draft = FrameDraft(frame_id="attic", calibration=Calibration())
    drafted = analyse(request(tier="shape", frames=("attic",), frame_drafts=(draft,)), reader, HOUSE)
    assert drafted.frames == () and drafted.unsized == ("attic",)
    only = analyse(request(tier="shape", frames=("hall",)), reader, HOUSE)
    assert only.unsized == ()


def test_a_draft_box_is_judged_at_its_own_output_and_size() -> None:
    """Frame box run fb-10: a Frame filling a 1,920 x 1,080 screen of a 950 x 540 mm Display,
    drafted as a box a quarter wide and half high, is judged at 480 x 540 (the draft's Frame
    output, never the saved Position's) and 237.5 x 270 mm (`frame_size_mm` of the draft box).
    Mutation probe: judge the draft at the saved Position's output -> 1,920 x 1,080, RED."""
    saved = frame_output_of("lounge", (1920, 1080), inches=43)
    photos = [photo(n, 600, 675) for n in range(4)] + [photo(9, 1920, 1080)]
    reader = Reader(photos, (saved,), {"lounge": (950, 540)})
    box = Calibration(fit=PositionBox(left=.5, top=.25, width=.25, height=.5))
    answer = analyse(request(tier="shape", frame_drafts=(
        FrameDraft(frame_id="lounge", calibration=box),)), reader, HOUSE)
    (lounge,) = answer.frames
    assert lounge.output_px == (480, 540) and lounge.draft is True
    assert math.isclose(lounge.aspect, 480 / 540)
    assert lounge.fit_by_shape == 4      # the 8:9 photos fit the draft; the 16:9 one does not
    undrafted = analyse(request(tier="shape"), Reader(photos, (saved,)), HOUSE).frames[0]
    assert undrafted.output_px == (1920, 1080) and undrafted.fit_by_shape == 1


def test_a_draft_frame_is_large_by_its_drafted_physical_size() -> None:
    """The draft's physical size is the draft box on the Display's own size (950 x 540 mm, 43
    inches across): a 360-row video is under the floor on the whole box (a large Frame from 30
    inches), and the same Frame drafted as a half-size box (21.5 inches) is not a large Frame."""
    saved = frame_output_of("lounge", (1920, 1080), inches=43)
    video = SamplePhoto(asset_id="clip", kind="video", width=640, height=360)
    settings = FitSettings(large_frame_inches=30)
    reader = Reader([video], (saved,), {"lounge": (950, 540)})
    whole = analyse(request(tier="shape", settings=settings), reader, HOUSE).frames[0]
    small = analyse(request(tier="shape", settings=settings, frame_drafts=(FrameDraft(
        frame_id="lounge", calibration=Calibration(fit=PositionBox(width=.5, height=.5))),)),
        reader, HOUSE).frames[0]
    refusals = {count.reason: count.photos for count in whole.refused_by_shape}
    assert refusals["below_frame_pixels"] == 1 and whole.fit_by_shape == 0
    assert small.fit_by_shape == 1
