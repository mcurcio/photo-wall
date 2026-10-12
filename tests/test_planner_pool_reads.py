"""The Planner reads only what the plan needs (follow-up to 5a, slice S3; design rule 3): each
Scene's line is read top first, `slots x hydrate_factor` members at a time, fresh every pass,
never a whole pool. The plans are the plans a whole-pool read makes; a batch the fit rule refuses
entirely is read again twice as long; and a capture time another pool's check edits reaches the
next pass, because nothing is snapshotted.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from support.house_time import UTC_HOUSE
from support.key_content_builders import face, measured
from support.photo_pools import photo as library_photo
from support.photo_pools import seed_pool

from central.catalog import Candidate, SourcePool
from central.db import ProcessTransactionClock
from central.house_time.model import HouseTime
from central.media_repository import MediaRepository
from central.planner import PlannerLimits, Projection, project
from central.runtime import Contribution, Runtime, Scene
from central.wall_plan import assignment
from central.wall_plan.fit_settings import FitSettings
from central.wall_plan.order import (
    LineAnswer,
    LineQuery,
    PhotoShowing,
    PoolLines,
    SceneLines,
    SourceLine,
    WallHistory,
)
from central.wall_plan.selection import SceneSelection
from contracts.models import FrameProfile, Mat, OutputBinding, Variant
from contracts.on_screen import Extent

PROFILES = {
    "wide": FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24),
    "tall": FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24),
}
SIZES = {"wide": (3840, 2160), "tall": (2160, 3840), "four_three": (4000, 3000),
         "square": (3000, 3000)}
POOL = "pool:1"


class CountingLines:
    """A `SceneLines` that counts the members each pass reads, and the time its reads take."""

    def __init__(self, inner: SceneLines) -> None:
        self.inner, self.members, self.queries, self.seconds = inner, 0, [], 0.0

    def sources(self, source_refs: set[str]) -> dict[str, SourceLine]:
        return self.inner.sources(source_refs)

    def line(self, query: LineQuery) -> LineAnswer:
        started = time.perf_counter()
        answer = self.inner.line(query)
        self.seconds += time.perf_counter() - started
        self.members += len(answer.members)
        self.queries.append(query)
        return answer


def photo(asset: str, shape: str, *, captured: float, prepared: bool = True,
          is_measured: bool = True, faces=()) -> Candidate:
    width, height = SIZES[shape]
    variant = Variant(asset_id=asset, recipe_id="e" * 64,
                      sha256=hashlib.sha256(asset.encode()).hexdigest(), size=10,
                      media_type="image/jpeg", width=width, height=height) if prepared else None
    if is_measured:
        return measured(asset, width, height, faces=faces, captured_at=captured, variant=variant)
    return Candidate(asset_id=asset, kind="image", original_width=width, original_height=height,
                     captured_at=captured, variant=variant, readable_size=(width, height))


def wall(frames: dict[str, str], *, order: str, no_repeat_days: int = 0,
         cycle: float = 30, house: HouseTime = UTC_HOUSE) -> Runtime:
    instance = Runtime(house=house)
    instance.set_scene(Scene(
        scene_id="scene", cycle_seconds=cycle, loop=True,
        selection=SceneSelection(order=order, no_repeat_days=no_repeat_days),
        contributions=tuple(Contribution(target=f"frame:{frame}", source_refs=(POOL,))
                            for frame in frames)))
    instance.activate("scene", "run", 0)
    return instance


def plan(instance: Runtime, frames: dict[str, str], lines: SceneLines, *, now: float = 0,
         history: WallHistory = WallHistory(), limits: PlannerLimits = PlannerLimits(),
         horizon: float = 60, plan_seconds: float = 300) -> Projection:
    return project(
        instance, now,
        bindings_by_player={"player": tuple(OutputBinding(
            output_id=f"out-{frame}", frame_id=frame, generation=1, profile=PROFILES[shape], mat=Mat())
            for frame, shape in frames.items())},
        lines=lines, authored_candidates={}, locked_assignments={}, history=history,
        horizon_seconds=horizon, plan_seconds=plan_seconds, max_clock_uncertainty=0.1,
        limits=limits, settings=FitSettings(),
        frame_outputs={frame: (PROFILES[shape].width_px, PROFILES[shape].height_px)
                       for frame, shape in frames.items()})


def mixed_pool(count: int) -> SourcePool:
    shapes = ("wide", "tall", "four_three", "square")
    photos = tuple(photo(f"p{n:04d}", shapes[n % 4], captured=float(n * 7 % count),
                         prepared=n % 5 != 0, is_measured=n % 11 != 0) for n in range(count))
    return SourcePool(source_ref=POOL, ready=True, candidates=photos)


def shown(asset: str, *, pass_number: int, start: float) -> PhotoShowing:
    return PhotoShowing(assignment_id=f"a-{asset}-{start}", asset_id=asset, frame_id="left",
                        scene_id="scene", pass_number=pass_number, start=start, end=start + 30,
                        fade_in=0, after_end="leave_as_is")


HISTORIES = {
    "none": WallHistory(),
    "shown": WallHistory(shown=tuple(
        shown(f"p{n:04d}", pass_number=2, start=-10_000 + n) for n in range(0, 120, 3))),
}


def test_a_partial_read_finds_on_this_day_on_the_house_calendar():
    """Roadmap 2b: the line's top is read on the house calendar, as the whole line is ranked. At
    23:40 on 12 Oct in New York (13 Oct in UTC) the 300 photos taken on a 13 Oct are not On this
    day; a top read on UTC's date would hold only them, ordered by name, and plan otherwise.

    Mutation probe: the Planner's `LineQuery` without `house` -> the plans differ."""
    house = HouseTime(zone="America/New_York")
    zone = ZoneInfo(house.zone)
    now = datetime(2026, 10, 12, 23, 40, tzinfo=zone).timestamp()
    frames = {"left": "wide"}
    pool = SourcePool(source_ref=POOL, ready=True, candidates=(
        photo("late-12th", "wide", captured=datetime(2025, 10, 12, 23, 30, tzinfo=zone).timestamp()),
        *(photo(f"f{n:03d}", "wide", captured=datetime(2020, 10, 13, 12, tzinfo=zone).timestamp())
          for n in range(300))))
    lean = plan(wall(frames, order="on_this_day", house=house), frames,
                PoolLines({POOL: pool}), now=now)
    full = plan(wall(frames, order="on_this_day", house=house), frames,
                PoolLines({POOL: pool}), now=now, limits=PlannerLimits(hydrate_factor=64))
    assert [entry.asset_id for entry in lean.plan][:1] == ["late-12th"]
    assert lean == full


