"""Position's trial carries the Frame's draft mat to the Player (print look, 2026-10-12): the
applied trial holds it, a bad one is refused like a bad calibration. No database: the envelope is
built by hand with contracts.node_calibration."""
from __future__ import annotations

from uuid import uuid4

import pytest

from contracts.models import Calibration, CoverFit, FrameProfile, Light, Mat, OutputBinding
from contracts.node_calibration import (
    NO_MAT_JSON,
    TrialCandidate,
    candidate_hash,
    canonical,
    encode_trial,
)
from contracts.node_display import Surface
from contracts.node_protocol import NodeProcessIdentity, OutputKey
from player.calibration_trial import TrialState
from player.rendering import OutputComposition

BINDING = OutputBinding(output_id="HDMI-A-1", frame_id="frame", generation=3, configuration_revision=2,
                        profile=FrameProfile(width_px=1920, height_px=1080, diagonal_inches=27), mat=Mat())
COMPOSITION = OutputComposition(BINDING)
BASELINE = Surface(OutputKey(uuid4(), uuid4(), "HDMI-A-1", 1, 1), NodeProcessIdentity(100, 5, uuid4()),
                   1, BINDING.generation, BINDING.configuration_revision, "frame")
TOP_LIGHT = Light(from_degrees=0, steep_degrees=45, softness_degrees=0, strength_tenths=10, colour_kelvin=6500)


def envelope(mat_json: str, sequence: int = 1, trial_id=None) -> str:
    trial_id = trial_id or uuid4()
    calibration = canonical(Calibration(fit=CoverFit(rotation=7)).model_dump(mode="json"))
    return encode_trial(TrialCandidate(
        trial_id=trial_id, generation=1, sequence=sequence, baseline=BASELINE,
        calibration_json=calibration,
        candidate_sha256=candidate_hash(trial_id, 1, sequence, BASELINE, calibration, mat_json),
        expires_boottime_ms=5000, hard_expires_boottime_ms=9000, mat_json=mat_json))


def test_the_applied_trial_holds_the_drafts_mat():
    """Mutation probe: apply Mat() whatever the envelope carries -> the drafted mat is lost."""
    drafted = Mat(depth_mm=2.5, lights=(TOP_LIGHT,))
    applied = TrialState().apply(envelope(canonical(drafted.model_dump(mode="json"))), COMPOSITION, 1000)
    assert applied.mat == drafted
    assert applied.calibration.fit == CoverFit(rotation=7)
    assert TrialState().apply(envelope(NO_MAT_JSON), COMPOSITION, 1000).mat == Mat()


def test_an_edit_that_moves_only_the_mat_is_a_new_candidate():
    """The candidate's hash covers the mat, so an edit of the lights alone is a change to draw."""
    state, trial_id = TrialState(), uuid4()
    first = state.apply(envelope(NO_MAT_JSON, 1, trial_id), COMPOSITION, 1000)
    drafted = canonical(Mat(depth_mm=1, lights=(TOP_LIGHT,)).model_dump(mode="json"))
    second = state.apply(envelope(drafted, 2, trial_id), COMPOSITION, 1100)
    assert second.mat.depth_mm == 1 and second.candidate.candidate_sha256 != first.candidate.candidate_sha256


def test_a_mat_out_of_range_is_refused_like_a_bad_calibration():
    deep = canonical({"depth_mm": 25.0, "lights": []})
    with pytest.raises(ValueError):
        TrialState().apply(envelope(deep), COMPOSITION, 1000)
