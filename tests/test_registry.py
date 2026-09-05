"""Actual PostgreSQL integration, isolated in a fresh schema for every test."""

import base64
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from central.app import create_app
from central.db import Database
from central.registry import (
    Enrollment,
    FrameCreate,
    OutputReport,
    Registry,
    RegistryError,
    enrollment_message,
)
from contracts.models import Calibration, FrameProfile

ADMIN = "test-operator-" + "x" * 40


def enroll(registry, key=None, count=2, persistence="durable"):
    key = key or Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw().hex()
    nonce = registry.challenge(public)["nonce"]
    outputs = tuple(OutputReport(output_id=f"HDMI-A-{i+1}", width_px=1920, height_px=1080)
                    for i in range(count))
    request = Enrollment(public_key=public, nonce=nonce, outputs=outputs, persistence=persistence,
                         signature=base64.b64encode(key.sign(enrollment_message(
                             nonce, outputs, persistence))).decode())
    return registry.enroll(request), key, request


def frame(registry, frame_id="portrait"):
    registry.create_frame(FrameCreate(id=frame_id, width_mm=300, height_mm=500,
                                     profile=FrameProfile(width_px=1080, height_px=1920,
                                                          diagonal_inches=24)))


def test_registration_precedes_binding_proof_replay_and_token_rotation(registry):
    identity, key, request = enroll(registry)
    assert registry.inventory()["frames"] == []
    assert len(registry.inventory()["outputs"]) == 2
    assert registry.bindings_for(identity["player_id"], identity["authority_epoch"]) == []
    assert registry.authenticate(identity["token"])["id"] == identity["player_id"]
    with pytest.raises(RegistryError, match="used_challenge"):
        registry.enroll(request)
    rotated, _, _ = enroll(registry, key)
    assert rotated["player_id"] == identity["player_id"]
    assert rotated["authority_epoch"] == identity["authority_epoch"] + 1
    with pytest.raises(RegistryError, match="unauthorized"):
        registry.authenticate(identity["token"])


def test_registration_without_panels_and_observation_removal(registry):
    identity, key, _ = enroll(registry, count=0)
    assert len(registry.inventory()["players"]) == 1
    assert registry.inventory()["outputs"] == []
    enroll(registry, key, count=2)
    enroll(registry, key, count=1)
    observations = {o["output_id"]: o["observation"] for o in registry.inventory()["outputs"]}
    assert observations["HDMI-A-1"]["connected"] is True
    assert observations["HDMI-A-2"]["connected"] is False


def test_volatile_player_appears_with_storage_fault_but_cannot_be_bound(registry):
    identity, _, _ = enroll(registry, persistence="volatile")
    inventory = registry.inventory()
    assert inventory["players"][0]["health"]["storage_fault"] is True
    assert len(inventory["outputs"]) == 2
    frame(registry)
    with pytest.raises(RegistryError, match="persistent_storage_required"):
        registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    assert registry.configuration_for(identity["player_id"], 1)["execution_bindings"] == []


def test_loss_of_persistence_disables_existing_binding_and_signed_flag_cannot_be_forged(registry):
    identity, key, request = enroll(registry)
    frame(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    registry.calibrate("portrait", "commit", 1, Calibration(), expected_generation=1)
    with pytest.raises(RegistryError, match="invalid_proof"):
        registry.enroll(request.model_copy(update={"persistence": "volatile"}))
    identity, _, _ = enroll(registry, key, persistence="volatile")
    config = registry.configuration_for(identity["player_id"], identity["authority_epoch"])
    assert len(config["bindings"]) == 1
    assert config["execution_bindings"] == []
    identity, _, _ = enroll(registry, key)
    config = registry.configuration_for(identity["player_id"], identity["authority_epoch"])
    assert len(config["execution_bindings"]) == 1


def test_expired_challenge_and_invalid_proof_do_not_create_player(registry):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw().hex()
    nonce = registry.challenge(public)["nonce"]
    outputs = (OutputReport(output_id="HDMI-A-1", width_px=1920, height_px=1080),)
    request = Enrollment(public_key=public, nonce=nonce, outputs=outputs,
                         signature=base64.b64encode(key.sign(enrollment_message(nonce, outputs))).decode())
    bad = request.model_copy(update={"signature": base64.b64encode(b"x" * 64).decode()})
    with pytest.raises(RegistryError, match="invalid_proof"):
        registry.enroll(bad)
    registry.clock.advance(61)
    with pytest.raises(RegistryError, match="expired"):
        registry.enroll(request)
    assert registry.inventory()["players"] == []


def test_replacement_preserves_frame_rejects_retired_identity_and_revalidates(registry):
    old, key, _ = enroll(registry)
    new, _, _ = enroll(registry)
    frame(registry)
    first = registry.bind("portrait", old["player_id"], "HDMI-A-1", expected_generation=0)
    assert first["generation"] == 1
    registry.calibrate("portrait", "commit", 1, Calibration(gain=.8), expected_generation=1)
    assert len(registry.bindings_for(old["player_id"], 1)) == 1
    registry.retire(old["player_id"])
    with pytest.raises(RegistryError, match="unauthorized"):
        registry.authenticate(old["token"])
    with pytest.raises(RegistryError, match="retired"):
        registry.challenge(key.public_key().public_bytes_raw().hex())
    assert registry.bind("portrait", new["player_id"], "HDMI-A-2", expected_generation=2)["generation"] == 3
    assert registry.bindings_for(new["player_id"], 1) == []
    location = registry.inventory()["frames"][0]
    assert (location["id"], location["width_mm"], location["calibration"]["gain"]) == ("portrait", 300, .8)
    with pytest.raises(RegistryError, match="generation_conflict"):
        registry.calibrate("portrait", "commit", 2, Calibration(gain=.5), expected_generation=1)
    registry.calibrate("portrait", "commit", 2, Calibration(gain=.9), expected_generation=3)
    assert registry.bindings_for(new["player_id"], 1)[0].generation == 3


def test_two_outputs_and_concurrent_conflicting_binding_are_atomic(registry):
    identity, _, _ = enroll(registry)
    frame(registry, "left")
    frame(registry, "right")

    def claim(frame_id):
        try:
            registry.bind(frame_id, identity["player_id"], "HDMI-A-1", expected_generation=0)
            return "ok"
        except RegistryError as exc:
            return exc.code

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(claim, ["left", "right"]))
    assert sorted(outcomes) == ["ok", "output_already_bound"]
    frames = registry.inventory()["frames"]
    unbound = next(f for f in frames if f["player_id"] is None)
    registry.bind(unbound["id"], identity["player_id"], "HDMI-A-2", expected_generation=0)
    assert len(registry.bindings_for(identity["player_id"], 1, include_unvalidated=True)) == 2
    assert all(f["generation"] == 1 for f in registry.inventory()["frames"])


