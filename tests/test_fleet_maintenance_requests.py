"""PostgreSQL contract for inert operator maintenance intent."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from central.fleet.models import (
    Artifact,
    FleetError,
    MaintenanceRequestCancel,
    MaintenanceRequestWrite,
    PolicyWrite,
)
from central.fleet.routes import mount_fleet_routes
from central.fleet.service import FleetService

DEVICE = "device-" + "d" * 64
TAG = "v1.2.3"
DIGEST = "a" * 64
ABI = "sha256:" + "b" * 64


def _ready(registry):
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO devices(device_id,serial,first_seen,last_seen) "
                     "VALUES(%s,'abcdef1234567890',1,1)", (DEVICE,))
        conn.execute(
            "INSERT INTO app_releases(tag,major,minor,patch,is_prerelease,"
            "discovered_at,updated_at,mirror_state,payload_url,payload_sha256,"
            "payload_size,payload_format,payload_base_abi,payload_source_manifest) "
            "VALUES(%s,1,2,3,FALSE,1,1,'mirrored','https://example.invalid/app',"
            "%s,123,'pw-player-data-v1',%s,'manifest.v2.json')",
            (TAG, DIGEST, ABI),
        )
    service = FleetService(registry.db, registry.clock)
    selected = service.set_app_policy(PolicyWrite(
        expected_revision=0, target=Artifact(tag=TAG, sha256=DIGEST, size=123)))
    return service, selected["revision"]


def _write(revision: int, *, request_id: UUID | None = None,
           generation: int = 1, digest: str = DIGEST,
           ttl_seconds: int = 300) -> MaintenanceRequestWrite:
    return MaintenanceRequestWrite(
        request_id=request_id or uuid4(), expected_device_generation=generation,
        expected_policy_source="explicit", expected_policy_revision=revision,
        expected_target_sha256=digest, ttl_seconds=ttl_seconds,
    )


def test_frozen_request_replay_and_honest_status(registry) -> None:
    service, revision = _ready(registry)
    request = _write(revision)
    first = service.request_maintenance(DEVICE, request)
    assert first == service.request_maintenance(DEVICE, request)
    assert first["status"] == "queued" and first["revision"] == 1
    assert first["reason"] == "awaiting_command_authority"
    assert first["target"] == {
        "tag": TAG, "sha256": DIGEST, "size": 123,
        "format": "pw-player-data-v1", "base_abi": ABI,
        "source_manifest": "manifest.v2.json",
    }
    assert first["requested_at"] == 1000 and first["expires_at"] == 1300
    assert "attempt_id" not in first and "drain_id" not in first
    row = next(d for d in service.status()["devices"] if d["device_id"] == DEVICE)
    assert row["device_generation"] == 1
    assert row["maintenance_request"] == first
    assert row["update_now"] == {"available": False, "reason": "command_trust_unapproved"}
    assert row["base"]["current_physical_boot"] == "unknown"
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM fleet_app_attempts").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM equipment_drains").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM fleet_os_command_sessions").fetchone()["n"] == 0


def test_policy_target_generation_and_request_id_conflicts(registry) -> None:
    service, revision = _ready(registry)
    with pytest.raises(FleetError, match="device_generation_conflict"):
        service.request_maintenance(DEVICE, _write(revision, generation=2))
    with pytest.raises(FleetError, match="maintenance_policy_conflict"):
        service.request_maintenance(DEVICE, _write(revision, digest="f" * 64))
    first_request = _write(revision)
    first = service.request_maintenance(DEVICE, first_request)
    with pytest.raises(FleetError, match="maintenance_request_id_conflict"):
        service.request_maintenance(DEVICE, _write(
            revision, request_id=first_request.request_id, ttl_seconds=301))
    service.set_app_policy(PolicyWrite(expected_revision=revision, target=None))
    assert service.request_maintenance(DEVICE, first_request) == first
    with pytest.raises(FleetError, match="maintenance_policy_conflict"):
        service.request_maintenance(DEVICE, _write(revision))


def test_override_selection_and_release_recut_do_not_rewrite_snapshot(registry) -> None:
    service, fleet_revision = _ready(registry)
    override = service.set_override(
        DEVICE, expected_revision=0, target=Artifact(tag=TAG, sha256=DIGEST, size=123))
    request = MaintenanceRequestWrite(
        request_id=uuid4(), expected_device_generation=1,
        expected_policy_source="override",
        expected_policy_revision=override["revision"],
        expected_target_sha256=DIGEST, ttl_seconds=300,
    )
    with pytest.raises(FleetError, match="maintenance_policy_conflict"):
        service.request_maintenance(DEVICE, _write(fleet_revision))
    frozen = service.request_maintenance(DEVICE, request)
    assert frozen["policy_source"] == "override"
    with registry.db.transaction() as conn:
        conn.execute("UPDATE app_releases SET payload_sha256=%s WHERE tag=%s",
                     ("e" * 64, TAG))
    assert service.request_maintenance(DEVICE, request) == frozen
    with pytest.raises(FleetError, match="maintenance_target_unavailable|release_artifact_changed"):
        service.request_maintenance(DEVICE, request.model_copy(update={"request_id": uuid4()}))


def test_duplicate_and_competing_requests_serialize_across_connections(registry) -> None:
    service, revision = _ready(registry)
    first_request = _write(revision)
    gate = Barrier(2)

    def submit(request):
        gate.wait(timeout=5)
        try:
            return service.request_maintenance(DEVICE, request)
        except FleetError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, (first_request, first_request)))
    assert results[0] == results[1]
    gate = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, (_write(revision), _write(revision))))
    assert results == ["maintenance_request_already_queued"] * 2
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM fleet_maintenance_requests ").fetchone()["n"] == 1


def test_expiry_requeue_and_cancel_cas(registry) -> None:
    service, revision = _ready(registry)
    request = _write(revision)
    first = service.request_maintenance(DEVICE, request)
    registry.clock.advance(300)
    status = service.status()["devices"][0]["maintenance_request"]
    assert status["status"] == "expired" and status["reason"] == "request_expired"
    with pytest.raises(FleetError, match="maintenance_request_revision_conflict"):
        service.cancel_maintenance(DEVICE, request.request_id,
                                   MaintenanceRequestCancel(expected_revision=1))
    replay = service.request_maintenance(DEVICE, request)
    assert replay["status"] == "expired" and replay["revision"] == 2
    newer = service.request_maintenance(DEVICE, _write(revision))
    assert newer["request_id"] != first["request_id"]
    canceled = service.cancel_maintenance(
        DEVICE, UUID(newer["request_id"]), MaintenanceRequestCancel(expected_revision=1))
    assert canceled["status"] == "canceled" and canceled["revision"] == 2
    with pytest.raises(FleetError, match="maintenance_request_revision_conflict"):
        service.cancel_maintenance(DEVICE, UUID(newer["request_id"]),
                                   MaintenanceRequestCancel(expected_revision=1))


def test_latest_request_uses_insertion_order_across_backward_utc_step(registry) -> None:
    service, revision = _ready(registry)
    original = service.request_maintenance(DEVICE, _write(revision))
    service.cancel_maintenance(
        DEVICE, UUID(original["request_id"]), MaintenanceRequestCancel(expected_revision=1))
    registry.clock.step_utc(-120)
    newer = service.request_maintenance(DEVICE, _write(revision))
    assert newer["requested_at"] < original["requested_at"]
    projected = next(d for d in service.status()["devices"] if d["device_id"] == DEVICE)
    assert projected["maintenance_request"] == newer


def test_cancel_race_and_retirement_close_queued(registry) -> None:
    service, revision = _ready(registry)
    request = _write(revision)
    service.request_maintenance(DEVICE, request)
    gate = Barrier(2)

    def cancel(_):
        gate.wait(timeout=5)
        try:
            return service.cancel_maintenance(
                DEVICE, request.request_id, MaintenanceRequestCancel(expected_revision=1))["status"]
        except FleetError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert set(pool.map(cancel, range(2))) == {
            "canceled", "maintenance_request_revision_conflict"}
    queued = service.request_maintenance(DEVICE, _write(revision))
    with registry.db.transaction() as conn:
        conn.execute("UPDATE devices SET retired_at=%s WHERE device_id=%s",
                     (registry.clock.utc(), DEVICE))
        row = conn.execute("SELECT status,reason,revision FROM fleet_maintenance_requests "
                           "WHERE request_id=%s", (UUID(queued["request_id"]),)).fetchone()
        assert row == {"status": "canceled", "reason": "device_retired", "revision": 2}
    with pytest.raises(FleetError, match="device_retired"):
        service.request_maintenance(DEVICE, _write(revision))


def test_snapshot_is_database_immutable(registry) -> None:
    service, revision = _ready(registry)
    request = _write(revision)
    service.request_maintenance(DEVICE, request)
    with pytest.raises(psycopg.errors.CheckViolation):
        with registry.db.transaction() as conn:
            conn.execute("UPDATE fleet_maintenance_requests SET target_sha256=%s "
                         "WHERE request_id=%s", ("f" * 64, request.request_id))
    assert service.request_maintenance(DEVICE, request)["target"]["sha256"] == DIGEST


def test_admin_routes_validate_and_expose_intent(registry) -> None:
    _service, revision = _ready(registry)
    app = FastAPI()
    mount_fleet_routes(app, db=registry.db, clock=registry.clock, admin=lambda: None)
    client = TestClient(app)
    body = _write(revision).model_dump(mode="json")
    path = f"/v1/operator/fleet/devices/{DEVICE}/maintenance-requests"
    assert client.post(path, json={**body, "ttl_seconds": 299}).status_code == 422
    created = client.post(path, json=body)
    assert created.status_code == 200 and created.json()["status"] == "queued"
    status = client.get("/v1/operator/fleet").json()
    assert status["devices"][0]["maintenance_request"] == created.json()
    canceled = client.request("DELETE", f"{path}/{body['request_id']}",
                              json={"expected_revision": 1})
    assert canceled.status_code == 200 and canceled.json()["status"] == "canceled"
