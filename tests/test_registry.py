"""Actual PostgreSQL integration, isolated in a fresh schema for every test."""

import base64
import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from central.app import create_app
from central.db import Database
from central.installation_models import InstallationInventory
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


def enroll(registry, key=None, count=2, device_id=None):
    # Ticketless enroll (0009): the signed-rootfs boot-ticket path is retired,
    # so no boot server ever issues a ticket -- ticket_id is always None.
    key = key or Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw().hex()
    device_id = device_id or "device-" + hashlib.sha256(bytes.fromhex(public)).hexdigest()
    boot_id = str(uuid.uuid4())
    nonce = registry.challenge(public)["nonce"]
    outputs = tuple(OutputReport(output_id=f"HDMI-A-{i+1}", width_px=1920, height_px=1080)
                    for i in range(count))
    request = Enrollment(public_key=public, nonce=nonce, outputs=outputs,
                         device_id=device_id, boot_id=boot_id, ticket_id=None,
                         signature=base64.b64encode(key.sign(enrollment_message(
                             nonce, outputs, device_id, boot_id, None))).decode())
    return registry.enroll(request), key, request


def frame(registry, frame_id="portrait"):
    registry.create_frame(FrameCreate(id=frame_id, width_mm=300, height_mm=500,
                                     profile=FrameProfile(width_px=1080, height_px=1920,
                                                          diagonal_inches=24)))


def enroll_d0(registry, key=None, count=2, device_id=None):
    """m3-central-d0-enroll: a flashed/ticketless enroll (0008 baseline) --
    unlike `enroll` above, this NEVER calls `select_boot`, so no
    `appliance_devices` row exists for the device; `ticket_id=None` is the
    explicit D0 signal (contracts/enrollment.py)."""
    key = key or Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw().hex()
    device_id = device_id or "device-" + hashlib.sha256(bytes.fromhex(public)).hexdigest()
    boot_id = str(uuid.uuid4())
    nonce = registry.challenge(public)["nonce"]
    outputs = tuple(OutputReport(output_id=f"HDMI-A-{i+1}", width_px=1920, height_px=1080)
                    for i in range(count))
    request = Enrollment(public_key=public, nonce=nonce, outputs=outputs,
                         device_id=device_id, boot_id=boot_id, ticket_id=None,
                         signature=base64.b64encode(key.sign(enrollment_message(
                             nonce, outputs, device_id, boot_id, None))).decode())
    return registry.enroll(request), key, request


def test_d0_ticketless_enroll_succeeds_and_creates_an_unbound_pending_record(registry):
    identity, _, request = enroll_d0(registry)
    assert request.ticket_id is None
    assert identity["token"] and identity["player_id"] and identity["authority_epoch"] == 1
    by_id = {p.id: p for p in registry.inventory().players}
    assert by_id[identity["player_id"]].device_id == request.device_id
    assert by_id[identity["player_id"]].is_bound is False
    assert by_id[identity["player_id"]].retired_at is None
    pending_ids = [p.id for p in registry.inventory().players if p.retired_at is None and not p.is_bound]
    assert pending_ids == [identity["player_id"]]


