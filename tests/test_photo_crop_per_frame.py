"""Each Frame gets its own crop of a photo (issue #131), through the real Planner (`project`), the
fit rule it asks (`judge`) and the Player's drawing of the crop (`player.geometry.fit_source_uv` at
the aspect of the Frame's placement, `player.planes.finish_size`).

Owner, 2026-10-21 (#131): "the same photo planned onto two Frames of different viewable shape gets
two different crops, each matching its Frame." Owner, 2026-10-22: "Do not cut out faces"; smart
crop is "mostly just to prevent faces from being cut off — not to make editorial decisions about
photo composition"; "Photos and frames come in all shapes and sizes".

Every Frame here is a Position box on a 3,840 x 2,160 screen, so the screens are all the same size
and only the boxes differ: a Frame's shape is its box's. A screen's Output is bound to one Frame
(bindings UNIQUE(player_id, output_id)), so each Frame has a screen of its own.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict

import pytest
from support.house_time import UTC_HOUSE
from support.key_content_builders import banded_grid, face, measured
from support.position import LIGHTING

from central.catalog import Candidate, SourcePool
from central.installation_models import FrameOutput
from central.planner import Projection, project
from central.runtime import Contribution, Runtime, Scene
from central.wall_plan.fit import DoesNotFit, Fits, FitTarget, faces_judged, fit_target, judge
from central.wall_plan.fit_settings import FitSettings
from central.wall_plan.order import PoolLines, WallHistory
from central.wall_plan.selection import SceneSelection
from contracts.frame_fit import place_box
from contracts.models import Calibration, OutputBinding, PositionBox, Variant, frame_output
from contracts.photo_crop import crop_window
from player.geometry import fit_source_uv
from player.planes import finish_size

SCREEN = (3840, 2160)
POOL = "library:1"
CROP_LIMIT = 0.6          # the widest a Scene may set: room for every box below
SETTINGS = FitSettings()
# Position boxes on SCREEN, each named by its Frame output's width : height.
BOXES = {
    "two_to_one": PositionBox(left=0, top=0, width=0.5, height=4 / 9),              # 1920 x 960
    "sixteen_to_nine": PositionBox(left=0, top=0, width=0.5, height=0.5),           # 1920 x 1080
    "one_to_one": PositionBox(left=0, top=0, width=0.28125, height=0.5),            # 1080 x 1080
    "two_to_three": PositionBox(left=0, top=0, width=0.1875, height=0.5),           # 720 x 1080
}
# The photo: 3 : 2. Its interest is weighted to its top and left, and its faces sit right of and
# below centre, so each Frame's crop slides towards the interest only as far as its own shape lets
# it keep the faces: the offset depends on the Frame's shape.
PHOTO_SIZE = (4500, 3000)
OFF_CENTRE_FACES = (face(0.70, 0.55, 0.82, 0.80), face(0.60, 0.50, 0.68, 0.70))
# Faces at its two side edges: no window narrower than the photo holds both whole.
EDGE_FACES = (face(0.02, 0.30, 0.12, 0.50), face(0.88, 0.30, 0.98, 0.50))


def frame_of(frame_id: str, box: PositionBox) -> FrameOutput:
    """The Frame as `central.infra.display_store.frame_outputs_in` reads it: its Frame output is
    `frame_output` of its Position on its screen, hung upright."""
    return FrameOutput(frame_id=frame_id, turn=0, video=True, display_px=SCREEN,
                       output_px=frame_output(Calibration(fit=box), SCREEN, 0), size_mm=None)


def candidate(asset_id: str, faces, *, captured: float, grid=None) -> Candidate:
    width, height = PHOTO_SIZE
    digest = hashlib.sha256(asset_id.encode()).hexdigest()
    return measured(asset_id, width, height, faces=faces, grid=grid, captured_at=captured,
                    variant=Variant(asset_id=asset_id, recipe_id="e" * 64, sha256=digest, size=10,
                                    media_type="image/jpeg", width=width, height=height))


def pool(subject: Candidate) -> tuple[Candidate, ...]:
    """`subject` among faceless photos of its shape that fit every box, so the Frames take turns
    with it over the hour instead of contending for one photo."""
    return (subject, *(candidate(f"filler-{n}", (), captured=n) for n in range(3)))


def an_hour(frames: dict[str, PositionBox], photos: tuple[Candidate, ...]) -> Projection:
    runtime = Runtime(house=UTC_HOUSE)
    runtime.set_scene(Scene(
        scene_id="crops", cycle_seconds=30, loop=True,
        selection=SceneSelection(crop_limit=CROP_LIMIT),
        contributions=tuple(Contribution(target=f"frame:{frame}", source_refs=(POOL,),
                                         after_end="keep_nothing") for frame in frames)))
    runtime.activate("crops", "run", 0)
    return project(
        runtime, 0,
        bindings_by_player={"player": tuple(
            OutputBinding(output_id=f"out-{frame}", frame_id=frame, generation=1,
                          calibration=Calibration(fit=box), lighting=LIGHTING, display_mm=None)
            for frame, box in frames.items())},
        lines=PoolLines({POOL: SourcePool(source_ref=POOL, ready=True, candidates=photos)}),
        authored_candidates={}, locked_assignments={}, history=WallHistory(),
        horizon_seconds=300, plan_seconds=3600, max_clock_uncertainty=0.1, settings=SETTINGS,
        frame_outputs={frame: frame_of(frame, box) for frame, box in frames.items()})


def crops_of(projection: Projection, asset_id: str) -> dict[str, set]:
    crops = defaultdict(set)
    for entry in projection.plan:
        if entry.asset_id == asset_id:
            crops[entry.frame_id].add(entry.crop)
    return crops


def inside(box, window) -> bool:
    left, top, right, bottom = window
    slack = 1e-9
    return (box.left >= left - slack and box.right <= right + slack
            and box.top >= top - slack and box.bottom <= bottom + slack)


@pytest.mark.parametrize("first, second", [
    ("two_to_one", "sixteen_to_nine"),     # both wider than the photo: cut at its top and bottom
    ("one_to_one", "two_to_three"),        # both narrower: cut at its left and right
    ("sixteen_to_nine", "one_to_one"),     # the photo's ratio between theirs: one cut each way
])
def test_one_photo_on_two_frames_gets_a_crop_of_each_frames_shape(first: str, second: str) -> None:
    """The photo is planned on both Frames within the hour; each Frame's crop window has that
    Frame's shape, the two windows differ, every face is whole in each, and the Player draws
    exactly that window over the Frame's placement.
    Mutation probes: judge with the screen's size instead of the Frame output -> each Frame
    draws an offset chosen for another shape, which cuts a face; let a window cut a face -> the
    rule slides to the interest and a face falls outside the window."""
    frames = {first: BOXES[first], second: BOXES[second]}
    subject = candidate("subject", OFF_CENTRE_FACES, captured=100,
                        grid=banded_grid(columns=16, rows=11, left=0.3, top=0.3))
    projection = an_hour(frames, pool(subject))
    crops = crops_of(projection, "subject")
    assert set(crops) == {first, second}, crops
    faces = faces_judged(subject.analysis.key, SETTINGS)
    assert len(faces) == len(OFF_CENTRE_FACES)
    width, height = PHOTO_SIZE
    windows = {}
    for frame, box in frames.items():
        assert len(crops[frame]) == 1, crops[frame]
        (crop,) = crops[frame]
        output = frame_output(Calibration(fit=box), SCREEN, 0)
        aspect = output[0] / output[1]
        window = crop_window(crop, width, height, aspect)
        left, top, right, bottom = window
        assert (right - left) * width / ((bottom - top) * height) == pytest.approx(aspect)
        assert all(inside(one, window) for one in faces), (frame, window)
        # The rule asked of the box's own pixels, not through the Planner's Frame reading.
        verdict = judge("image", width, height, subject.analysis.key,
                        FitTarget(output_px=output, video=True, size_mm=None), CROP_LIMIT, SETTINGS)
        assert isinstance(verdict, Fits) and verdict.crop == crop
        # The Player: the Frame's placement on its screen, its aspect, and the window drawn.
        placement = place_box(box.box(), SCREEN)
        assert (placement.width, placement.height) == output
        assert fit_source_uv(0, 0, placement.aspect, PHOTO_SIZE, crop) == pytest.approx((left, top))
        assert fit_source_uv(1, 1, placement.aspect, PHOTO_SIZE, crop) == pytest.approx((right, bottom))
        layer = next(layer for layer in projection.for_player("player").layers
                     if layer.frame_id == frame and layer.variant is not None
                     and layer.variant.asset_id == "subject")
        assert layer.crop == crop
        finished = finish_size(layer, placement)
        assert finished[0] / finished[1] == pytest.approx(aspect, rel=2e-3)
        windows[frame] = window
    assert windows[first] != windows[second]
    assert len({crop for frame_crops in crops.values() for crop in frame_crops}) == 2


def test_a_photo_whose_faces_no_window_of_a_frames_shape_holds_skips_that_frame() -> None:
    """Faces at both side edges of a 3 : 2 photo: a 2 : 3 Frame's window keeps four ninths of the
    photo's width, so no window of that Frame's shape holds both faces whole. The fit rule refuses
    the photo there ("face_cut") and the Planner never places it there all hour, while the 2 : 1
    Frame, cut at top and bottom, takes it with both faces whole."""
    frames = {"two_to_one": BOXES["two_to_one"], "two_to_three": BOXES["two_to_three"]}
    subject = candidate("subject", EDGE_FACES, captured=100)
    width, height = PHOTO_SIZE
    narrow = fit_target(frame_of("two_to_three", BOXES["two_to_three"]))
    assert judge("image", width, height, subject.analysis.key, narrow, CROP_LIMIT, SETTINGS) == \
        DoesNotFit(reason="face_cut")
    crops = crops_of(an_hour(frames, pool(subject)), "subject")
    assert set(crops) == {"two_to_one"}, crops
    (crop,) = crops["two_to_one"]
    window = crop_window(crop, width, height, 1920 / 960)
    assert all(inside(one, window) for one in faces_judged(subject.analysis.key, SETTINGS))
