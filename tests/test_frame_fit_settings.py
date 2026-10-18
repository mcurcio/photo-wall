"""The Frame fit through Position's Save (roadmap 1c, slice C3; design "Frame fit and Cover"; owner q1).

Every Save crosses Registry's Save CAS (`commit_calibration_trial_in`, what a trial's Save calls;
tests/support/position.py reads the binding baseline a trial would carry). Save writes the fit and
nothing else: the Frame profile is the one source of the Frame's aspect, set on the Hardware tab
or by the display-changed card, and the angle never changes it or is checked against it (owner,
2026-10-11: ""landscape" and "portrait" are descriptions of the aspect ratio. whereas the angle of
rotation is a quality of the way frames are hung on the wall"). The Player configuration carries
the fit in `calibration`.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError
from support.position import save_position
from test_registry import enroll

from central.displays.model import Readiness
from central.fleet.models import TrialRefusal, TrialRefused
from central.fleet.position_history import PositionEntryKind
from central.infra.position_history_store import latest_kept_position_in, latest_position_in
from central.registry import FrameCreate
from contracts.models import Calibration, CornersFit, CoverFit, FrameProfile, RectangleFit

LANDSCAPE = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24)
FOLDED = ((0, 0), (1, 1), (1, 0), (0, 1))


def _bound(registry, frame_id: str = "lounge") -> str:
    """A landscape Frame bound to HDMI-A-1 of a Pi whose Outputs reported 1920 x 1080; its player id."""
    player, _key, _request = enroll(registry, count=1)
    registry.create_frame(FrameCreate(id=frame_id, width_mm=500, height_mm=300, profile=LANDSCAPE))
    registry.bind(frame_id, player["player_id"], "HDMI-A-1", expected_generation=0)
    return player["player_id"]


def _frame(registry, frame_id: str = "lounge"):
    return next(frame for frame in registry.inventory().frames if frame.id == frame_id)


def test_a_new_frame_starts_on_cover_at_angle_0(registry):
    """Owner q1: Cover is the default for new Frames, at angle 0."""
    _bound(registry)
    assert _frame(registry).calibration.fit == CoverFit()


def test_a_cover_save_at_a_fine_angle_reaches_the_player_configuration_and_the_frame_is_ready(registry):
    """(1) Cover at 7 degrees on the whole output: the fit reaches the Player configuration, the
    Frame is ready at its generation, and its profile is the one it had."""
    player = _bound(registry)
    generation = _frame(registry).generation
    save_position(registry, "lounge", Calibration(fit=CoverFit(rotation=7)))
    frame = _frame(registry)
    assert (frame.readiness, frame.generation, frame.profile) == (Readiness.READY, generation, LANDSCAPE)
    [binding] = registry.configuration_for(player, 1)["execution_bindings"]
    assert binding.calibration.fit == CoverFit(rotation=7)
    assert binding.profile == LANDSCAPE


@pytest.mark.parametrize("rotation", [60, 90, 135])
def test_a_landscape_frame_saves_cover_at_any_angle_and_stays_landscape(registry, rotation):
    """(3) Owner 2026-10-11: the angle is how the Frame is hung, never its orientation. A
    landscape Frame (500 x 300 mm, 1920 x 1080) saves Cover at 60 degrees, where the corners'
    bounding box is portrait, and its profile, the one source of its orientation, stays landscape.
    Mutation probe: write a profile from the turned corners at Save, or refuse the angle -> this
    fails."""
    _bound(registry)
    save_position(registry, "lounge", Calibration(fit=CoverFit(rotation=rotation)))
    frame = _frame(registry)
    assert (frame.calibration.fit, frame.readiness) == (CoverFit(rotation=rotation), Readiness.READY)
    assert frame.profile == LANDSCAPE and frame.profile.width_px > frame.profile.height_px


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


# --- The Rectangle (render redesign S2 B2, run ledger .claude/runs/render-planes/s2-page.md) -----

UHD_LANDSCAPE = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=55)
OFF_SCREEN = RectangleFit(centre_x=0.1, width=0.5)


def _turned_on_uhd(registry, frame_id: str = "lounge", size=(3840, 2160)) -> str:
    """A 16:9 Frame bound to HDMI-A-1 of a Pi whose Output reported `size`, turned 90 (its
    Position then saved again, so it is READY); its player id."""
    player, _key, _request = enroll(registry, count=1, size=size)
    registry.create_frame(FrameCreate(id=frame_id, width_mm=500, height_mm=300, profile=UHD_LANDSCAPE))
    registry.bind(frame_id, player["player_id"], "HDMI-A-1", expected_generation=0)
    registry.turn_frame(frame_id, 90, expected_generation=_frame(registry, frame_id).generation)
    save_position(registry, frame_id)
    return player["player_id"]


def test_a_rectangle_save_reaches_the_player_configuration_and_its_history(registry):
    """A Rectangle is a Position form: Save keeps it, the Player configuration carries it, the
    Frame is READY and the Save's kept-saved history entry records it."""
    player = _bound(registry)
    rectangle = RectangleFit(centre_x=0.25, centre_y=0.5, width=0.5)
    save_position(registry, "lounge", Calibration(fit=rectangle))
    assert _frame(registry).readiness is Readiness.READY
    [binding] = registry.bindings_for(player, 1)
    assert binding.calibration.fit == rectangle
    with registry.db.transaction() as conn:
        entry = latest_kept_position_in(conn, "lounge")
    assert (entry.record.kind, entry.record.values.fit) == (PositionEntryKind.KEPT_SAVED, rectangle)


def test_a_rectangle_off_its_turned_screen_is_refused_before_any_write(registry):
    """On a 3840 x 2160 Output hung a quarter turn the screen is 2160 wide: a half-width Rectangle
    centred 10 % across runs off its left edge. Save refuses it (422) and writes nothing."""
    _turned_on_uhd(registry)
    before = _frame(registry)
    with registry.db.transaction() as conn:
        latest = latest_position_in(conn, "lounge")
    with pytest.raises(TrialRefused) as refused:
        save_position(registry, "lounge", Calibration(fit=OFF_SCREEN))
    assert (refused.value.code, refused.value.status) == (TrialRefusal.POSITION_OFF_SCREEN, 422)
    after = _frame(registry)
    assert (after.calibration.revision, after.generation) == (before.calibration.revision, before.generation)
    with registry.db.transaction() as conn:
        assert latest_position_in(conn, "lounge") == latest


def test_a_rectangle_on_an_output_with_no_reported_size_is_not_checked(registry):
    """The same Save on an Output that reported 0 x 0 (a real Pi today) succeeds: nothing to check."""
    _turned_on_uhd(registry, size=(0, 0))
    save_position(registry, "lounge", Calibration(fit=OFF_SCREEN))
    assert _frame(registry).calibration.fit == OFF_SCREEN
