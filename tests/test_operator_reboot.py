"""The operator's reboot over the bus (decision 0034 S4c part 1), through Central's real route and
device read: the route fences the device, queues S4a's `RebootPi` with the console's command id and
writes its ask in that transaction; the worker's real `RebootPiHandler` asks the Pi (a recording
`PiReboot` here) and records the outcome; the device read folds the two. No node session is named.

Mutation probes: drop the route's pending-copy check -> a second command id while one is queued
answers 200 and queues nothing, fails; serve `outstanding` without the queue's copy -> a request
whose job is gone stays Requested, fails.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from support.pi_reboots import RecordingReboot, reboot_queue, run_queued
from test_node_boot import enroll
from test_registry import ADMIN

from central.app import create_app
from central.fleet.node_sessions import NodeControlConfig
from central.infra.transactions import PgTransaction
from central.kernel.job_types import RebootPi

HEADERS = {"Authorization": "Bearer " + ADMIN}
SERIAL = "c0ffee01"


@pytest.fixture
def pi(registry):
    """(client, device id, the worker's run of the one queued reboot, the recording Pi)."""
    reboot_queue(registry.db)
    device = enroll(registry.db, SERIAL, registry.clock.utc())
    recording = RecordingReboot()

    def run(delivered: bool = True) -> RebootPi:
        return run_queued(registry.db, device, delivered=delivered, pi=recording)

    app = create_app(registry.db, registry.clock, ADMIN, node_control=NodeControlConfig("node-test"))
    with TestClient(app) as client:
        yield client, device, run, recording


def _reboot(client, device, command_id, reason="console/2026-11-18"):
    return client.post(f"/v1/operator/node/devices/{device}/reboots", headers=HEADERS,
                       json={"command_id": str(command_id), "reason": reason})


def _records(client, device):
    read = client.get(f"/v1/operator/node/devices/{device}", headers=HEADERS)
    assert read.status_code == 200, read.text
    return read.json()["reboot_commands"]


def test_an_operator_reboot_is_queued_then_asked_over_the_bus_and_listed(registry, pi):
    client, device, run_queued, pi_reboot = pi
    first = uuid4()
    answer = _reboot(client, device, first)
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"duplicate": False}
    [queued] = _records(client, device)
    assert {key: queued[key] for key in ("command_id", "reason", "by", "delivered", "answered_at", "outstanding")} == {
        "command_id": str(first), "reason": "console/2026-11-18", "by": "api-token", "delivered": None,
        "answered_at": None, "outstanding": True}
    # The same command id again is a duplicate; another while it is queued is refused, and leaves no ask.
    assert _reboot(client, device, first).json() == {"duplicate": True}
    refused = _reboot(client, device, uuid4())
    assert refused.status_code == 409 and refused.json()["error"] == "node_reboot_outstanding"
    assert [item["command_id"] for item in _records(client, device)] == [str(first)]
    job = run_queued()
    assert (job.request_id, job.by) == (str(first), "api-token")
    assert pi_reboot.asked == [(device, "console/2026-11-18")]
    [asked] = _records(client, device)
    assert asked["delivered"] is True and asked["outstanding"] is False
    assert asked["answered_at"] >= asked["requested_at"]
    # Once run, a new request is queued; a Pi with no link reads Not delivered.
    second = uuid4()
    assert _reboot(client, device, second, "console/2026-11-18/stuck").json() == {"duplicate": False}
    run_queued(delivered=False)
    newest = _records(client, device)[0]
    assert (newest["command_id"], newest["reason"], newest["delivered"]) == (
        str(second), "console/2026-11-18/stuck", False)


def test_an_install_reboot_is_listed_and_an_ask_off_the_queue_is_not_outstanding(registry, pi):
    client, device, run_queued, _ = pi
    publisher = client.app.state.content.publisher
    with registry.db.transaction() as conn:   # Install's reboot: its job alone, no ask row
        publisher.publish(RebootPi(request_id=str(uuid4()), device_id=device, reason="install:v2.0.0", by="central"),
                          within=PgTransaction(conn))
    # Install's queued reboot holds the Pi's one pending copy: the operator's is refused.
    assert _reboot(client, device, uuid4()).status_code == 409
    install = run_queued()
    [listed] = _records(client, device)
    assert (listed["command_id"], listed["reason"], listed["by"], listed["delivered"]) == (
        install.request_id, "install:v2.0.0", "central", True)
    # An ask whose job left the queue without an outcome is no longer outstanding.
    lost = uuid4()
    assert _reboot(client, device, lost).status_code == 200
    with registry.db.transaction() as conn:
        conn.execute("DELETE FROM procrastinate_jobs WHERE status='todo'")
    newest = _records(client, device)[0]
    assert (newest["command_id"], newest["delivered"], newest["outstanding"]) == (str(lost), None, False)
    assert _reboot(client, device, uuid4()).json() == {"duplicate": False}


def test_a_reboot_needs_an_active_device_and_the_new_body(registry, pi):
    client, device, _, _ = pi
    unknown = _reboot(client, "device-" + "ef" * 32, uuid4())
    assert unknown.status_code == 403 and unknown.json()["error"] == "node_device_unavailable"
    old = client.post(f"/v1/operator/node/devices/{device}/reboots", headers=HEADERS, json={
        "command_id": str(uuid4()), "session_id": str(uuid4()), "device_generation": 1,
        "operator_audit_ref": "console/2026-11-18", "valid_for_seconds": 30})
    assert old.status_code == 422 and old.json()["error"] == "invalid_operator_reboot"
    assert _reboot(client, device, uuid4(), reason="two words").status_code == 422
    assert client.post(f"/v1/operator/node/devices/{device}/reboots",
                       json={"command_id": str(uuid4()), "reason": "x"}).status_code in (401, 403)
    assert _records(client, device) == []