def test_delayed_binding_retry_cannot_undo_a_newer_transfer(registry):
    first, _, _ = enroll(registry)
    second, _, _ = enroll(registry)
    frame(registry)
    registry.bind("portrait", first["player_id"], "HDMI-A-1", expected_generation=0)
    duplicate = registry.bind("portrait", first["player_id"], "HDMI-A-1", expected_generation=0)
    assert duplicate == {"generation": 1, "changed": False}
    registry.bind("portrait", second["player_id"], "HDMI-A-1", expected_generation=1)
    with pytest.raises(RegistryError, match="generation_conflict"):
        registry.bind("portrait", first["player_id"], "HDMI-A-1", expected_generation=0)
    assert registry.inventory()["frames"][0]["player_id"] == second["player_id"]


def test_calibration_preview_expiry_revert_commit_conflict_and_restart(registry):
    identity, _, _ = enroll(registry)
    frame(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    registry.calibrate("portrait", "commit", 1, Calibration(), expected_generation=1)
    registry.calibrate("portrait", "preview", 2, Calibration(gain=.5), expected_generation=1)
    preview = registry.bindings_for(identity["player_id"], 1)[0]
    assert preview.calibration.gain == 1
    assert preview.effective_calibration(registry.clock.utc()).gain == .5
    registry.clock.advance(31)
    # The downloaded configuration contains everything needed to revert offline.
    assert preview.effective_calibration(registry.clock.utc()).gain == 1
    expired = registry.bindings_for(identity["player_id"], 1)[0]
    assert expired.calibration.gain == 1
    assert expired.configuration_revision > preview.configuration_revision
    registry.calibrate("portrait", "preview", 2, Calibration(gain=.7), expected_generation=1)
    registry.calibrate("portrait", "revert", 2, expected_generation=1)
    assert registry.bindings_for(identity["player_id"], 1)[0].calibration.gain == 1
    registry.calibrate("portrait", "commit", 2, Calibration(gain=.8), expected_generation=1)
    restarted = Registry(Database(registry.db.dsn), registry.clock)
    restarted.db.migrate()  # Idempotent migration on a fresh connection.
    assert restarted.bindings_for(identity["player_id"], 1)[0].calibration.revision == 3
    with pytest.raises(RegistryError, match="revision_conflict"):
        restarted.calibrate("portrait", "commit", 2, Calibration(gain=.2), expected_generation=1)


def test_http_operator_and_player_authority_are_separate_and_errors_are_sanitized(registry):
    with TestClient(create_app(registry.db, registry.clock, ADMIN)) as client:
        assert client.get("/healthz").json()["database"] is True
        assert client.get("/").status_code == 200
        assert client.get("/v1/operator/inventory").status_code == 401
        identity, _, _ = enroll(registry)
        player_headers = {"Authorization": "Bearer " + identity["token"]}
        assert client.get("/v1/operator/inventory", headers=player_headers).status_code == 401
        assert client.get("/v1/player/config", headers=player_headers).json()["bindings"] == []
        assert client.get("/v1/player/config", headers={"Authorization": "Bearer " + ADMIN}).status_code == 401
        response = client.post("/v1/enrollment/challenge", json={"public_key": "private-test-secret"})
        assert response.status_code == 422
        assert "private-test-secret" not in response.text
        data = client.get("/v1/operator/inventory", headers={"Authorization": "Bearer " + ADMIN}).text
        assert identity["token"] not in data and "token_hash" not in data and "public_key" not in data
