"""Position's trial carries the Frame's draft lighting to the Player (decision 0028; it replaced the
print look's mat): the applied trial holds it, a bad one is refused like a bad calibration. No
database: the envelope is built by hand with contracts.node_calibration."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from contracts.frame_lighting import AT_FULL, CloseLight, FrameLighting, Paper, WashLight
from contracts.models import Calibration, OutputBinding, PositionBox
from contracts.node_calibration import TrialCandidate, candidate_hash, canonical, encode_trial
from contracts.node_display import Surface
from contracts.node_protocol import NodeProcessIdentity, OutputKey
from player.calibration_trial import TrialState
from player.rendering import OutputComposition

LIGHTING = FrameLighting(mat_depth_mm=0, lights=(WashLight(light="room", lux=1000, reference=AT_FULL),),
                         paper=Paper(whiteness=0.9, ink_density=3.5))
BINDING = OutputBinding(output_id="HDMI-A-1", frame_id="frame", generation=3, configuration_revision=2,
                        lighting=LIGHTING, display_mm=(600, 340))
COMPOSITION = OutputComposition(BINDING)
DRAFT_FIT = PositionBox(left=0.15, top=0.25, width=0.5, height=0.5)    # a drafted Position unlike the binding's
BASELINE = Surface(OutputKey(uuid4(), uuid4(), "HDMI-A-1", 1, 1), NodeProcessIdentity(100, 5, uuid4()),
                   1, BINDING.generation, BINDING.configuration_revision, "frame")
SPOT = CloseLight(light="bookshelf-spot", lux=400, reference=AT_FULL, across_mm=0, up_mm=350, out_mm=250,
                  inner_beam_degrees=20, outer_beam_degrees=40)


def envelope(lighting: FrameLighting | dict, sequence: int = 1, trial_id=None) -> str:
    """A trial envelope carrying `lighting`; a dict rides as given, in place of a valid lighting."""
    trial_id = trial_id or uuid4()
    calibration = canonical(Calibration(fit=DRAFT_FIT).model_dump(mode="json"))
    valid = lighting if isinstance(lighting, FrameLighting) else LIGHTING
    raw = encode_trial(TrialCandidate(
        trial_id=trial_id, generation=1, sequence=sequence, baseline=BASELINE,
        calibration_json=calibration,
        candidate_sha256=candidate_hash(trial_id, 1, sequence, BASELINE, calibration, valid),
        expires_boottime_ms=5000, hard_expires_boottime_ms=9000, lighting=valid))
    if isinstance(lighting, FrameLighting):
        return raw
    return canonical({**json.loads(raw), "lighting": lighting})


def test_the_applied_trial_holds_the_drafts_lighting():
    """Mutation probe: apply the binding's lighting whatever the envelope carries -> the draft is lost."""
    drafted = LIGHTING.model_copy(update={"mat_depth_mm": 2.5, "lights": (*LIGHTING.lights, SPOT)})
    applied = TrialState().apply(envelope(drafted), COMPOSITION, 1000)
    assert applied.lighting == drafted
    assert applied.calibration.fit == DRAFT_FIT


def test_an_edit_that_moves_only_the_lights_is_a_new_candidate():
    """The candidate's hash covers the lighting, so an edit of the lights alone is a change to draw."""
    state, trial_id = TrialState(), uuid4()
    first = state.apply(envelope(LIGHTING, 1, trial_id), COMPOSITION, 1000)
    drafted = LIGHTING.model_copy(update={"lights": (SPOT,)})
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
        TrialState().apply(envelope(lighting), COMPOSITION, 1000)
