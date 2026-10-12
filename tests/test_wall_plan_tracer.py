"""The 5a tracer, end to end (roadmap 5a, run ledger .claude/runs/photo-5a.md, INTEGRATE I1).

One Scene on two Frames (16:9 at 3840 x 2160 and 9:16 at 1080 x 1920), 30 s cycles, each Frame
keeping its last photo: a pool of a 16:9, a 4:3, a 2:3 (faces on its left) and a 1:1 photo.
First through the real Planner (`project`) over an hour, the fit rule (`judge`) and the Player's
scalar reference (`player.geometry.picture_source_uv`); then the same wall through
`Coordinator.advance` over PostgreSQL: offered plans carry the crops, an unmeasured photo is
measured by the media worker and then placed, and a "presented" report writes shown history that
the next advance ranks after the others.

Owner, 2026-10-11: "Never show the same photo on two frames at the same time"; "a photo either
fills the frame or it doesn't show"; "crop happens directly on the Player".
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import defaultdict

import pytest
from support.frame_media import seed_variant
from support.key_content_builders import face, measured
from support.measured_photos import measure
from support.photo_pools import photo, seed_pool
from support.position import save_position
from test_media_worker import FakeFaces, _grey_jpeg, worker, worker_storage  # noqa: F401
from test_planner_wall_assignment import FrameSetup, assert_never_on_two_frames, layers
from test_registry import enroll

from central.catalog import SourcePool
from central.coordination import Coordinator
from central.db import ProcessTransactionClock
from central.infra.key_content_store import PgKeyContent
from central.media_queue import QueueReceipt
from central.media_repository import MediaRepository
from central.planner import Projection, project
from central.registry import FrameCreate
from central.runtime import Contribution, Program, Runtime, Scene
from central.wall_plan.fit import Fits, FitTarget, judge
from central.wall_plan.fit_settings import FitSettings
from central.wall_plan.order import WallHistory
from central.wall_plan.selection import SceneSelection
from contracts.models import (
    Calibration,
    CornersFit,
    CoverFit,
    FrameProfile,
    Observation,
    OutputBinding,
    Variant,
    frame_output,
)
from player.geometry import picture_source_uv

WIDE = FrameProfile(width_px=3840, height_px=2160, diagonal_inches=55)
TALL = FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)
PROFILES = {"wide": WIDE, "tall": TALL}
FRAMES = {"wide": FrameSetup("wide", after_end="keep_this_photo"),
          "tall": FrameSetup("tall", after_end="keep_this_photo")}
POOL = "library:1"
# Each photo's original size: every one clears its Frame's pixels wherever its shape fits.
SIZES = {"sixteen_nine": (3840, 2160), "four_three": (5120, 3840),
         "two_three": (2400, 3600), "square": (4000, 4000)}
LEFT_FACES = (face(0.0, 0.3, 0.15, 0.5),)
UNCERTAINTY = 0.1


def pool():
    return {name: measured(name, width, height,
                           faces=LEFT_FACES if name == "two_three" else (),
                           captured_at=index, variant=Variant(
                               sha256=hashlib.sha256(name.encode()).hexdigest(), size=10,
                               media_type="image/jpeg", width=width, height=height))
            for index, (name, (width, height)) in enumerate(SIZES.items())}


def wall(crop_limit: float | None = None) -> Runtime:
    selection = SceneSelection() if crop_limit is None else SceneSelection(crop_limit=crop_limit)
    instance = Runtime()
    instance.set_scene(Scene(
        scene_id="tracer", cycle_seconds=30, loop=True, selection=selection,
        contributions=tuple(Contribution(target=f"frame:{frame}", source_refs=(POOL,),
                                         after_end="keep_this_photo") for frame in FRAMES)))
    instance.activate("tracer", "run", 0)
    return instance


def full_screen(frame: str) -> FitTarget:
    """A Frame full screen on a Display of its profile's size: its Frame output is the profile's."""
    profile = PROFILES[frame]
    return FitTarget(profile=profile, output_px=(profile.width_px, profile.height_px))


def an_hour(instance: Runtime, photos, *, frames=FRAMES, outputs=None) -> Projection:
    """`outputs`: each Frame's Frame output (default: full screen, `full_screen`)."""
    return project(
        instance, 0,
        bindings_by_player={"player": tuple(
            OutputBinding(output_id=f"out-{frame}", frame_id=frame, generation=1,
                          profile=PROFILES[frame]) for frame in frames)},
        source_pools={POOL: SourcePool(source_ref=POOL, ready=True,
                                       candidates=tuple(photos.values()))},
        authored_candidates={}, locked_assignments={}, history=WallHistory(),
        horizon_seconds=300, plan_seconds=3600,
        max_clock_uncertainty=UNCERTAINTY, settings=FitSettings(),
        frame_outputs=outputs if outputs is not None else {
            frame: full_screen(frame).output_px for frame in frames})


