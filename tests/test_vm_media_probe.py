"""The media probe preserves safe domain failures across successful transport."""

import json
from types import SimpleNamespace

import pytest
from test_appliance_media import Operator

from central.installation_models import EquipmentSessionObservation
from scripts import vm_media_probe as probe
from scripts.appliance_media import ApplianceMedia
from scripts.boot_fixture import FixtureError


@pytest.mark.parametrize("path,phase", [
    ("/v1/operator/inventory", "inventory"),
    ("/v1/operator/sources/vm-photo:1", "source"),
    ("/v1/operator/frames", "frame"),
    ("/v1/operator/frames/vm-photo-frame/binding", "binding"),
    ("/v1/operator/frames/vm-photo-frame/calibration", "calibration"),
    ("/v1/operator/scenes/vm-photo", "scene"),
    ("/v1/operator/programs/vm-photo", "program"),
])
def test_configuration_failures_identify_the_exact_operator_operation(path, phase):
    operator = Operator()
    request = operator.request

    def fail(method, selected, body=None):
        if selected == path:
            raise probe.ProbeError("operator_response_invalid")
        return request(method, selected, body)

    operator.request = fail
    with pytest.raises(probe.ProbeError) as caught:
        probe.configure(operator, player_id="p1", epoch=1, captured_from=100,
                        captured_until=101, now=lambda: 200)
    assert caught.value.failure.model_dump() == {
        "ok": False, "error": "operator_response_invalid", "phase": phase,
    }


def test_complete_installation_contract_is_validated_before_configuration_writes():
    operator = Operator()
    operator.outputs[0]["observation"]["width_px"] = "invalid-mode"
    with pytest.raises(probe.ProbeError) as caught:
        probe.configure(operator, player_id="p1", epoch=1, captured_from=100,
                        captured_until=101)
    assert caught.value.failure.error == "probe_contract_invalid"
    assert caught.value.failure.phase == "inventory"
    assert len(operator.calls) == 1


@pytest.mark.parametrize("unexpected", [False, True])
def test_cli_failure_is_a_bounded_envelope_without_secret_exception_text(
        monkeypatch, capsys, unexpected):
    operator = Operator()
    request = operator.request
    closed = []
    operator.close = lambda: closed.append(True)

    def fail(method, selected, body=None):
        if selected.endswith("/binding"):
            if unexpected:
                raise RuntimeError("Authorization: Bearer private-test-token")
            raise probe.ProbeError("operator_response_invalid")
        return request(method, selected, body)

    operator.request = fail
    monkeypatch.setattr(probe, "Operator", lambda: operator)
    assert probe.main(["configure", "--player-id", "p1", "--epoch", "1",
                       "--captured-from", "100", "--captured-until", "101"]) is None
    output = capsys.readouterr()
    assert output.err == "" and len(output.out) < 160
    assert "private-test-token" not in output.out
    envelope = probe.decode_result(output.out.encode())
    assert envelope == probe.ProbeFailure(
        error="probe_internal" if unexpected else "operator_response_invalid", phase="binding")
    assert closed == [True]


def test_cli_success_wraps_the_configuration_result(monkeypatch, capsys):
    operator = Operator()
    operator.close = lambda: None
    monkeypatch.setattr(probe, "Operator", lambda: operator)
    probe.main(["configure", "--player-id", "p1", "--epoch", "1",
                "--captured-from", "100", "--captured-until", "101"])
    result = probe.decode_result(capsys.readouterr().out.encode())
    assert isinstance(result, probe.ProbeSuccess)
    assert result.value["player_id"] == "p1" and result.value["authority_epoch"] == 1


@pytest.mark.parametrize("payload", [
    {}, {"presentations": [], "grants": []},
    {"ok": 1, "value": {}}, {"ok": True, "value": [], "error": "probe_internal"},
    {"ok": False, "error": "private-test-token", "phase": "binding"},
    {"ok": False, "error": "probe_internal", "phase": "private-test-token"},
    {"ok": False, "error": "probe_internal", "phase": "binding", "secret": "value"},
])
def test_probe_rejects_untyped_or_unbounded_failure_fields(payload):
    with pytest.raises(ValueError):
        probe.decode_result(json.dumps(payload).encode())


@pytest.mark.parametrize("invalid", [False, True])
def test_host_preserves_probe_failure_phase_without_accepting_unknown_codes(invalid):
    player = EquipmentSessionObservation(player_id="p-" + "a" * 32,
        device_id="device-" + "b" * 64, authority_epoch=1, retired=False)
    envelope = {"ok": False, "error": "operator_response_invalid", "phase": "binding"}
    if invalid:
        envelope["error"] = "Authorization: Bearer private-test-token"
    harness = SimpleNamespace(report={}, fixture_central=lambda: "central",
        run=lambda *args, **kwargs: json.dumps(envelope).encode())
    error = "media_probe_contract_invalid" if invalid else "operator_response_invalid"
    with pytest.raises(FixtureError, match=error):
        ApplianceMedia(harness, "image").probe("configure", player)
    if invalid:
        assert "private-test-token" not in json.dumps(harness.report)
    else:
        assert harness.report["media_probe"] == dict(action="configure", **envelope)


def test_media_configuration_against_real_central_does_not_need_local_health(registry):
    from fastapi.testclient import TestClient
    from test_registry import ADMIN, enroll

    from central.app import create_app

    identity, _, _ = enroll(registry, count=1)
    assert "persistence" not in registry.inventory().players[0].health
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False,
                     release_authority=registry.release_authority)
    with TestClient(app) as client:
        operator = object.__new__(probe.Operator)
        operator.client = client
        client.headers["Authorization"] = "Bearer " + ADMIN
        result = probe.configure(operator, player_id=identity["player_id"],
            epoch=identity["authority_epoch"], captured_from=100, captured_until=101,
            now=registry.clock.utc)
        frame = registry.inventory().frames[0]
        assert frame.id == result["frame_id"]
        assert frame.player_id == identity["player_id"] and frame.output_id == result["output_id"]
        assert frame.calibration_valid
