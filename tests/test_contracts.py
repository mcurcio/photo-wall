import math

import pytest
from pydantic import ValidationError

from contracts.models import Calibration, Layer, OutputBinding, Plan, Readiness, Variant
from contracts.time import ManualClock, TimeMapping


def test_local_monotonic_mapping_invalidates_step_staleness_and_bad_clock():
    clock = ManualClock(100)
    mapping = TimeMapping(clock)
    assert not mapping.healthy()
    mapping.establish(.01)
    assert mapping.deadline(110) == 10
    clock.advance(3)
    assert mapping.deadline(110) == 10
    clock.step_utc(1)
    assert not mapping.healthy()
    mapping.establish(.01)
    clock.advance(31)
    assert not mapping.healthy()
    mapping.establish(.01)
    clock.wall = math.nan
    assert not mapping.healthy()
    with pytest.raises(ValueError):
        mapping.establish(.01)


def test_clock_uncertainty_is_not_pipeline_position():
    clock = ManualClock(1_000)
    mapping = TimeMapping(clock)
    mapping.establish(.5)
    assert not mapping.healthy()
    mapping.establish(.05)
    clock.advance(20)  # A paused pipeline cannot stop this clock.
    layer = Layer(assignment_id="a", run_id="run", output_id="hdmi-1", frame_id="f",
                  binding_generation=1, start=1000, end=1100, media_origin=988,
                  variant=Variant(sha256="a" * 64, size=20, media_type="video/mp4",
                                  width=1920, height=1080, duration=60))
    assert layer.position(clock.utc()) == 32


@pytest.mark.parametrize("corners", [((0, 0), (1, 1), (1, 0), (0, 1)),
                                     ((0, 0), (0, 0), (1, 1), (0, 1)),
                                     ((0, 0), (2, 0), (1, 1), (0, 1))])
def test_calibration_rejects_folded_or_invalid_aperture(corners):
    with pytest.raises(ValidationError):
        Calibration(corners=corners)


def test_wire_rejects_upstream_fields_and_failure_text():
    with pytest.raises(ValidationError):
        Variant(sha256="a" * 64, size=1, media_type="image/jpeg", width=1, height=1,
                url="https://upstream.invalid/secret")
    common = dict(plan_id="p", revision=1, authority_epoch=1, sequence=1,
                  clock_uncertainty=.01, observed_at=100)
    with pytest.raises(ValidationError):
        Readiness(**common, failures=[{"assignment_id": "https://upstream.invalid/token", "code": "download"}])
    with pytest.raises(ValidationError):
        Readiness(**common, failures=[{"assignment_id": "a", "code": "API token was rejected"}])


def test_readiness_distinguishes_file_from_preparation_and_is_deeply_immutable():
    common = dict(plan_id="p", revision=1, authority_epoch=1, sequence=1,
                  clock_uncertainty=.01, observed_at=100, secured=("a",))
    acquired_but_failed = Readiness(**common, failures=[{"assignment_id": "a", "code": "decode"}])
    assert acquired_but_failed.prepared == ()
    with pytest.raises(ValidationError):
        acquired_but_failed.failures[0].code = "arbitrary"
    with pytest.raises(ValidationError):
        Readiness(**common, prepared=("missing",))
    with pytest.raises(ValidationError):
        Readiness(**common, prepared=("a",), failures=[{"assignment_id": "a", "code": "decode"}])


def test_plan_rejects_old_binding_generation_and_outside_validity():
    binding = OutputBinding(output_id="out", frame_id="frame", generation=2,
                            profile=dict(width_px=1080, height_px=1920, diagonal_inches=24))
    common = dict(plan_id="p", revision=1, player_id="player", authority_epoch=1,
                  issued_at=100, valid_from=100, valid_until=400, bindings=(binding,))
    layer = Layer(assignment_id="a", run_id="run", output_id="out", frame_id="frame",
                  binding_generation=1, start=100, end=110, media_origin=100,
                  presentation="black")
    with pytest.raises(ValidationError):
        Plan(**common, layers=(layer,))
    layer = layer.model_copy(update={"binding_generation": 2, "end": 401})
    with pytest.raises(ValidationError):
        Plan(**common, layers=(layer,))
