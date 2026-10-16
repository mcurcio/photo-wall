"""Position's trial carries the Frame's draft lighting to the Player (decision 0028; it replaced the
print look's mat): the applied trial holds it, a bad one is refused like a bad calibration. No
database: the envelope is built by hand with contracts.node_calibration."""
from __future__ import annotations

from uuid import uuid4

import pytest

from contracts.frame_lighting import DIMMER_AT_FULL, CloseLight, FrameLighting, Paper, WashLight
from contracts.models import Calibration, CoverFit, FrameProfile, OutputBinding
from contracts.node_calibration import TrialCandidate, candidate_hash, canonical, encode_trial
from contracts.node_display import Surface
from contracts.node_protocol import NodeProcessIdentity, OutputKey
from player.calibration_trial import TrialState
from player.rendering import OutputComposition

LIGHTING = FrameLighting(mat_depth_mm=0, lights=(WashLight(light="room", lux=1000, reference=DIMMER_AT_FULL),),
                         paper=Paper(whiteness=0.9, ink_density=3.5))
BINDING = OutputBinding(output_id="HDMI-A-1", frame_id="frame", generation=3, configuration_revision=2,
                        profile=FrameProfile(width_px=1920, height_px=1080, diagonal_inches=27), lighting=LIGHTING)
COMPOSITION = OutputComposition(BINDING, BINDING.calibration)
BASELINE = Surface(OutputKey(uuid4(), uuid4(), "HDMI-A-1", 1, 1), NodeProcessIdentity(100, 5, uuid4()),
                   1, BINDING.generation, BINDING.configuration_revision, "frame")
SPOT = CloseLight(light="bookshelf-spot", lux=400, reference=DIMMER_AT_FULL, across_mm=0, up_mm=350, out_mm=250,
                  inner_beam_degrees=20, outer_beam_degrees=40)


def as_json(lighting: FrameLighting) -> str:
    return canonical(lighting.model_dump(mode="json"))


def envelope(lighting_json: str, sequence: int = 1, trial_id=None) -> str:
    trial_id = trial_id or uuid4()
    calibration = canonical(Calibration(fit=CoverFit(rotation=7)).model_dump(mode="json"))
    return encode_trial(TrialCandidate(
        trial_id=trial_id, generation=1, sequence=sequence, baseline=BASELINE,
        calibration_json=calibration,
        candidate_sha256=candidate_hash(trial_id, 1, sequence, BASELINE, calibration, lighting_json),
        expires_boottime_ms=5000, hard_expires_boottime_ms=9000, lighting_json=lighting_json))


def test_the_applied_trial_holds_the_drafts_lighting():
    """Mutation probe: apply the binding's lighting whatever the envelope carries -> the draft is lost."""
    drafted = LIGHTING.model_copy(update={"mat_depth_mm": 2.5, "lights": (*LIGHTING.lights, SPOT)})
    applied = TrialState().apply(envelope(as_json(drafted)), COMPOSITION, 1000)
    assert applied.lighting == drafted
    assert applied.calibration.fit == CoverFit(rotation=7)


def test_an_edit_that_moves_only_the_lights_is_a_new_candidate():
    """The candidate's hash covers the lighting, so an edit of the lights alone is a change to draw."""
    state, trial_id = TrialState(), uuid4()
    first = state.apply(envelope(as_json(LIGHTING), 1, trial_id), COMPOSITION, 1000)
    drafted = as_json(LIGHTING.model_copy(update={"lights": (SPOT,)}))
    second = state.apply(envelope(drafted, 2, trial_id), COMPOSITION, 1100)
    assert second.lighting.lights == (SPOT,)
    assert second.candidate.candidate_sha256 != first.candidate.candidate_sha256


@pytest.mark.parametrize("lighting", [
    {"mat_depth_mm": 25.0, "lights": [], "paper": {"whiteness": 0.9, "ink_density": 2.3, "profile": None}},
    {"mat_depth_mm": 0.0, "lights": [], "paper": {"whiteness": 0.2, "ink_density": 2.3, "profile": None}},
    {"depth_mm": 0.0, "lights": []},                     # the print look's mat
], ids=["too-deep", "paper-too-dark", "a-mat"])
def test_a_lighting_frame_lighting_refuses_is_refused_like_a_bad_calibration(lighting):
    with pytest.raises(ValueError):
        TrialState().apply(envelope(canonical(lighting)), COMPOSITION, 1000)