@pytest.mark.parametrize("order", ["shuffle", "chronological", "newest_first", "on_this_day"])
@pytest.mark.parametrize("history", sorted(HISTORIES))
def test_the_plans_equal_the_whole_pool_plans(order, history):
    """A pass that reads the top of the line plans what a pass reading the whole pool plans, for
    every order, with and without a shown history (tiers, a pass, chronological's wrap)."""
    frames = {"left": "wide", "right": "tall", "far": "wide"}
    pool = mixed_pool(600)
    top = CountingLines(PoolLines({POOL: pool}))
    whole = CountingLines(PoolLines({POOL: pool}))
    instance = wall(frames, order=order, no_repeat_days=1)
    lean = plan(instance, frames, top, history=HISTORIES[history], now=1_000)
    full = plan(wall(frames, order=order, no_repeat_days=1), frames, whole,
                history=HISTORIES[history], now=1_000,
                limits=PlannerLimits(hydrate_factor=64))
    assert top.members < whole.members < whole.queries[0].limit   # the whole line, once
    assert lean == full
    assert any(entry.asset_id for entry in lean.plan)


def test_a_batch_the_fit_rule_refuses_entirely_is_read_again_twice_as_long():
    """The newest 40 photos would each cut a face on the wide Frame; the five older ones fit. The
    first batch (2 slots x 4) is refused, so it doubles until it reaches them."""
    cut = (face(0.0, 0.0, 0.3, 0.2), face(0.7, 0.8, 1.0, 1.0))
    photos = [photo(f"cut{n:02d}", "four_three", captured=1000 + n, faces=cut) for n in range(40)]
    photos += [photo(f"fit{n}", "wide", captured=n) for n in range(5)]
    lines = CountingLines(PoolLines({POOL: SourcePool(source_ref=POOL, ready=True,
                                                      candidates=tuple(photos))}))
    frames = {"left": "wide"}
    result = plan(wall(frames, order="newest_first"), frames, lines, horizon=60, plan_seconds=60)
    assert [query.limit for query in lines.queries] == [8, 16, 32, 64]
    placed = [entry.asset_id for entry in result.plan]
    assert placed == ["fit4", "fit3"]


def test_a_batch_with_fewer_useful_photos_than_slots_is_read_deeper():
    """Sparse fits: the newest photo fits, the next 40 would each cut a face on the wide Frame,
    and five older ones fit. A first batch holding one useful photo for four slots is read
    deeper (until it holds a useful photo per slot, or the line ends), so the plan rotates
    through the fitting photos as a whole-pool read does, never repeating the one it found."""
    cut = (face(0.0, 0.0, 0.3, 0.2), face(0.7, 0.8, 1.0, 1.0))
    photos = [photo("fitA", "wide", captured=2000)]
    photos += [photo(f"cut{n:02d}", "four_three", captured=1000 + n, faces=cut) for n in range(40)]
    photos += [photo(f"fit{n}", "wide", captured=n) for n in range(5)]
    pool = SourcePool(source_ref=POOL, ready=True, candidates=tuple(photos))
    frames = {"left": "wide"}
    lean = plan(wall(frames, order="newest_first"), frames, PoolLines({POOL: pool}),
                horizon=60, plan_seconds=120)
    full = plan(wall(frames, order="newest_first"), frames, PoolLines({POOL: pool}),
                horizon=60, plan_seconds=120, limits=PlannerLimits(hydrate_factor=64))
    assert [entry.asset_id for entry in lean.plan] == ["fitA", "fit4", "fit3", "fit2"]
    assert lean == full


