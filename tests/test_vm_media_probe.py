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
        assert frame.id == result.frame_id
        assert frame.player_id == identity["player_id"] and frame.output_id == result.output_id
        assert frame.calibration_valid


def configuration_value():
    operator = Operator()
    operator.player["id"] = "p-" + "a" * 32
    operator.outputs[0]["player_id"] = operator.player["id"]
    operator.outputs[0]["observation"].update(width_px=0, height_px=0)
    return probe.configure(operator, player_id=operator.player["id"], epoch=1,
        captured_from=100, captured_until=101, now=lambda: 200).model_dump(mode="json")


def configured_host(value):
    player = EquipmentSessionObservation(player_id="p-" + "a" * 32,
        device_id="device-" + "b" * 64, authority_epoch=1, retired=False)
    harness = SimpleNamespace(report={"media": {}}, fixture_central=lambda: "central",
        run=lambda *args, **kwargs: json.dumps(dict(ok=True, value=value)).encode())
    media = ApplianceMedia(harness, "image")
    media.photo = {"captured": "1970-01-01T00:01:40Z"}
    return media, player


def test_host_journals_typed_authored_frame_and_unchanged_unknown_output():
    value = configuration_value()
    media, player = configured_host(value)
    media.configure(player)
    assert media.setup == value == media.harness.report["media"]["configuration"]
    assert value["authored_frame"] == probe.VM_PHOTO_FRAME.model_dump(mode="json")
    assert value["observed_output"]["width_px"] == value["observed_output"]["height_px"] == 0


@pytest.mark.parametrize("field", list(probe.MediaConfigurationReceipt.model_fields))
def test_host_requires_every_configuration_receipt_field(field):
    value = configuration_value()
    del value[field]
    media, player = configured_host(value)
    with pytest.raises(FixtureError, match="media_configuration_invalid"):
        media.configure(player)
    assert media.setup is None and "configuration" not in media.harness.report["media"]


@pytest.mark.parametrize("fault", ["schema_bool", "kind", "extra", "epoch_bool", "epoch_string",
    "player", "epoch", "output_identity", "disconnected", "profile_width", "profile_video",
    "frame_geometry", "missing_nested", "missing_profile", "output_width_string", "output_bool",
    "starts_nan"])
def test_host_rejects_configuration_authority_or_receipt_drift_before_use(fault):
    value = configuration_value()
    if fault == "schema_bool":
        value["schema_version"] = True
    elif fault == "kind":
        value["kind"] = "another-scenario"
    elif fault == "extra":
        value["token"] = "private-token"
    elif fault == "epoch_bool":
        value["authority_epoch"] = True
    elif fault == "epoch_string":
        value["authority_epoch"] = "1"
    elif fault == "player":
        value["player_id"] = "p-" + "f" * 32
    elif fault == "epoch":
        value["authority_epoch"] = 2
    elif fault == "output_identity":
        value["observed_output"]["output_id"] = "Virtual-2"
    elif fault == "disconnected":
        value["observed_output"]["connected"] = False
    elif fault == "profile_width":
        value["authored_frame"]["profile"]["width_px"] = 1280
    elif fault == "profile_video":
        value["authored_frame"]["profile"]["video"] = 0
    elif fault == "frame_geometry":
        value["authored_frame"]["width_mm"] = 501
    elif fault == "missing_nested":
        del value["observed_output"]["connected"]
    elif fault == "missing_profile":
        del value["authored_frame"]["profile"]["video"]
    elif fault == "output_width_string":
        value["observed_output"]["width_px"] = "0"
    elif fault == "output_bool":
        value["observed_output"]["connected"] = 1
    else:
        value["starts_at"] = float("nan")
    media, player = configured_host(value)
    with pytest.raises(FixtureError, match="media_configuration_(invalid|authority)"):
        media.configure(player)
    assert media.setup is None and "configuration" not in media.harness.report["media"]
    assert "private-token" not in json.dumps(media.harness.report)


