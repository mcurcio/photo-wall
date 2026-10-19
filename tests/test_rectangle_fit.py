"""The Rectangle as a Position form (render redesign S2 B2, run ledger
.claude/runs/render-planes/s2-page.md): `RectangleFit` (its rule is contracts/frame_fit.py
`FrameRectangle`'s), the only Position form (S5 B6), where it lands on its screen as the screen hangs (`frame_placement`), the
Frame output it gives (`frame_output`) and Save's off-screen check (`off_screen`). Pure."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from support.position import LIGHTING

from central.fleet.node_calibration import TrialEdit
from contracts.frame_fit import Placement
from contracts.models import (
    Calibration,
    FrameProfile,
    RectangleFit,
    frame_output,
    frame_placement,
    off_screen,
)

PROFILE = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24)
UHD = (3840, 2160)
OFF = RectangleFit(centre_x=0.1, width=0.5)


def test_the_default_rectangle_is_the_whole_width_centred():
    assert RectangleFit().model_dump() == {"mode": "rectangle", "centre_x": 0.5, "centre_y": 0.5, "width": 1.0}


@pytest.mark.parametrize("fields", [{"width": 0}, {"width": 1.5}, {"centre_x": -0.1}, {"centre_y": 1.1}])
def test_a_rectangle_outside_its_ranges_is_refused(fields):
    with pytest.raises(ValidationError):
        RectangleFit(**fields)


@pytest.mark.parametrize("field", ["centre_x", "centre_y", "width"])
def test_a_json_true_is_not_a_number_under_the_routes_strict_parse(field):
    fit = {"mode": "rectangle", "centre_x": 0.5, "centre_y": 0.5, "width": 0.5, field: True}
    with pytest.raises(ValidationError):
        Calibration.model_validate_json(json.dumps({"fit": fit}), strict=True)


def test_a_rectangle_is_a_position_form_a_save_and_a_trial_edit_take():
    body = {"mode": "rectangle", "centre_x": 0.25, "centre_y": 0.5, "width": 0.5}
    calibration = Calibration.model_validate({"fit": body})
    assert calibration.fit == RectangleFit(centre_x=0.25, centre_y=0.5, width=0.5)
    edit = TrialEdit.model_validate({"operation": "edit", "expected_sequence": 1,
                                     "calibration": calibration.model_dump(),
                                     "lighting": LIGHTING.model_dump()})
    assert edit.calibration.fit == calibration.fit


def test_the_rectangle_lands_on_the_screen_as_it_hangs():
    """A 16:9 Frame across the whole width: on a 4K screen hung level it fills the screen; hung a
    quarter turn either way the screen is 2160 wide and the Frame 2160 x 1215, centred down it."""
    assert frame_placement(PROFILE, RectangleFit(), UHD, 0) == Placement(0, 0, 3840, 2160)
    assert frame_placement(PROFILE, RectangleFit(), UHD, 180) == Placement(0, 0, 3840, 2160)
    for turn in (90, 270):
        assert frame_placement(PROFILE, RectangleFit(), UHD, turn) == Placement(0, 1313, 2160, 1215)


def test_a_rectangle_off_its_screen_has_no_placement():
    with pytest.raises(ValueError, match="placement_outside"):
        frame_placement(PROFILE, OFF, UHD, 0)


def test_the_frame_output_of_a_rectangle_is_its_placement_size():
    turned = Calibration(fit=RectangleFit())
    assert frame_output(PROFILE, turned, UHD, 90) == (2160, 1215)
    assert frame_output(PROFILE, Calibration(fit=OFF), UHD, 0) is None
    assert frame_output(PROFILE, turned, None, 90) is None


@pytest.mark.parametrize("fit", [
    {"mode": "cover"},
    {"mode": "cover", "corners": [[0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75]], "rotation": 7},
    {"mode": "corners"},
    {"mode": "corners", "corners": [[0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75]], "rotation": 90},
], ids=["cover", "cover-opening", "corners", "corners-opening"])
def test_a_cover_or_corners_calibration_is_a_validation_error(fit):
    """Cover and Corners are gone (S5 B6): a stored record or body in either form is refused, never
    read or defaulted. Mutation probe: keep CoverFit in `Calibration.fit` -> the Cover cases pass."""
    with pytest.raises(ValidationError):
        Calibration.model_validate({"revision": 1, "fit": fit})


def test_off_screen_is_a_rectangle_that_does_not_fit_a_known_screen():
    assert off_screen(PROFILE, Calibration(fit=OFF), UHD, 0)
    assert not off_screen(PROFILE, Calibration(fit=RectangleFit()), UHD, 90)
    assert not off_screen(PROFILE, Calibration(fit=OFF), None, 0)
    assert not off_screen(PROFILE, Calibration(fit=OFF), (0, 0), 0)
