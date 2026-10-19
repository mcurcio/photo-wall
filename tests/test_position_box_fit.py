"""The box as the Position (frame box run, .claude/runs/frame-box/page.md, bead fb-6; owner,
2026-10-19: "I need to be able to set width and height independently, along with the position of
that box on the display"): `Calibration.fit` is a `PositionBox`, where it lands on its screen as the
screen hangs (`frame_placement`) and the Frame output it gives (`frame_output`), which takes its
shape from the box alone. Pure, but for the vocabulary check, which reads the repository.

Mutation probe: give `frame_output` a profile's 16:9 aspect for the height -> the 480 x 540 case
goes RED."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError
from support.position import LIGHTING

from central.fleet.models import TRIAL_REFUSAL_STATUS, TrialRefusal
from central.fleet.node_calibration import TrialEdit
from contracts.frame_fit import Placement, place_box
from contracts.models import Calibration, PositionBox, frame_output, frame_placement

ROOT = Path(__file__).resolve().parents[1]
UHD = (3840, 2160)
HD = (1920, 1080)


def test_a_new_frames_position_is_the_whole_screen():
    assert Calibration().fit == PositionBox()
    assert PositionBox().model_dump() == {"mode": "box", "left": 0.0, "top": 0.0, "width": 1.0, "height": 1.0}


@pytest.mark.parametrize("field", ["left", "top", "width", "height"])
def test_a_json_true_is_not_a_number_under_the_routes_strict_parse(field):
    fit = {"mode": "box", "left": 0.25, "top": 0.25, "width": 0.5, "height": 0.5, field: True}
    with pytest.raises(ValidationError):
        Calibration.model_validate_json(json.dumps({"fit": fit}), strict=True)


@pytest.mark.parametrize("fit", [
    {"mode": "rectangle", "centre_x": 0.5, "centre_y": 0.5, "width": 0.5},
    {"mode": "box", "left": 0.6, "top": 0, "width": 0.5, "height": 0.5},
    {"mode": "box", "left": 0, "top": 0, "width": 0, "height": 0.5},
    {"mode": "cover"},
], ids=["rectangle", "past-the-right-edge", "no-width", "cover"])
def test_only_a_box_on_its_screen_is_a_position(fit):
    """The rectangle (v0.41.0), Cover and Corners are gone: a stored record or body in those forms
    is refused, never read or defaulted; a box past the screen's edge is refused at construction."""
    with pytest.raises(ValidationError):
        Calibration.model_validate({"revision": 1, "fit": fit})


def test_a_box_is_a_position_form_a_save_and_a_trial_edit_take():
    body = {"mode": "box", "left": 0.25, "top": 0.1, "width": 0.25, "height": 0.5}
    calibration = Calibration.model_validate({"fit": body})
    assert calibration.fit == PositionBox(left=0.25, top=0.1, width=0.25, height=0.5)
    edit = TrialEdit.model_validate({"operation": "edit", "expected_sequence": 1,
                                     "calibration": calibration.model_dump(),
                                     "lighting": LIGHTING.model_dump()})
    assert edit.calibration.fit == calibration.fit


def test_the_frame_output_is_the_boxs_own_shape_not_16_9():
    """A box a quarter wide and half high on 1920 x 1080 is 480 x 540: the Frame's shape is the box's."""
    box = Calibration(fit=PositionBox(left=0.5, top=0.25, width=0.25, height=0.5))
    assert frame_output(box, HD, 0) == (480, 540)
    assert frame_placement(box.fit, HD, 0) == Placement(960, 270, 480, 540)


def test_the_box_lands_on_the_screen_as_it_hangs():
    """A 3840 x 2160 mode hung a quarter turn either way is a 2160 x 3840 screen: the box's fractions
    are of that screen."""
    box = PositionBox(left=0.25, top=0.5, width=0.5, height=0.25)
    for turn in (90, 270):
        assert frame_placement(box, UHD, turn) == place_box(box.box(), (2160, 3840)) == Placement(540, 1920, 1080, 960)
    for turn in (0, 180):
        assert frame_placement(box, UHD, turn) == Placement(960, 1080, 1920, 540)
    assert frame_output(Calibration(), UHD, 90) == (2160, 3840)


def test_no_known_screen_has_no_frame_output():
    for output in (None, (0, 0), (1920, 0)):
        assert frame_output(Calibration(), output, 0) is None


def test_save_has_no_off_screen_refusal_and_nothing_names_the_old_forms():
    """A box always lies on its screen, so Save refuses no Position for where it lies: the refusal
    is gone from the vocabulary, and neither it nor the rectangle form is named anywhere in code
    outside the migrations that converted them (and their tests)."""
    assert "position_off_screen" not in {refusal.value for refusal in TrialRefusal}
    assert set(TRIAL_REFUSAL_STATUS) == set(TrialRefusal)
    found = subprocess.run(
        ["git", "grep", "-n", "-e", "RectangleFit", "-e", "position_off_screen", "--",
         ".", ":!docs", ":!.claude", ":!AGENTS.md", ":!central/migrations", ":!tests/test_migration_*", ":!tests/test_position_box_fit.py"],
        cwd=ROOT, capture_output=True, text=True, check=False)
    assert found.returncode == 1, found.stdout
