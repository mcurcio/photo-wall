"""The mounted T0 seam: serial check-ins and the operator status; no V1 offer or fleet intent."""

from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import central.fleet.routes as routes
from central.fleet.models import FleetError
from contracts.time import ManualClock

BOOT_ID = UUID(int=2)
CHECK_IN = {"schema": 1, "kind": "pi", "serial": "abcdef1234567890",
            "kernel_boot_id": str(BOOT_ID), "agent_incarnation": "agent-1",
            "observation_sequence": 1, "phase": "base_ready"}


class Service:
    def __init__(self, _db, _clock):
        self.calls = []

    def record_check_in(self, body):
        self.calls.append(("check_in", body.observation_sequence))
        return {"accepted": True}

    def status(self):
        return {"devices": [], "commands_available": False}


def _client(monkeypatch, service=Service):
    monkeypatch.setattr(routes, "FleetService", service)
    app = FastAPI()
    mounted = routes.mount_fleet_routes(app, db=None, clock=ManualClock(100),
                                        admin=lambda: None)
    return TestClient(app), mounted


def test_a_check_in_and_the_status_are_served(monkeypatch) -> None:
    client, service = _client(monkeypatch)
    assert client.post("/v1/appliance/check-ins", json=CHECK_IN).json() == {"accepted": True}
    assert service.calls == [("check_in", 1)]
    assert client.get("/v1/operator/fleet").json() == {"devices": [],
                                                       "commands_available": False}


@pytest.mark.parametrize(("method", "path"), [
    ("POST", "/v1/netboot/offers"),
    ("GET", f"/v1/netboot/offers/{UUID(int=1)}/base"),
    ("GET", f"/v1/netboot/offers/{UUID(int=1)}/app"),
    ("PUT", "/v1/operator/fleet/app-policy"),
    ("PUT", "/v1/operator/fleet/devices/device-1/app-override"),
    ("DELETE", "/v1/operator/fleet/devices/device-1/app-override"),
    ("PUT", "/v1/operator/fleet/base-baseline"),
    ("POST", "/v1/operator/fleet/devices/device-1/maintenance-requests"),
    ("DELETE", f"/v1/operator/fleet/devices/device-1/maintenance-requests/{UUID(int=1)}"),
])
def test_no_v1_offer_or_fleet_intent_route_is_mounted(monkeypatch, method, path) -> None:
    client, service = _client(monkeypatch)
    assert client.request(method, path, json={}).status_code == 404
    assert service.calls == []


def test_quota_429_retries_after_the_utc_day(monkeypatch) -> None:
    class QuotaService(Service):
        def record_check_in(self, body):
            raise FleetError("t0_rate_limited", 429)

    client, _ = _client(monkeypatch, QuotaService)
    response = client.post("/v1/appliance/check-ins", json=CHECK_IN)
    assert response.status_code == 429
    assert response.headers["retry-after"] == str(86400 - 100)
