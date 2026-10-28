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
from contracts.kelvin import (
    _CHARITY,
    KELVIN_TABLE_MAX,
    KELVIN_TABLE_MIN,
    KELVIN_WHITE,
    kelvin_white,
)
from contracts.lut import LUT_BYTES, LUT_GRID, LutRef, lut_offset
from contracts.models import OutputBinding
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

LIGHTING = FrameLighting(mat_depth_mm=0.0, lights=(WashLight(light="room", lux=1000.0, reference={"kind": "at-full"}),),
                         paper=Paper(whiteness=0.9, ink_density=2.3))
SPOT_LIGHTING = LIGHTING.model_copy(update={"mat_depth_mm": 2.5, "lights": (
    CloseLight(light="bookshelf-spot", lux=800.0, reference={"kind": "at-full"}, across_mm=0, up_mm=600, out_mm=900,
               inner_beam_degrees=20, outer_beam_degrees=40),)})


# Charity's published 2deg rows (bbr_color.txt 2001-Jun-22), copied by hand: the decimal R G B and
# the 0-255 r g b columns. The decimals are linear light (encoding them with the sRGB curve gives the
# bytes, and as linear sRGB they give the file's own x y), so kelvin_white divides them as they are.
CHARITY_ROWS = {
    3000: ((1.0000, 0.4870, 0.1411), (255, 185, 105)),
    4300: ((1.0000, 0.7083, 0.4310), (255, 219, 175)),
    5500: ((1.0000, 0.8541, 0.7277), (255, 238, 222)),
    6500: ((1.0000, 0.9436, 0.9621), (255, 249, 251)),
    9000: ((0.6925, 0.7522, 1.0000), (217, 225, 255)),
    9300: ((0.6703, 0.7362, 1.0000), (214, 223, 255)),
}


def _encoded(linear: float) -> float:
    """IEC 61966-2-1's encode, written out here so the test does not lean on the code it checks."""
    return 12.92 * linear if linear <= 0.0031308 else 1.055 * linear ** (1 / 2.4) - 0.055


@pytest.mark.parametrize("kelvin", sorted(CHARITY_ROWS))
def test_charitys_decimals_are_linear_light_their_bytes_the_srgb_encoding(kelvin):
    """What the table holds: its row is Charity's decimals, and each decimal, sRGB-encoded, is the
    published byte within one step (read as already encoded, 0.7083 would be byte 181, not 219)."""
    decimals, published = CHARITY_ROWS[kelvin]
    assert _CHARITY[kelvin] == decimals
    for value, byte in zip(decimals, published, strict=True):
        assert _encoded(value) * 255 == pytest.approx(byte, abs=1)


@pytest.mark.parametrize(("kelvin", "expected"), [
    (4300, (1.0, 0.7506, 0.4480)), (5500, (1.0, 0.9052, 0.7564)), (6500, (1.0, 1.0, 1.0)),
    (9000, (0.6663, 0.7669, 1.0)), (3000, (1.0, 0.5161, 0.1467)),
])
def test_kelvin_white_is_charitys_linear_row_against_the_6500_row(kelvin, expected):
    """kelvin_white is Charity's row divided by the 6500 K row, largest 1, both linear as published:
    4300 K keeps green 0.75 and blue 0.45 (the fireplace Frame, 2026-10-28: "very orange" when the
    rows were linearised a second time, giving 0.525 and 0.170). Mutation probe: take the rows
    through srgb_to_linear first -> 4300 K reads (1, 0.525, 0.170) and this fails."""
    row, white = CHARITY_ROWS[kelvin][0], CHARITY_ROWS[KELVIN_WHITE][0]
    ratio = [value / neutral for value, neutral in zip(row, white, strict=True)]
    assert [value / max(ratio) for value in ratio] == pytest.approx(expected, abs=1e-4)
    assert kelvin_white(kelvin) == pytest.approx(expected, abs=1e-4)


def test_kelvin_white_is_the_displays_white_at_6500_and_covers_the_lights_range():
    """Mutation probe: linearise the (already linear) rows a second time -> 2700 K's green moves
    from ~0.45 to ~0.17 and the bound below fails."""
    assert kelvin_white(KELVIN_WHITE) == (1.0, 1.0, 1.0)
    for kelvin in range(KELVIN_TABLE_MIN, KELVIN_TABLE_MAX + 1, 100):
        white = kelvin_white(kelvin)
        assert max(white) == 1.0 and min(white) > 0
    warm = kelvin_white(2700)
    assert warm[0] == 1.0 and 0.4 < warm[1] < 0.5 and 0.05 < warm[2] < 0.15
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
                            lighting=LIGHTING, display_mm=(600, 340))
    round_trip = OutputBinding.model_validate_json(
        binding.model_copy(update={"lighting": SPOT_LIGHTING}).model_dump_json())
    assert round_trip.lighting == SPOT_LIGHTING


def test_the_trial_candidate_hash_covers_the_lighting():
    """Mutation probe: leave "lighting" out of candidate_hash -> the two hashes are equal."""
    trial_id = uuid4()
    calibration = '{"fit":{"height":1.0,"left":0.0,"mode":"box","top":0.0,"width":1.0},"revision":1}'
    baseline = _surface()
    assert candidate_hash(trial_id, 1, 1, baseline, calibration, SPOT_LIGHTING) != candidate_hash(
        trial_id, 1, 1, baseline, calibration, LIGHTING)


def test_the_frames_wires_refuse_a_message_without_its_lighting():
    """No backwards compatibility: a trial envelope without `lighting`, and a binding without
    `lighting` (or with the print look's `mat`), are refused, never read as a default (decision 0028).

    Mutation probes: let parse_trial take a missing lighting (a default on TrialCandidate) ->
    the envelope is read; default OutputBinding.lighting -> the binding without it is read."""
    trial_id, baseline = uuid4(), _surface()
    calibration = '{"fit":{"height":1.0,"left":0.0,"mode":"box","top":0.0,"width":1.0},"revision":1}'
    lighting = LIGHTING
    envelope = json.loads(encode_trial(TrialCandidate(
        trial_id, 1, 1, baseline, calibration, candidate_hash(trial_id, 1, 1, baseline, calibration, lighting),
        5000, 9000, lighting)))
    assert parse_trial(canonical(envelope)).lighting == lighting
    del envelope["lighting"]
    with pytest.raises(ValueError, match="trial_envelope_invalid"):
        parse_trial(canonical(envelope))
    binding = {"output_id": "HDMI-A-1", "frame_id": "f", "generation": 1, "display_mm": [600, 340]}
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