def placed_by_frame(projection: Projection) -> dict[str, list]:
    planned = defaultdict(list)
    for entry in projection.plan:
        if entry.asset_id is not None:
            planned[entry.frame_id].append(entry)
    return planned


def test_an_hour_of_the_wall_fills_each_frame_and_crops_on_the_player():
    """The page's wall at the Scene's default crop limit (35 %): every placed photo fits its
    Frame, the 1:1 photo never shows (it would lose 44 % on either Frame), no photo is on both
    Frames at once, and the 2:3 photo on the 9:16 Frame keeps its faces (offset 0), which the
    Player's reference then samples from the photo's left part only.
    Mutation probe: centre every crop -> the offset assertions fail."""
    photos = pool()
    projection = an_hour(wall(), photos)
    planned = placed_by_frame(projection)
    assert {frame: len(entries) for frame, entries in planned.items()} == {"wide": 120, "tall": 120}
    for frame, entries in planned.items():
        for entry in entries:
            candidate = photos[entry.asset_id]
            verdict = judge(candidate.kind, candidate.original_width, candidate.original_height,
                            candidate.analysis.key, full_screen(frame), 0.35, FitSettings())
            assert isinstance(verdict, Fits), (frame, entry.asset_id, verdict)
            assert entry.crop == verdict.crop
    assert "square" not in {entry.asset_id for entries in planned.values() for entry in entries}
    assert {entry.asset_id for entry in planned["wide"]} == {"sixteen_nine", "four_three"}
    assert {entry.asset_id for entry in planned["tall"]} == {"two_three"}
    assert_never_on_two_frames(FRAMES, projection)

    offered = [layer for layer in layers(projection) if layer.frame_id == "tall"]
    assert offered and {layer.crop.offset for layer in offered} == {0.0}
    assert {entry.crop.offset for entry in planned["tall"]} == {0.0}
    width, height = SIZES["two_three"]
    samples = [picture_source_uv(x / 20, y / 20, CornersFit(), (1080, 1920),
                                 TALL.width_px / TALL.height_px, width, height, offered[0].crop)
               for x in range(21) for y in range(21)]
    kept_width = (9 / 16) / (2 / 3)            # the 9:16 window's share of a 2:3 photo's width
    assert min(u for u, _ in samples) == pytest.approx(0)
    assert max(u for u, _ in samples) == pytest.approx(kept_width)
    assert all(u <= kept_width + 1e-9 for u, _ in samples)    # the right side, faceless, is never drawn


