"""The Frame fit through Position's Save (roadmap 1c, slice C3; design "Frame fit and Cover"; owner q1).

Every Save crosses Registry's Save CAS (`commit_calibration_trial_in`, what a trial's Save calls;
tests/support/position.py reads the binding baseline a trial would carry). In Cover, Save writes the
Frame profile's shape (the cover rectangle at the bound Output's size) as part of the commit, so the
Frame stays ready; it refuses a cover rectangle whose orientation disagrees with the Frame's
physical one (run ledger F-4), and an Output that reported no size. In Corners the profile is set as
before. The Player configuration carries the fit in `calibration`.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError
from support.position import save_position
from test_registry import enroll

from central.displays.model import Readiness
from central.registry import FrameCreate, RegistryError
from contracts.models import Calibration, CornersFit, CoverFit, FrameProfile

LANDSCAPE = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24)
PORTRAIT = FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)
CENTRED = ((0.25, 0.25), (0.75, 0.25), (0.75, 0.75), (0.25, 0.75))   # 960 x 540 of a 1920 x 1080 output
FOLDED = ((0, 0), (1, 1), (1, 0), (0, 1))


def _bound(registry, frame_id: str = "lounge", *, landscape: bool = True) -> str:
    """A Frame bound to HDMI-A-1 of a Pi whose Outputs reported 1920 x 1080; its player id."""
    player, _key, _request = enroll(registry, count=1)
    registry.create_frame(FrameCreate(id=frame_id, width_mm=500 if landscape else 300,
                                      height_mm=300 if landscape else 500,
                                      profile=LANDSCAPE if landscape else PORTRAIT))
    registry.bind(frame_id, player["player_id"], "HDMI-A-1", expected_generation=0)
    return player["player_id"]


def _frame(registry, frame_id: str = "lounge"):
    return next(frame for frame in registry.inventory().frames if frame.id == frame_id)


def test_a_new_frame_starts_on_cover_at_angle_0(registry):
    """Owner q1: Cover is the default for new Frames, at angle 0."""
    _bound(registry)
    assert _frame(registry).calibration.fit == CoverFit()


def test_a_cover_save_at_a_fine_angle_reaches_the_player_configuration_and_the_frame_is_ready(registry):
    """(1) and (2): Cover at 7 degrees on the whole output; Save writes the cover rectangle's shape
    (1920 cos 7 + 1080 sin 7 = 2037 by 1920 sin 7 + 1080 cos 7 = 1306) without raising the
    generation. Mutation probe: raise the generation when writing the profile -> not ready."""
    player = _bound(registry)
    generation = _frame(registry).generation
    save_position(registry, "lounge", Calibration(fit=CoverFit(rotation=7)))
    frame = _frame(registry)
    assert (frame.readiness, frame.generation) == (Readiness.READY, generation)
    assert (frame.profile.width_px, frame.profile.height_px) == (2037, 1306)
    assert (frame.profile.diagonal_inches, frame.profile.video) == (24, True)
    [binding] = registry.configuration_for(player, 1)["execution_bindings"]
    assert binding.calibration.fit == CoverFit(rotation=7)
    assert binding.profile == frame.profile


def test_a_cover_save_writes_the_openings_shape(registry):
    """(2) A centred 960 x 540 opening at 0 degrees: the profile is 960 x 540, and the Frame stays ready."""
    _bound(registry)
    generation = _frame(registry).generation
    save_position(registry, "lounge", Calibration(fit=CoverFit(corners=CENTRED)))
    frame = _frame(registry)
    assert (frame.profile.width_px, frame.profile.height_px, frame.readiness) == (960, 540, Readiness.READY)
    assert frame.generation == generation   # the Player's surface grants are not fenced by Save


def test_a_cover_rectangle_that_turns_the_frames_orientation_is_refused(registry):
    """(3) Run ledger F-4: a landscape Frame at 60 degrees has a portrait cover rectangle; a portrait
    Frame at 90 degrees on a landscape output agrees. Mutation probe: skip the orientation check ->
    the 60-degree Save lands."""
    _bound(registry)
    with pytest.raises(RegistryError) as refused:
        save_position(registry, "lounge", Calibration(fit=CoverFit(rotation=60)))
    assert (refused.value.code, refused.value.status) == ("fit_orientation_mismatch", 409)
    assert (_frame(registry).calibration.revision, _frame(registry).readiness) == (1, Readiness.POSITION_NEEDED)

    _bound(registry, "hall", landscape=False)
    save_position(registry, "hall", Calibration(fit=CoverFit(rotation=90)))
    hall = _frame(registry, "hall")
    assert (hall.profile.width_px, hall.profile.height_px, hall.readiness) == (1080, 1920, Readiness.READY)


def test_a_corners_save_leaves_the_profile_as_it_was(registry):
    """(4) Corners stretches onto a skewed quad; its profile is set as before. Mutation probe: write
    the profile in Corners -> the profile changes."""
    _bound(registry)
    skewed = CornersFit(corners=((0.1, 0.05), (0.95, 0), (1, 1), (0, 0.9)), rotation=180)
    save_position(registry, "lounge", Calibration(fit=skewed))
    frame = _frame(registry)
    assert (frame.calibration.fit, frame.profile, frame.readiness) == (skewed, LANDSCAPE, Readiness.READY)


def test_a_folded_quad_is_refused_in_either_fit_before_any_write(registry):
    """(5) One check, both fits (contracts/frame_fit.py `convex_clockwise`)."""
    _bound(registry)
    before = _frame(registry)
    for fit in (CoverFit, CornersFit):
        with pytest.raises(ValidationError, match="convex"):
            fit(corners=FOLDED)
        with pytest.raises(ValidationError, match="convex"):
            Calibration.model_validate({"revision": 1, "fit": {"mode": fit().mode, "corners": FOLDED}})
    assert _frame(registry) == before


def test_a_cover_save_needs_the_outputs_size(registry):
    """(6) An Output that reported no size: Cover cannot size the picture, so Save refuses."""
    player = _bound(registry)
    with registry.db.transaction() as conn:
        conn.execute("UPDATE outputs SET observation=observation || '{\"width_px\": 0}'::jsonb WHERE player_id=%s",
                     (player,))
    with pytest.raises(RegistryError) as refused:
        save_position(registry, "lounge", Calibration(fit=CoverFit()))
    assert (refused.value.code, refused.value.status) == ("output_size_unknown", 409)
    save_position(registry, "lounge", Calibration(fit=CornersFit()))   # Corners needs no size
    assert _frame(registry).readiness is Readiness.READY
