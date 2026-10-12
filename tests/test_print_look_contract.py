"""The print look's shared contracts (design /print-look, 2026-10-12): the one kelvin-to-white
conversion (contracts/kelvin.py), a LUT's name and layout (contracts/lut.py), the Frame's Mat and
Light on its binding (contracts/models.py) and the mat in Position's trial
(contracts/node_calibration.py)."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from contracts.kelvin import KELVIN_TABLE_MAX, KELVIN_TABLE_MIN, KELVIN_WHITE, kelvin_white
from contracts.lut import LUT_BYTES, LUT_GRID, LutRef, lut_offset
from contracts.models import MAT_LIGHTS_MAX, FrameProfile, Light, Mat, OutputBinding
from contracts.node_calibration import NO_MAT_JSON, candidate_hash, canonical
from contracts.node_display import Surface
from contracts.node_picture import KELVIN_MAX, KELVIN_MIN
from contracts.node_protocol import NodeProcessIdentity, OutputKey

WARM_TOP = Light(from_degrees=0, steep_degrees=45, softness_degrees=10, strength_tenths=10, colour_kelvin=2700)
COOL_SIDE = Light(from_degrees=90, steep_degrees=20, softness_degrees=30, strength_tenths=15, colour_kelvin=7500)


def test_kelvin_white_is_the_displays_white_at_6500_and_covers_the_lights_range():
    """Mutation probe: normalise by the encoded rows (not linear light) -> 2700 K's green moves
    from ~0.17 to ~0.45 and the bound below fails."""
    assert kelvin_white(KELVIN_WHITE) == (1.0, 1.0, 1.0)
    for kelvin in range(KELVIN_TABLE_MIN, KELVIN_TABLE_MAX + 1, 100):
        white = kelvin_white(kelvin)
        assert max(white) == 1.0 and min(white) > 0
    warm = kelvin_white(2700)
    assert warm[0] == 1.0 and 0.1 < warm[1] < 0.2 and warm[2] < 0.01
    assert KELVIN_TABLE_MIN <= KELVIN_MIN and KELVIN_MAX <= KELVIN_TABLE_MAX   # the software range is inside
    for bad in (1900, 2050, 10100, 6500.0, True):
        with pytest.raises(ValueError, match="^kelvin$"):
            kelvin_white(bad)


def test_a_lut_is_named_by_its_inputs_and_laid_out_red_fastest():
    ref = LutRef("ab" * 32, "lut33-perceptual-relcol-bpc-1")
    assert ref.path == f"/v1/luts/{'ab' * 32}/lut33-perceptual-relcol-bpc-1"
    assert ref.key == f"{'ab' * 32}.lut33-perceptual-relcol-bpc-1"
    assert LUT_BYTES == LUT_GRID ** 3 * 3 == 107811
    assert (lut_offset(0, 0, 0), lut_offset(1, 0, 0), lut_offset(0, 1, 0), lut_offset(0, 0, 1)) == (
        0, 3, 3 * LUT_GRID, 3 * LUT_GRID ** 2)
    assert lut_offset(LUT_GRID - 1, LUT_GRID - 1, LUT_GRID - 1) == LUT_BYTES - 3
    for bad in ((LUT_GRID, 0, 0), (-1, 0, 0), (0.0, 0, 0)):
        with pytest.raises(ValueError, match="^lut_point$"):
            lut_offset(*bad)
    for profile, recipe in (("ab" * 31, "r"), ("AB" * 32, "r"), ("ab" * 32, ""), ("ab" * 32, "R"),
                            ("ab" * 32, "r" * 65), ("ab" * 32, "-r")):
        with pytest.raises(ValueError, match="^lut_ref$"):
            LutRef(profile, recipe)


def test_the_mat_rides_the_binding_and_shades_only_with_depth_and_a_light():
    binding = OutputBinding(output_id="HDMI-A-1", frame_id="f", generation=1,
                            profile=FrameProfile(width_px=1920, height_px=1080, diagonal_inches=27))
    assert binding.mat == Mat() and not binding.mat.shades
    assert canonical(Mat().model_dump(mode="json")) == NO_MAT_JSON
    assert Mat(depth_mm=1.5, lights=(WARM_TOP, COOL_SIDE)).shades
    assert not Mat(depth_mm=0, lights=(WARM_TOP,)).shades
    assert not Mat(depth_mm=2, lights=()).shades
    assert not Mat(depth_mm=2, lights=(WARM_TOP.model_copy(update={"strength_tenths": 0}),)).shades
    round_trip = OutputBinding.model_validate_json(
        binding.model_copy(update={"mat": Mat(depth_mm=2.5, lights=(COOL_SIDE,))}).model_dump_json())
    assert round_trip.mat.lights == (COOL_SIDE,)


@pytest.mark.parametrize("bad", [
    {"depth_mm": 20.5}, {"depth_mm": 1.25}, {"depth_mm": -0.5},
    {"lights": [WARM_TOP.model_dump()] * (MAT_LIGHTS_MAX + 1)},
    {"lights": [{**WARM_TOP.model_dump(), "from_degrees": 360}]},
    {"lights": [{**WARM_TOP.model_dump(), "steep_degrees": 4}]},
    {"lights": [{**WARM_TOP.model_dump(), "softness_degrees": 46}]},
    {"lights": [{**WARM_TOP.model_dump(), "strength_tenths": 101}]},
    {"lights": [{**WARM_TOP.model_dump(), "colour_kelvin": 2750}]},
    {"lights": [{**WARM_TOP.model_dump(), "colour_kelvin": 10100}]},
    {"lights": [{**WARM_TOP.model_dump(), "colour_kelvin": 6500, "extra": 1}]},
])
def test_the_mat_refuses_out_of_range(bad):
    with pytest.raises(ValidationError):
        Mat.model_validate(bad)


def test_the_trial_candidate_hash_covers_the_mat():
    """Mutation probe: leave "mat" out of candidate_hash -> the two hashes are equal."""
    trial_id = uuid4()
    calibration = '{"fit":{"corners":[[0,0],[1,0],[1,1],[0,1]],"mode":"cover","rotation":0},"revision":1}'
    mat = canonical(Mat(depth_mm=1.5, lights=(WARM_TOP,)).model_dump(mode="json"))
    assert json.loads(mat)["lights"][0]["colour_kelvin"] == 2700
    baseline = _surface()
    assert candidate_hash(trial_id, 1, 1, baseline, calibration) == candidate_hash(
        trial_id, 1, 1, baseline, calibration, NO_MAT_JSON)
    assert candidate_hash(trial_id, 1, 1, baseline, calibration, mat) != candidate_hash(
        trial_id, 1, 1, baseline, calibration, NO_MAT_JSON)


def _surface() -> Surface:
    """A Surface for hashing (its values are irrelevant: the hash only needs one)."""
    return Surface(OutputKey(uuid4(), uuid4(), "HDMI-A-1", 1, 1), NodeProcessIdentity(42, 7, uuid4()), 1, 1, 1, "f")