def test_a_pass_reads_at_most_its_slots_times_the_factor_per_line():
    frames = {f"frame{n}": ("wide", "tall")[n % 2] for n in range(8)}
    photos = tuple(photo(f"p{n:05d}", ("wide", "tall")[n % 2], captured=n)
                   for n in range(50_000))
    lines = CountingLines(PoolLines({POOL: SourcePool(source_ref=POOL, ready=True,
                                                      candidates=photos)}))
    result = plan(wall(frames, order="shuffle", cycle=60), frames, lines, horizon=300,
                  plan_seconds=3600)
    slots = len(result.plan)
    assert slots == 8 * 60
    assert lines.members <= slots * PlannerLimits().hydrate_factor * 1
    assert len({entry.asset_id for entry in result.plan}) == slots


def test_a_capture_time_edited_by_another_pools_check_changes_the_next_pass(registry):
    """Newest first: the family pool's newest photo leads its line. The garden pool's check finds
    the older photo with a later capture time (Immich's date was edited), and the next pass, which
    reads the family line fresh, leads with it."""
    older = library_photo("older", taken_at=1.0, width=3840, height=2160)
    newer = library_photo("newer", taken_at=2.0, width=3840, height=2160)
    seed_pool(registry, "family:1", (older, newer))
    repository = MediaRepository(registry.db, registry.clock,
                                 times=ProcessTransactionClock(registry.clock))
    frames = {"left": "wide"}

    def first_photo() -> str:
        instance = Runtime(house=UTC_HOUSE)
        instance.set_scene(Scene(
            scene_id="scene", cycle_seconds=30, loop=True,
            selection=SceneSelection(order="newest_first"),
            contributions=(Contribution(target="frame:left", source_refs=("family:1",)),)))
        instance.activate("scene", "run", 0)
        with registry.db.transaction() as conn:
            result = plan(instance, frames, repository.scene_lines_in(conn), horizon=30,
                          plan_seconds=120)
        return next(entry.asset_id for entry in result.plan if entry.asset_id)

    assert first_photo() == newer.asset_id
    seed_pool(registry, "garden:1", (older.model_copy(update={"taken_at": 3.0}),))
    assert first_photo() == older.asset_id


def test_rule_1_reads_a_bounded_number_of_extents_per_photo_placed(monkeypatch):
    """A perpetual Run (the Default Scene) on an 8 s cycle, three Frames and four photos: an
    hour's plan places each photo about 340 times. Rule 1 (never one photo on two Frames at once)
    reads only the showings that can overlap (`_Spans`), so the extents a pass computes and
    compares grow with the photos it places, never with the photos placed before them. The pass
    holds the coordination lock while it plans, so a slow one stalls every Player's readiness
    (the wall demo's fallback gaps). Counted, not timed: the count is the same on every machine.

    Mutation probe: rule 1 reading every earlier showing of the photo (`_Wall.add` putting every
    showing in `of_asset`, the previous `free`) -> about 680 extent reads a placement in the hour,
    four times the half hour's, where this reads 5 in both."""
    reads = 0
    computed, compared = assignment.on_screen_extent, Extent.overlaps

    def count_computed(*args, **kwargs):
        nonlocal reads
        reads += 1
        return computed(*args, **kwargs)

    def count_compared(self, other):
        nonlocal reads
        reads += 1
        return compared(self, other)

    monkeypatch.setattr(assignment, "on_screen_extent", count_computed)
    monkeypatch.setattr(Extent, "overlaps", count_compared)
    frames = {"left": "wide", "right": "wide", "far": "wide"}
    pool = SourcePool(source_ref=POOL, ready=True, candidates=tuple(
        photo(f"p{n}", "wide", captured=float(n)) for n in range(4)))

    def reads_a_placement(plan_seconds: float) -> float:
        nonlocal reads
        reads = 0
        projection = plan(wall(frames, order="shuffle", cycle=8), frames,
                          PoolLines({POOL: pool}), plan_seconds=plan_seconds)
        placed = sum(entry.asset_id is not None for entry in projection.plan)
        assert placed > 3 * plan_seconds / 8 * 0.9
        return reads / placed

    half_hour, hour = reads_a_placement(1800), reads_a_placement(3600)
    assert hour <= 1.1 * half_hour
    assert hour < 20
