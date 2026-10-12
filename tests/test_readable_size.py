"""A photo is judged at the largest copy of it Photo Wall can read (follow-up to 5a, slice S1;
central/photo_library/rendition.py). Owner, 2026-10-09: "the image resolution should always be at
least as large as the visible frame dimensions"; Q1: "both sides of the content need to at least
span the visible frame". An unedited RAW photo with Immich's full-size copies off can only be
read at its preview's size, so that is the size the fit rule and the Planner judge; until the
preview is measured nothing guesses it, the photo is not placed and a measurement is asked for.
"""

from __future__ import annotations

import pytest
from support.house_time import UTC_HOUSE
from support.key_content_builders import key_content

from central.catalog import Candidate, SourcePool
from central.photo_library.model import CopySize, LibraryAsset, OriginalFile
from central.photo_library.rendition import copy_ladder, copy_to_measure, readable_size
from central.planner import project
from central.runtime import Contribution, Runtime, Scene
from central.wall_plan.assignment import fit_of
from central.wall_plan.fit import DoesNotFit, Fits, FitTarget
from central.wall_plan.fit_settings import FitSettings
from central.wall_plan.key_content import Measured
from central.wall_plan.order import PoolLines, WallHistory
from central.wall_plan.selection import SceneSelection
from contracts.models import FrameProfile, Mat, OutputBinding

RAW = "image/x-sony-arw"
SHA = "a" * 40
P1080 = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24)
P720 = FrameProfile(width_px=1280, height_px=720, diagonal_inches=24)
HOUSE = FitSettings()


def raw_photo(*, edited: bool = False) -> LibraryAsset:
    return LibraryAsset(
        connection_ref="main", upstream_id="00000000-0000-4000-8000-000000000001",
        revision=SHA + ("-" + "b" * 16 if edited else ""), kind="image", taken_at=1.0,
        width=6000, height=4000, original=OriginalFile(media_type=RAW, width=6000, height=4000))


def preview(width: int = 1440, height: int = 960) -> dict:
    return {"preview": CopySize(name="preview", media_type="image/jpeg", width=width,
                                height=height, measured_at=1.0)}


def candidate(readable: tuple[int, int] | None) -> Candidate:
    return Candidate(asset_id="raw", kind="image", original_width=6000, original_height=4000,
                     captured_at=0, readable_size=readable,
                     analysis=Measured(key=key_content(6000, 4000)))


def test_an_unedited_raw_with_full_size_copies_off_is_read_at_its_preview():
    assert copy_ladder(raw_photo(), False) == ("preview",)
    assert readable_size(raw_photo(), False, preview()) == (1440, 960)
    # Unmeasured: no size, never the original's nor a guessed preview's.
    assert readable_size(raw_photo(), False, {}) is None
    assert copy_to_measure(raw_photo(), None, {}) == "preview"
    assert copy_to_measure(raw_photo(), None, preview()) is None
    # Full-size copies on, or an edited photo: Immich keeps a copy at the photo's own size.
    assert readable_size(raw_photo(), True, {}) == (6000, 4000)
    assert readable_size(raw_photo(edited=True), False, {}) == (6000, 4000)


@pytest.mark.parametrize(("profile", "fits"), [(P1080, False), (P720, True)])
def test_the_raw_preview_is_refused_on_1080p_and_accepted_on_720p(profile, fits):
    """Acceptance 1. Mutation probe 1 (judge the original's size): the 1080p case fits."""
    target = FitTarget(profile=profile, output_px=(profile.width_px, profile.height_px))
    verdict = fit_of(candidate((1440, 960)), target, HOUSE.crop_limit, HOUSE)
    if fits:
        assert isinstance(verdict, Fits)
    else:
        assert verdict == DoesNotFit(reason="below_frame_pixels")


def _plan(photo: Candidate, profile: FrameProfile):
    runtime = Runtime(house=UTC_HOUSE)
    runtime.set_scene(Scene(scene_id="scene", cycle_seconds=30, loop=True,
                            selection=SceneSelection(),
                            contributions=(Contribution(target="frame:f", source_refs=("pool",)),)))
    runtime.activate("scene", "run", 0)
    return project(
        runtime, 0, bindings_by_player={"player": (OutputBinding(
            output_id="out", frame_id="f", generation=1, profile=profile, mat=Mat()),)},
        lines=PoolLines({"pool": SourcePool(source_ref="pool", ready=True, candidates=(photo,))}),
        authored_candidates={}, locked_assignments={}, history=WallHistory(),
        horizon_seconds=300, plan_seconds=600, settings=HOUSE,
        frame_outputs={"f": (profile.width_px, profile.height_px)})


def test_an_unmeasured_raw_is_not_placed_and_its_measurement_is_asked_for():
    """Acceptance 1, unmeasured. Mutation probe 2 (assume a preview size): it is placed."""
    projection = _plan(candidate(None), P720)
    assert all(entry.state == "unfilled" for entry in projection.plan)
    assert "raw" in {request.asset_id for request in projection.measurements}
    assert not projection.acquisitions
    # Measured at 1440 x 960, the same photo is planned on the 720p Frame (asked to be prepared).
    measured = _plan(candidate((1440, 960)), P720)
    assert {request.asset_id for request in measured.acquisitions} == {"raw"}