def test_a_mat_opening_frame_plans_a_photo_smaller_than_its_display():
    """Follow-ups B1 case 9 (owner, 2026-10-11: compatibility "measuring against the Frame
    output and not the Display"): one 16:9 Frame drawn through a 40 % x 40 % Cover opening of a
    3,840 x 2,160 Display needs 1,536 x 864 pixels, so a 1,600 x 900 photo fills it all hour;
    judged against the Display's size the same wall has nothing to show.
    Mutation probe: judge rule 2 against the profile's pixels -> nothing is placed."""
    mat = Calibration(fit=CoverFit(corners=((.3, .3), (.7, .3), (.7, .7), (.3, .7))))
    small = {"small": measured("small", 1600, 900, variant=Variant(
        sha256=hashlib.sha256(b"small").hexdigest(), size=10, media_type="image/jpeg",
        width=1600, height=900))}
    instance = Runtime()
    instance.set_scene(Scene(
        scene_id="tracer", cycle_seconds=30, loop=True, selection=SceneSelection(),
        contributions=(Contribution(target="frame:wide", source_refs=(POOL,),
                                    after_end="keep_this_photo"),)))
    instance.activate("tracer", "run", 0)
    opening = frame_output(WIDE, mat, (3840, 2160))
    assert opening == (1536, 864)
    planned = placed_by_frame(an_hour(instance, small, frames=("wide",),
                                      outputs={"wide": opening}))
    assert len(planned["wide"]) == 120
    assert {entry.asset_id for entry in planned["wide"]} == {"small"}
    on_display = an_hour(instance, small, frames=("wide",), outputs={"wide": (3840, 2160)})
    assert placed_by_frame(on_display) == {}


def test_photos_that_fit_both_frames_are_never_on_both_at_once():
    """The same wall at a 60 % crop limit: the 1:1 and the 4:3 photos now fit both Frames, so
    the two Frames contend for them over the hour, and no photo's on-screen extents (kept stills
    and the clock guard included) overlap on the two. Mutation probe: drop the extent check ->
    the overlap assertion fails."""
    projection = an_hour(wall(crop_limit=0.6), pool())
    planned = placed_by_frame(projection)
    shared = ({entry.asset_id for entry in planned["wide"]}
              & {entry.asset_id for entry in planned["tall"]})
    assert shared, "no photo went on both Frames in the hour: the wall has no contention"
    assert_never_on_two_frames(FRAMES, projection)


# Through Coordinator.advance over PostgreSQL --------------------------------------------------

class MeasureQueue:
    """The media task queue's measurement side: records each queued photo once."""

    def __init__(self) -> None:
        self.waiting: list[str] = []

    def enqueue_measure_in(self, conn, asset_id):
        coalesced = asset_id in self.waiting
        if not coalesced:
            self.waiting.append(asset_id)
        return QueueReceipt(coalesced=coalesced)


def wall_players(registry):
    players = []
    for frame_id, profile, size in (("frame-wide", WIDE, (1210, 680)),
                                    ("frame-tall", TALL, (300, 530))):
        player, _, _ = enroll(registry)
        registry.create_frame(FrameCreate(id=frame_id, width_mm=size[0], height_mm=size[1],
                                          profile=profile))
        registry.bind(frame_id, player["player_id"], "HDMI-A-1", expected_generation=0)
        save_position(registry, frame_id)
        players.append(player)
    return players


def prepare(registry, photos):
    """Each photo's media variant made under the wall's recipe, as a worker leaves it (the
    wall's Frames exist first). Returns asset id by prepared digest."""
    digests = {}
    for number, item in enumerate(photos):
        digest = f"{number + 1:064x}"
        digests[digest] = item.asset_id
        seed_variant(registry, item.asset_id, digest, width=item.width, height=item.height)
    return digests


def observe_presented(coordinator, player, plan, layer):
    coordinator.observe(player["player_id"], Observation(
        plan_id=plan.plan_id, revision=plan.revision, assignment_id=layer.assignment_id,
        authority_epoch=plan.authority_epoch, observed_at=coordinator.clock.utc(),
        status="presented", detail="none"))