def test_d0_reboot_reassociates_by_serial_preserving_frame_binding_no_duplicate(registry):
    identity, _, request = enroll_d0(registry)
    frame(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    registry.calibrate("portrait", "commit", 1, Calibration(), expected_generation=1)
    rebooted, _, _ = enroll_d0(registry, device_id=request.device_id)
    assert rebooted["player_id"] == identity["player_id"]
    assert rebooted["authority_epoch"] == identity["authority_epoch"] + 1
    inventory = registry.inventory()
    assert len(inventory.players) == 1
    assert {p.id: p for p in inventory.players}[identity["player_id"]].is_bound is True
    assert len(registry.configuration_for(rebooted["player_id"], rebooted["authority_epoch"])
               ["execution_bindings"]) == 1


def test_d0_unbound_player_gets_no_execution_bindings_until_operator_binds(registry):
    # A Frame already exists at enroll time, so the D0 branch has something
    # it COULD (wrongly) auto-bind to -- the assertions below must catch that.
    # Cross-player: a SECOND D0 player is bound to the Frame with a real,
    # committed (calibration_valid) execution binding. Without this second
    # player, an empty result is indistinguishable from a broken
    # `configuration_in` scope (central/registry.py `WHERE b.player_id=%s`)
    # that returns nothing for ANY player -- this bites that leak instead.
    frame(registry)
    identity, _, _ = enroll_d0(registry)
    bound_identity, _, _ = enroll_d0(registry)
    registry.bind("portrait", bound_identity["player_id"], "HDMI-A-1", expected_generation=0)
    registry.calibrate("portrait", "commit", 1, Calibration(), expected_generation=1)
    bound_config = registry.configuration_for(bound_identity["player_id"], bound_identity["authority_epoch"])
    assert len(bound_config["execution_bindings"]) == 1
    assert registry.bindings_for(identity["player_id"], identity["authority_epoch"]) == []
    config = registry.configuration_for(identity["player_id"], identity["authority_epoch"])
    assert config["bindings"] == [] and config["execution_bindings"] == []


def test_d0_retired_serial_is_refused_reenrollment(registry):
    identity, _, request = enroll_d0(registry)
    registry.retire(identity["player_id"])
    with pytest.raises(RegistryError, match="retired"):
        enroll_d0(registry, device_id=request.device_id)


def test_ticketless_enroll_succeeds_with_no_release_authority_configured(registry):
    """0009 migration edit A: the enroll<->release-authority guard
    (central/registry.py) now lives INSIDE the `ticket_id is not None`
    (ticketed) branch, not ahead of it -- a ticketless enroll (the diskless
    base's fleet-brick path) must succeed even when central has no release
    authority configured at all, never dereferencing `self.release_authority`."""
    unauthorized = Registry(Database(registry.db.dsn), registry.clock)
    identity, _, request = enroll_d0(unauthorized)
    assert request.ticket_id is None
    assert identity["token"] and identity["player_id"] and identity["authority_epoch"] == 1
    by_id = {p.id: p for p in unauthorized.inventory().players}
    assert by_id[identity["player_id"]].device_id == request.device_id
    assert by_id[identity["player_id"]].is_bound is False
    assert by_id[identity["player_id"]].retired_at is None


def test_d0_enroll_never_touches_appliance_devices(registry):
    """Distinguishes the D0 branch from netboot: no boot-ticket bookkeeping
    row is created for a serial that never went through select_boot."""
    identity, _, request = enroll_d0(registry)
    with registry.db.transaction() as conn:
        row = conn.execute("SELECT 1 FROM appliance_devices WHERE device_id=%s",
                           (request.device_id,)).fetchone()
    assert row is None
    assert identity["player_id"]


def test_registration_precedes_binding_proof_replay_and_token_rotation(registry):
    identity, key, request = enroll(registry)
    assert registry.inventory().frames == ()
    assert len(registry.inventory().outputs) == 2
    assert registry.bindings_for(identity["player_id"], identity["authority_epoch"]) == []
    assert registry.authenticate(identity["token"])["id"] == identity["player_id"]
    with pytest.raises(RegistryError, match="used_challenge"):
        registry.enroll(request)
    rotated, _, _ = enroll(registry, device_id=request.device_id)
    assert rotated["player_id"] == identity["player_id"]
    assert rotated["authority_epoch"] == identity["authority_epoch"] + 1
    with pytest.raises(RegistryError, match="unauthorized"):
        registry.authenticate(identity["token"])


def test_registration_without_panels_and_observation_removal(registry):
    identity, _, first = enroll(registry, count=0)
    assert len(registry.inventory().players) == 1
    assert registry.inventory().outputs == ()
    enroll(registry, count=2, device_id=first.device_id)
    enroll(registry, count=1, device_id=first.device_id)
    observations = {o.output_id: o.observation for o in registry.inventory().outputs}
    assert observations["HDMI-A-1"].connected is True
    assert observations["HDMI-A-2"].connected is False


def test_stateless_player_can_be_bound_and_recovers_binding_with_a_fresh_key(registry):
    identity, _, first = enroll(registry)
    inventory = registry.inventory()
    assert inventory.players[0].device_id == first.device_id
    assert inventory.players[0].health["boot_id"] == first.boot_id
    assert len(inventory.outputs) == 2
    frame(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    registry.calibrate("portrait", "commit", 1, Calibration(), expected_generation=1)
    restarted, _, _ = enroll(registry, device_id=first.device_id)
    assert restarted["player_id"] == identity["player_id"]
    assert restarted["authority_epoch"] == 2
    assert len(registry.configuration_for(restarted["player_id"], 2)["execution_bindings"]) == 1


def test_boot_context_is_signed_and_stale_ticket_cannot_reenroll(registry):
    identity, _, request = enroll(registry)
    frame(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    registry.calibrate("portrait", "commit", 1, Calibration(), expected_generation=1)
    forged = request.model_copy(update={"boot_id": str(uuid.uuid4())})
    with pytest.raises(RegistryError, match="invalid_proof"):
        registry.enroll(forged)
    with pytest.raises(RegistryError, match="expired_or_used_challenge"):
        registry.enroll(request)


def test_expired_challenge_and_invalid_proof_do_not_create_player(registry):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw().hex()
    nonce = registry.challenge(public)["nonce"]
    outputs = (OutputReport(output_id="HDMI-A-1", width_px=1920, height_px=1080),)
    device_id = "device-" + hashlib.sha256(bytes.fromhex(public)).hexdigest()
    boot_id = str(uuid.uuid4())
    request = Enrollment(public_key=public, nonce=nonce, outputs=outputs,
                         device_id=device_id, boot_id=boot_id, ticket_id=None,
                         signature=base64.b64encode(key.sign(enrollment_message(
                             nonce, outputs, device_id, boot_id, None))).decode())
    bad = request.model_copy(update={"signature": base64.b64encode(b"x" * 64).decode()})
    with pytest.raises(RegistryError, match="invalid_proof"):
        registry.enroll(bad)
    registry.clock.advance(61)
    with pytest.raises(RegistryError, match="expired"):
        registry.enroll(request)
    assert registry.inventory().players == ()


def test_replacement_preserves_frame_rejects_retired_identity_and_revalidates(registry):
    old, _, old_enrollment = enroll(registry)
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
        enroll(registry, device_id=old_enrollment.device_id)
    assert registry.bind("portrait", new["player_id"], "HDMI-A-2", expected_generation=2)["generation"] == 3
    assert registry.bindings_for(new["player_id"], 1) == []
    location = registry.inventory().frames[0]
    assert (location.id, location.width_mm, location.calibration.gain) == ("portrait", 300, .8)
    with pytest.raises(RegistryError, match="generation_conflict"):
        registry.calibrate("portrait", "commit", 2, Calibration(gain=.5), expected_generation=1)
    registry.calibrate("portrait", "commit", 2, Calibration(gain=.9), expected_generation=3)
    assert registry.bindings_for(new["player_id"], 1)[0].generation == 3


def test_unbind_withdraws_execution_binding_generation_fenced_and_allows_rebind(registry):
    identity, _, _ = enroll(registry)
    frame(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    registry.calibrate("portrait", "commit", 1, Calibration(), expected_generation=1)
    assert len(registry.bindings_for(identity["player_id"], 1)) == 1
    result = registry.unbind("portrait", expected_generation=1)
    # The generation bump (mirroring bind/retire) is what a stale caller is fenced on below;
    # deleting the binding row alone withdraws execution bindings regardless of generation.
    assert result == {"generation": 2, "changed": True}
    assert registry.bindings_for(identity["player_id"], 1) == []
    assert registry.configuration_for(identity["player_id"], 1)["bindings"] == []
    assert registry.inventory().frames[0].player_id is None
    with pytest.raises(RegistryError, match="generation_conflict"):
        registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=1)
    rebound = registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=2)
    assert rebound == {"generation": 3, "changed": True}


def test_unbind_without_active_binding_or_unknown_frame_raises(registry):
    frame(registry)
    with pytest.raises(RegistryError, match="not_bound"):
        registry.unbind("portrait", expected_generation=0)
    with pytest.raises(RegistryError, match="unknown_frame"):
        registry.unbind("nonexistent", expected_generation=0)


def test_unbind_stays_reversible_while_retire_stays_permanent(registry):
    identity, _, enrollment = enroll(registry)
    frame(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    registry.unbind("portrait", expected_generation=1)
    # Reversible: the player record survives and re-binds; nothing is retired.
    assert registry.inventory().players[0].retired_at is None
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=2)
    registry.retire(identity["player_id"])
    with pytest.raises(RegistryError, match="unauthorized"):
        registry.authenticate(identity["token"])
    with pytest.raises(RegistryError, match="retired"):
        enroll(registry, device_id=enrollment.device_id)


def test_unbind_leaves_session_token_and_epoch_valid_unlike_retire(registry):
    identity, _, _ = enroll(registry)
    frame(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    registry.unbind("portrait", expected_generation=1)
    # Unlike retire, unbind withdraws only the execution binding: the player's
    # session token and authority_epoch survive, so the existing session keeps working.
    authenticated = registry.authenticate(identity["token"])
    assert authenticated["id"] == identity["player_id"]
    assert authenticated["authority_epoch"] == identity["authority_epoch"]
    rebound = registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=2)
    assert rebound == {"generation": 3, "changed": True}


def test_pending_queue_lists_enrolled_unbound_non_retired_players_only(registry):
    pending, _, _ = enroll(registry)
    bound, _, _ = enroll(registry)
    retired, _, _ = enroll(registry)
    frame(registry)
    registry.bind("portrait", bound["player_id"], "HDMI-A-1", expected_generation=0)
    registry.retire(retired["player_id"])
    inventory = registry.inventory()
    by_id = {p.id: p for p in inventory.players}
    assert by_id[pending["player_id"]].is_bound is False
    assert by_id[bound["player_id"]].is_bound is True
    assert by_id[retired["player_id"]].is_bound is False
    pending_ids = [p.id for p in inventory.players if p.retired_at is None and not p.is_bound]
    assert pending_ids == [pending["player_id"]]


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
    frames = registry.inventory().frames
    unbound = next(f for f in frames if f.player_id is None)
    registry.bind(unbound.id, identity["player_id"], "HDMI-A-2", expected_generation=0)
    assert len(registry.bindings_for(identity["player_id"], 1, include_unvalidated=True)) == 2
    assert all(f.generation == 1 for f in registry.inventory().frames)


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
    assert registry.inventory().frames[0].player_id == second["player_id"]


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


def test_typed_inventory_preserves_the_complete_operator_json_response(registry):
    identity, _, _ = enroll(registry)
    frame(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    expected = registry.inventory()
    assert isinstance(expected, InstallationInventory)
    assert expected.frames[0].profile.width_px == 1080
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        response = client.get("/v1/operator/inventory", headers={"Authorization": "Bearer " + ADMIN})
        assert response.status_code == 200
        assert response.json() == expected.model_dump(mode="json")
        assert InstallationInventory.model_validate_json(response.content) == expected
