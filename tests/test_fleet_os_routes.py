"""HTTP contract for the separately mounted, authenticated OS data seam."""

import base64
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_fleet_attempts import _principal, _seed_file_attempt

from central.app import create_app
from central.fleet.os_routes import (
    OsVerifierForbidden,
    OsVerifierUnauthorized,
    mount_os_fleet_data_routes,
)
from contracts.os_attempt_report import MAX_ATTEMPT_REPORT_BYTES, OsAttemptReport
from contracts.time import ManualClock

COMMAND_ID = UUID(int=904)
DRAIN_ID = UUID(int=905)


def _app(registry, reader, *, verifier):
    app = FastAPI()
    mount_os_fleet_data_routes(
        app, verifier=verifier, db=registry.db, clock=registry.clock,
        content=SimpleNamespace(reader=reader),
    )
    return app


def _report(attempt_id: UUID, *, target_sha: str, **changes) -> dict:
    principal = _principal()
    values = dict(
        device_id=principal.device_id,
        device_generation=principal.device_generation,
        installation_audience=principal.installation_audience,
        kernel_boot_id=principal.kernel_boot_id,
        offer_id=principal.offer_id,
        command_session_id=principal.command_session_id,
        attempt_id=attempt_id,
        command_id=COMMAND_ID,
        drain_id=DRAIN_ID,
        report_sequence=1,
        sampled_boottime_ms=1234,
        executor_state="committed",
        active_sha256=target_sha,
    )
    values.update(changes)
    return OsAttemptReport(**values).model_dump(mode="json", by_alias=True)


def test_mount_requires_a_verifier() -> None:
    with pytest.raises(ValueError, match="os_request_verifier_required"):
        mount_os_fleet_data_routes(
            FastAPI(), verifier=None, db=None, clock=ManualClock(1000),
            content=SimpleNamespace(reader=None),
        )


def test_central_composition_has_no_os_data_routes_without_d14_provider(registry) -> None:
    app = create_app(
        db=registry.db, clock=registry.clock, admin_token="a" * 32,
        run_scheduler=False, mdns_enabled=False,
    )
    assert all(not route.path.startswith("/v1/os/") for route in app.routes)


def test_exact_attempt_bytes_require_verifier_and_current_session(registry, tmp_path) -> None:
    attempt, reader, target_sha, fallback_sha = _seed_file_attempt(registry, tmp_path)
    path = f"/v1/os/attempts/{attempt.attempt_id}/artifacts"

    def missing(_request):
        raise OsVerifierUnauthorized

    def denied(_request):
        raise OsVerifierForbidden

    for verifier, status, code in (
        (missing, 401, "os_authentication_required"),
        (denied, 403, "os_verifier_denied"),
        (lambda _request: None, 403, "os_verifier_denied"),
        (lambda _request: "t0-serial-claim", 403, "os_verifier_denied"),
    ):
        response = TestClient(_app(registry, reader, verifier=verifier)).get(path + "/target")
        assert response.status_code == status and response.json() == {"error": code}
        assert response.headers["cache-control"] == "private, no-store"
    assert reader.fds == []

    async def verified(_request):
        return _principal()

    client = TestClient(_app(registry, reader, verifier=verified))
    for role, expected, digest in (
        ("target", b"attempt target bytes", target_sha),
        ("fallback", b"accepted fallback bytes", fallback_sha),
    ):
        response = client.get(path + "/" + role)
        assert response.status_code == 200 and response.content == expected
        assert response.headers["content-length"] == str(len(expected))
        assert response.headers["digest"] == "sha-256=" + base64.b64encode(
            bytes.fromhex(digest)).decode()
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
    assert client.get(path + "/Target").json() == {
        "error": "attempt_artifact_request_invalid"}
    for automatic in ("/v1/os/attempts/not-a-uuid/artifacts/target",
                      "/v1/os/missing"):
        response = client.get(automatic)
        assert response.headers["cache-control"] == "private, no-store"
    assert len(reader.fds) == 2

    wrong_boot = replace(_principal(), kernel_boot_id=UUID(int=999))
    response = TestClient(_app(registry, reader, verifier=lambda _req: wrong_boot)).get(
        path + "/target")
    assert response.status_code == 403
    assert response.json() == {"error": "os_command_session_unavailable"}
    assert len(reader.fds) == 2

    registry.clock.advance(101)
    response = client.get(path + "/fallback")
    assert response.status_code == 403
    assert response.json() == {"error": "os_command_session_unavailable"}
    assert len(reader.fds) == 2


def test_reports_are_bounded_strict_and_only_carrier_claims(registry, tmp_path) -> None:
    attempt, reader, target_sha, _ = _seed_file_attempt(registry, tmp_path)
    with registry.db.transaction() as conn:
        conn.execute(
            "UPDATE fleet_app_attempts SET phase='stop_committed',command_id=%s,"
            "drain_id=%s WHERE attempt_id=%s",
            (COMMAND_ID, DRAIN_ID, attempt.attempt_id),
        )
    path = f"/v1/os/attempts/{attempt.attempt_id}/reports"
    body = _report(attempt.attempt_id, target_sha=target_sha)
    client = TestClient(_app(registry, reader, verifier=lambda _req: _principal()))

    response = client.post(path, json=body)
    assert response.status_code == 201 and response.json() == {"disposition": "stored"}
    assert response.headers["cache-control"] == "private, no-store"
    replay = client.post(path, json=body)
    assert replay.status_code == 200 and replay.json() == {"disposition": "replayed"}

    mismatch = client.post(
        f"/v1/os/attempts/{UUID(int=999)}/reports", json=body)
    assert mismatch.status_code == 409
    assert mismatch.json() == {"error": "attempt_report_path_mismatch"}
    valid_raw = json.dumps(body, separators=(",", ":")).encode()
    duplicate = valid_raw.replace(b'"report_sequence":1',
                                  b'"report_sequence":1,"report_sequence":1')
    assert duplicate != valid_raw
    invalid = client.post(path, content=duplicate)
    assert invalid.status_code == 422
    assert invalid.json() == {"error": "attempt_report_invalid"}
    oversized = client.post(path, content=b" " * (MAX_ATTEMPT_REPORT_BYTES + 1))
    assert oversized.status_code == 413
    assert oversized.json() == {"error": "attempt_report_too_large"}
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM fleet_os_attempt_reports")\
            .fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM fleet_generation_acceptances")\
            .fetchone()["n"] == 1  # the previously qualified fallback only

    registry.clock.advance(101)
    expired = client.post(path, json=body)
    assert expired.status_code == 403
    assert expired.json() == {"error": "os_command_session_unavailable"}


def test_t0_or_unmounted_os_route_never_records_a_report(registry, tmp_path) -> None:
    attempt, reader, target_sha, _ = _seed_file_attempt(registry, tmp_path)
    path = f"/v1/os/attempts/{attempt.attempt_id}/reports"
    body = _report(attempt.attempt_id, target_sha=target_sha)
    assert TestClient(FastAPI()).post(path, json=body).status_code == 404
    response = TestClient(_app(registry, reader, verifier=lambda _req: None)).post(
        path, json=body)
    assert response.status_code == 403
    assert response.json() == {"error": "os_verifier_denied"}
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM fleet_os_attempt_reports")\
            .fetchone()["n"] == 0