def test_discovered_unknown_output_configures_real_authenticated_central_and_host(
        registry, tmp_path, monkeypatch, capsys):
    import base64
    import uuid

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from fastapi.testclient import TestClient
    from test_registry import ADMIN

    from central.app import create_app
    from central.installation_models import InstallationInventory
    from contracts.enrollment import enrollment_message
    from player.output_discovery import discover_outputs

    for number, status in ((1, "connected"), (2, "disconnected")):
        connector = tmp_path / f"card0-Virtual-{number}"
        connector.mkdir()
        (connector / "status").write_text(status + "\n")
        (connector / "modes").write_text("3840x2160\n1280x720\n")
    discovery = discover_outputs(tmp_path)
    assert discovery.fault is None and len(discovery.outputs) == 2
    assert all(output.width_px == output.height_px == 0 for output in discovery.outputs)
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False,
                     release_authority=registry.release_authority)
    with TestClient(app) as client:
        device_id = "device-" + "d" * 64
        boot_id = str(uuid.uuid4())
        boot = client.post("/v1/bootstrap/boot", json=dict(
            device_id=device_id, boot_id=boot_id, request_id="a" * 48))
        assert boot.status_code == 200
        ticket = boot.json()
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes_raw().hex()
        nonce = client.post("/v1/enrollment/challenge", json=dict(public_key=public)).json()["nonce"]
        enrolled = client.post("/v1/enrollment/register", json=dict(public_key=public, nonce=nonce,
            outputs=[output.model_dump(mode="json") for output in discovery.outputs],
            device_id=device_id, boot_id=boot_id, ticket_id=ticket["ticket_id"],
            signature=base64.b64encode(key.sign(enrollment_message(
                nonce, discovery.outputs, device_id, boot_id, ticket["ticket_id"]))).decode()))
        assert enrolled.status_code == 200
        identity = enrolled.json()
        current = EquipmentSessionObservation(player_id=identity["player_id"], device_id=device_id,
            authority_epoch=identity["authority_epoch"], retired=False)
        client.headers["Authorization"] = "Bearer " + ADMIN
        before = InstallationInventory.model_validate(client.get("/v1/operator/inventory").json())
        operator = object.__new__(probe.Operator)
        operator.client = client
        operator.close = lambda: None  # The shared TestClient context owns its lifetime.
        monkeypatch.setattr(probe, "Operator", lambda: operator)
        serialized = []
        def execute(args, **kwargs):
            assert args[:6] == ["docker", "exec", "central", "python", "-m", "scripts.vm_media_probe"]
            capsys.readouterr()
            probe.main(args[6:])
            payload = capsys.readouterr().out.encode()
            serialized.append(payload)
            return payload
        harness = SimpleNamespace(report={"media": {}}, fixture_central=lambda: "central", run=execute)
        media = ApplianceMedia(harness, "worker-image")
        media.photo = {"captured": "1970-01-01T00:01:40Z"}
        media.configure(current)
        receipt = probe.decode_configuration(media.setup)
        after = InstallationInventory.model_validate(client.get("/v1/operator/inventory").json())
        assert before.outputs == after.outputs
        assert tuple(output.observation for output in after.outputs) == discovery.outputs
        assert receipt.observed_output == discovery.outputs[0]
        frame = after.frames[0]
        assert frame.id == receipt.frame_id == probe.VM_PHOTO_FRAME.id
        assert frame.profile == receipt.authored_frame.profile == probe.VM_PHOTO_FRAME.profile
        assert (frame.width_mm, frame.height_mm) == (500, 281.25)
        assert (frame.player_id, frame.output_id) == (current.player_id, "Virtual-1")
        assert frame.generation == 1 and frame.calibration_valid
        runtime = client.get("/v1/operator/runtime").json()
        scene = runtime["definitions"][probe.SCENE]
        program = runtime["programs"][probe.SCENE]
        assert scene["contributions"][0]["target"] == "frame:" + frame.id
        assert scene["contributions"][0]["source_refs"] == [probe.SOURCE]
        assert program["scene_id"] == probe.SCENE and program["starts_at"] == receipt.starts_at
        assert program["ends_at"] - program["starts_at"] == 7110
        assert probe.SOURCE in json.dumps(client.get("/v1/operator/media").json()["sources"])
        assert harness.report["media"]["configuration"] == receipt.model_dump(mode="json")
        assert all(identity["token"].encode() not in payload and ADMIN.encode() not in payload
                   and ticket["ticket_id"].encode() not in payload for payload in serialized)