def test_the_wall_through_the_real_advance_measures_crops_and_remembers_what_was_shown(
        registry, worker_storage):  # noqa: F811 (the fixture imported above)
    """Seams (a), (c), (d), (e): the first advance asks for the unmeasured 4:3 photo's key
    content; the scheduler's line queues it; the media worker measures it (a fake library
    client); the next advance places it. Offered plans carry crops (the 2:3 photo keeps its
    left on the 9:16 Frame), and a "presented" report becomes shown history the next advance
    ranks after the others."""
    wide_player, tall_player = wall_players(registry)
    library = {name: photo(name, taken_at=900 + index, width=width, height=height)
               for index, (name, (width, height)) in enumerate(SIZES.items())}
    seed_pool(registry, POOL, tuple(library.values()))
    for name, item in library.items():
        if name == "two_three":
            measure(registry, item, faces=LEFT_FACES)
        elif name != "four_three":
            measure(registry, item)
    asset_of = prepare(registry, library.values())
    four_three = library["four_three"].asset_id

    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.runtime.command("set_scene", Scene(
        scene_id="tracer", cycle_seconds=30, loop=True,
        contributions=tuple(Contribution(target=f"frame:{frame}", source_refs=(POOL,),
                                         after_end="keep_this_photo")
                            for frame in ("frame-wide", "frame-tall"))))
    coordinator.runtime.command("set_program", Program(
        program_id="calendar", scene_id="tracer", starts_at=1000, ends_at=1000 + 7200))
    first = coordinator.advance()
    assert [request.asset_id for request in first.measurements] == [four_three]
    assert four_three not in {entry.asset_id for entry in first.plan}

    # The scheduler's line, then the media worker's measure task.
    queue = MeasureQueue()
    requests = MediaRepository(registry.db, registry.clock, queue=queue,
                               times=ProcessTransactionClock(registry.clock))
    assert requests.request_measurements(first.measurements) == 1
    assert queue.waiting == [four_three]
    measurer = worker(worker_storage, FakeFaces(_grey_jpeg()),
                      key_content=PgKeyContent(registry.db,
                                               times=ProcessTransactionClock(registry.clock)))
    for asset_id in queue.waiting:
        asyncio.run(measurer.measure(asset_id))

    registry.clock.advance(1)
    second = coordinator.advance()
    assert second.measurements == ()
    assert four_three in {entry.asset_id for entry in second.plan
                          if entry.frame_id == "frame-wide"}

    plans = {name: coordinator.delivery(player["player_id"], 1)["plan"]
             for name, player in (("wide", wide_player), ("tall", tall_player))}
    media = {name: [layer for layer in plan.layers if layer.presentation == "media"]
             for name, plan in plans.items()}
    assert media["wide"] and media["tall"]
    assert all(layer.crop is not None for layers_ in media.values() for layer in layers_)
    assert {asset_of[layer.variant.sha256] for layer in media["tall"]} == {
        library["two_three"].asset_id}
    assert {layer.crop.offset for layer in media["tall"]} == {0.0}

    # Shown history: the wide Frame's first photo is reported presented; the next advance ranks
    # it after the other photo, so the wide Frame's next new slot takes the other one.
    shown = min(media["wide"], key=lambda layer: layer.start)
    observe_presented(coordinator, wide_player, plans["wide"], shown)
    with registry.db.transaction() as conn:
        rows = conn.execute("SELECT asset_id FROM shown_photos").fetchall()
    assert [row["asset_id"] for row in rows] == [asset_of[shown.variant.sha256]]
    locked = {layer.assignment_id for layers_ in media.values() for layer in layers_}
    registry.clock.advance(1)
    third = coordinator.advance()
    fresh = sorted((entry for entry in third.plan
                    if entry.frame_id == "frame-wide" and entry.asset_id is not None
                    and entry.assignment_id not in locked), key=lambda entry: entry.start)
    assert fresh
    others = {library["sixteen_nine"].asset_id, four_three} - {asset_of[shown.variant.sha256]}
    assert fresh[0].asset_id in others
