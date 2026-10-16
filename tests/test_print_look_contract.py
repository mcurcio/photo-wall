"""The print look's shared contracts (design /print-look, 2026-10-12): the one kelvin-to-white
conversion (contracts/kelvin.py), a LUT's name and layout (contracts/lut.py), and, since room light
(decision 0028), the Frame's lighting on its binding (contracts/models.py) and in Position's trial
(contracts/node_calibration.py)."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from contracts.frame_lighting import CloseLight, FrameLighting, Paper, WashLight
from contracts.kelvin import KELVIN_TABLE_MAX, KELVIN_TABLE_MIN, KELVIN_WHITE, kelvin_white
from contracts.lut import LUT_BYTES, LUT_GRID, LutRef, lut_offset
from contracts.models import FrameProfile, OutputBinding
from contracts.node_calibration import (
    TrialCandidate,
    candidate_hash,
    canonical,
    encode_trial,
    parse_trial,
)
from contracts.node_display import Surface
from contracts.node_picture import KELVIN_MAX, KELVIN_MIN
from contracts.node_protocol import NodeProcessIdentity, OutputKey

LIGHTING = FrameLighting(mat_depth_mm=0.0, lights=(WashLight(light="room", lux=1000.0, reference="dimmer-at-full"),),
                         paper=Paper(whiteness=0.9, ink_density=2.3))
SPOT_LIGHTING = LIGHTING.model_copy(update={"mat_depth_mm": 2.5, "lights": (
    CloseLight(light="bookshelf-spot", lux=800.0, reference="dimmer-at-full", across_mm=0, up_mm=600, out_mm=900,
               inner_beam_degrees=20, outer_beam_degrees=40),)})


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


def test_the_lighting_rides_the_binding():
    binding = OutputBinding(output_id="HDMI-A-1", frame_id="f", generation=1,
                            profile=FrameProfile(width_px=1920, height_px=1080, diagonal_inches=27), lighting=LIGHTING)
    round_trip = OutputBinding.model_validate_json(
        binding.model_copy(update={"lighting": SPOT_LIGHTING}).model_dump_json())
    assert round_trip.lighting == SPOT_LIGHTING


def test_the_trial_candidate_hash_covers_the_lighting():
    """Mutation probe: leave "lighting" out of candidate_hash -> the two hashes are equal."""
    trial_id = uuid4()
    calibration = '{"fit":{"corners":[[0,0],[1,0],[1,1],[0,1]],"mode":"cover","rotation":0},"revision":1}'
    baseline = _surface()
    assert candidate_hash(trial_id, 1, 1, baseline, calibration, _json(SPOT_LIGHTING)) != candidate_hash(
        trial_id, 1, 1, baseline, calibration, _json(LIGHTING))


def test_the_frames_wires_refuse_a_message_without_its_lighting():
    """No backwards compatibility: a trial envelope without `lighting_json`, and a binding without
    `lighting` (or with the print look's `mat`), are refused, never read as a default (decision 0028).

    Mutation probes: let parse_trial take a missing lighting_json (a default on TrialCandidate) ->
    the envelope is read; default OutputBinding.lighting -> the binding without it is read."""
    trial_id, baseline = uuid4(), _surface()
    calibration = '{"fit":{"corners":[[0,0],[1,0],[1,1],[0,1]],"mode":"cover","rotation":0},"revision":1}'
    lighting = _json(LIGHTING)
    envelope = json.loads(encode_trial(TrialCandidate(
        trial_id, 1, 1, baseline, calibration, candidate_hash(trial_id, 1, 1, baseline, calibration, lighting),
        5000, 9000, lighting)))
    assert parse_trial(canonical(envelope)).lighting_json == lighting
    del envelope["lighting_json"]
    with pytest.raises(ValueError, match="trial_envelope_invalid"):
        parse_trial(canonical(envelope))
    binding = {"output_id": "HDMI-A-1", "frame_id": "f", "generation": 1,
               "profile": {"width_px": 1920, "height_px": 1080, "diagonal_inches": 27}}
    for bad in (binding, {**binding, "mat": {"depth_mm": 0.0, "lights": []}},
                {**binding, "lighting": LIGHTING.model_dump(mode="json"), "mat": {"depth_mm": 0.0, "lights": []}}):
        with pytest.raises(ValidationError):
            OutputBinding.model_validate(bad)
    assert OutputBinding.model_validate({**binding, "lighting": LIGHTING.model_dump(mode="json")}).lighting == LIGHTING


def _json(lighting: FrameLighting) -> str:
    return canonical(lighting.model_dump(mode="json"))


def _surface() -> Surface:
    """A Surface for hashing (its values are irrelevant: the hash only needs one)."""
    return Surface(OutputKey(uuid4(), uuid4(), "HDMI-A-1", 1, 1), NodeProcessIdentity(42, 7, uuid4()), 1, 1, 1, "f")
