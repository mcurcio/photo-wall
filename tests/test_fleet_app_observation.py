"""Schema-2 T0 app evidence shares v1 admission and remains an untrusted claim."""

from copy import deepcopy
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from central.fleet.models import CheckInV2
from central.fleet.routes import mount_fleet_routes
from central.fleet.service import FleetService

SERIAL = "abcdef1234567890"
BOOT_A = UUID(int=1)
BOOT_B = UUID(int=2)
APP = "a" * 64


def _report(*, boot: UUID = BOOT_A, sequence: int = 1,
            installed: str | None = APP, running: bool = True) -> dict:
    return {
        "schema": 2, "kind": "pi", "serial": SERIAL,
        "kernel_boot_id": str(boot), "agent_incarnation": "base-agent-1",
        "observation_sequence": sequence, "phase": "base_ready",
        "app_evidence": {
            "kernel_boot_id": str(boot), "installed_sha256": installed,
            "running": {"sha256": installed, "pid": 123, "start_ticks": 456,
                        "invocation_id": "b" * 32} if running else None,
            "installed_reason": None if installed else "evidence_unavailable",
            "running_reason": None if running else "unit_inactive",
        },
    }


@pytest.mark.parametrize("change", [
    lambda body: body["app_evidence"].update(kernel_boot_id=str(BOOT_B)),
    lambda body: body["app_evidence"].update(installed_reason="unit_inactive"),
    lambda body: body["app_evidence"].update(running_reason="process_unconfirmed"),
    lambda body: body["app_evidence"]["running"].update(sha256="c" * 64),
    lambda body: body["app_evidence"]["running"].update(pid=True),
    lambda body: body["app_evidence"]["running"].update(start_ticks="456"),
    lambda body: body["app_evidence"].update(extra="not allowed"),
    lambda body: body.update(observation_sequence="1"),
    lambda body: body.update(sampled_boottime_ms=True),
])
def test_v2_rejects_incoherent_or_unbounded_evidence(change) -> None:
    body = _report()
    change(body)
    with pytest.raises(ValidationError):
        CheckInV2.model_validate(body)


def test_v1_v2_routes_share_admission_and_latest_row(registry) -> None:
    app = FastAPI()
    service = mount_fleet_routes(app, db=registry.db, clock=registry.clock,
                                 admin=lambda: None)
    client = TestClient(app)
    first = _report()
    assert client.post("/v2/appliance/check-ins", json=first).json() == {"accepted": True}
    assert client.post("/v1/appliance/check-ins", json=first).status_code == 422
    v1 = {key: value for key, value in first.items() if key != "app_evidence"}
    v1["schema"] = 1
    assert client.post("/v1/appliance/check-ins", json=v1).json() == {
        "accepted": False, "reason": "stale_or_duplicate", "next_sequence": 2}
    v1["observation_sequence"] = 2
    assert client.post("/v1/appliance/check-ins", json=v1).json() == {"accepted": True}
    device = service.status()["devices"][0]
    assert device["installed"]["state"] == "unknown"
    assert device["installed"]["reason"] == "schema_one_no_app_evidence"
    assert device["running"]["reason"] == "schema_one_no_app_evidence"
    assert device["base"]["boot_id"] == device["installed"]["boot_id"]
    with registry.db.transaction() as conn:
        rows = conn.execute("SELECT observation_schema,app_installed_sha256 "
                            "FROM fleet_os_observations ORDER BY observation_sequence").fetchall()
        assert rows == [{"observation_schema": 2, "app_installed_sha256": APP},
                        {"observation_schema": 1, "app_installed_sha256": None}]
        assert conn.execute("SELECT count(*) AS n FROM fleet_accepted_artifacts").fetchone()[
            "n"] == 0
        assert conn.execute("SELECT used FROM fleet_t0_daily_quotas "
                            "WHERE kind='observation' ORDER BY used DESC LIMIT 1").fetchone()[
            "used"] == 3


def test_v2_projects_one_boot_and_unknown_collector_reasons(registry) -> None:
    service = FleetService(registry.db, registry.clock)
    assert service.record_check_in(CheckInV2.model_validate(_report(
        boot=BOOT_B, installed=APP, running=True))) == {"accepted": True}
    # A delayed old-boot report wins the latest received claim without proving
    # that the old boot is the current physical boot.
    old = _report(boot=BOOT_A, installed=APP, running=False)
    old["app_evidence"]["running_reason"] = "process_unconfirmed"
    assert service.record_check_in(CheckInV2.model_validate(old)) == {"accepted": True}
    device = service.status()["devices"][0]
    assert device["base"]["boot_id"] == str(BOOT_A)
    assert device["base"]["state"] == "ambiguous_boot_claims"
    assert device["installed"]["boot_id"] == str(BOOT_A)
    assert device["installed"]["state"] == "reported"
    assert device["installed"]["digest"] == APP
    assert device["installed"]["source"] == "serial_claim"
    assert device["installed"]["assurance"] == "t0_unverified"
    assert device["installed"]["boot_linkage"] == "claim_only"
    assert device["installed"]["boot_ambiguity"] is True
    assert device["running"]["state"] == "unknown"
    assert device["running"]["reason"] == "process_unconfirmed"
    assert device["running"]["process"] is None
    assert device["running"]["boot_id"] == str(BOOT_A)
    assert device["running"]["current_physical_boot"] == "unknown"
    unknown = deepcopy(_report(boot=BOOT_A, sequence=2, installed=None, running=False))
    unknown["app_evidence"]["running_reason"] = "evidence_unavailable"
    assert service.record_check_in(CheckInV2.model_validate(unknown)) == {"accepted": True}
    device = service.status()["devices"][0]
    assert device["installed"]["reason"] == "evidence_unavailable"
    assert device["installed"]["digest"] is None
    assert device["running"]["reason"] == "evidence_unavailable"


def test_v2_projects_running_process_identity(registry) -> None:
    service = FleetService(registry.db, registry.clock)
    assert service.record_check_in(CheckInV2.model_validate(_report())) == {"accepted": True}
    registry.clock.advance(7)
    running = service.status()["devices"][0]["running"]
    assert running["state"] == "reported"
    assert running["digest"] == APP
    assert running["process"] == {"pid": 123, "start_ticks": 456,
                                  "invocation_id": "b" * 32}
    assert running["age_seconds"] == 7


def test_v2_http_invalid_evidence_does_not_charge_quota(registry) -> None:
    app = FastAPI()
    mount_fleet_routes(app, db=registry.db, clock=registry.clock, admin=lambda: None)
    body = _report()
    body["app_evidence"]["running"]["sha256"] = "c" * 64
    response = TestClient(app).post("/v2/appliance/check-ins", json=body)
    assert response.status_code == 422
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM fleet_t0_daily_quotas").fetchone()[
            "n"] == 0
