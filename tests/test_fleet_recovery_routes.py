"""Transport contract for the unmounted loader-OS recovery data adapter."""

from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_fleet_recovery import (
    ATTEMPT,
    LEASE1,
    LEASE2,
    _principal,
    _report,
    _seed,
)

from central.app import create_app
from central.fleet.os_routes import (
    OsVerifierForbidden,
    OsVerifierUnauthorized,
    mount_os_fleet_data_routes,
)
from central.fleet.recovery_routes import mount_os_recovery_data_routes
from contracts.time import ManualClock


def _client(registry, verifier):
    app = FastAPI()
    mount_os_recovery_data_routes(
        app, verifier=verifier, db=registry.db, clock=registry.clock,
        content=SimpleNamespace(reader=None),
    )
    return TestClient(app)


def test_recovery_mount_requires_verifier_and_is_absent_in_central(registry) -> None:
    with pytest.raises(ValueError, match="os_request_verifier_required"):
        mount_os_recovery_data_routes(
            FastAPI(), verifier=None, db=registry.db, clock=ManualClock(1000),
            content=SimpleNamespace(reader=None),
        )
    production = create_app(db=registry.db, clock=registry.clock,
                            admin_token="a" * 32, run_scheduler=False,
                            mdns_enabled=False)
    assert all(not route.path.startswith("/v1/os/") for route in production.routes)


def test_both_os_adapters_share_one_no_store_boundary(registry) -> None:
    app = FastAPI()
    services = SimpleNamespace(reader=None)
    def verifier(_request):
        return _principal()

    mount_os_fleet_data_routes(app, verifier=verifier, db=registry.db,
                               clock=registry.clock, content=services)
    mount_os_recovery_data_routes(app, verifier=verifier, db=registry.db,
                                  clock=registry.clock, content=services)
    assert [middleware.cls.__name__ for middleware in app.user_middleware].count(
        "BaseHTTPMiddleware") == 1


def test_claim_and_report_require_verified_current_carrier(registry) -> None:
    _seed(registry)
    path = f"/v1/os/attempts/{ATTEMPT}/recovery-leases"

    def missing(_request):
        raise OsVerifierUnauthorized

    def denied(_request):
        raise OsVerifierForbidden

    for verifier, status, code in (
        (missing, 401, "os_authentication_required"),
        (denied, 403, "os_verifier_denied"),
        (lambda _request: "serial-claim", 403, "os_verifier_denied"),
    ):
        response = _client(registry, verifier).post(
            path, json={"schema": 1, "lease_id": str(LEASE1),
                        "expected_lease_id": None})
        assert response.status_code == status and response.json() == {"error": code}
        assert response.headers["cache-control"] == "private, no-store"

    client = _client(registry, lambda _request: _principal())
    response = client.post(path, json={"schema": 1, "lease_id": str(LEASE1),
                                       "expected_lease_id": None})
    assert response.status_code == 200
    assert response.json()["lease_id"] == str(LEASE1)
    assert response.headers["cache-control"] == "private, no-store"
    report_path = f"{path}/{LEASE1}/reports"
    body = _report(LEASE1).model_dump(mode="json", by_alias=True)
    assert client.post(report_path, json=body).status_code == 201
    replay = client.post(report_path, json=body)
    assert replay.status_code == 200 and replay.json() == {"disposition": "replayed"}
    assert replay.headers["cache-control"] == "private, no-store"
    wrong_path = client.post(f"{path}/{UUID(int=999)}/reports", json=body)
    assert wrong_path.status_code == 409
    assert wrong_path.json() == {"error": "recovery_report_path_mismatch"}
    asset = client.get(f"{path}/{LEASE1}/artifacts/target")
    assert asset.status_code == 503 and asset.json() == {"error": "content_unavailable"}
    assert asset.headers["cache-control"] == "private, no-store"


def test_claim_rejects_oversize_duplicate_json_and_stale_cas(registry) -> None:
    _seed(registry)
    path = f"/v1/os/attempts/{ATTEMPT}/recovery-leases"
    client = _client(registry, lambda _request: _principal())
    assert client.post(path, content=b"{" + b" " * 1024 + b"}").status_code == 413
    duplicate = (b'{"schema":1,"schema":1,"lease_id":"' +
                 str(LEASE1).encode() + b'","expected_lease_id":null}')
    response = client.post(path, content=duplicate)
    assert response.status_code == 422
    assert response.json() == {"error": "recovery_lease_request_invalid"}
    assert client.post(path, json={"schema": 1, "lease_id": str(LEASE1),
                                   "expected_lease_id": None}).status_code == 200
    stale = client.post(path, json={"schema": 1, "lease_id": str(LEASE2),
                                    "expected_lease_id": None})
    assert stale.status_code == 409 and stale.json() == {"error": "recovery_lease_conflict"}
    automatic = client.get("/v1/os/attempts/not-a-uuid/recovery-leases/none/artifacts/target")
    assert automatic.headers["cache-control"] == "private, no-store"
