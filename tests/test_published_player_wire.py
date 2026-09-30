"""Central's real REST/WebSocket bytes against pinned published Player `.deb` parsers.

Local pytest skips without PHOTO_WALL_PUBLISHED_PLAYER_WIRE_DIR. CI prepares the
downloaded assets and sets it, making this matrix a required Postgres gate.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_coordination import schedule
from test_registry import ADMIN, enroll, frame

from central.app import create_app
from contracts.models import Calibration
from scripts.published_player_wire import PLAYERS, _verified_package, package_root

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/published_player_wire.py"
STATE_FIELDS = {"configuration", "plan", "commits", "revocations"}


@pytest.fixture(scope="module")
def published_directory():
    configured = os.environ.get("PHOTO_WALL_PUBLISHED_PLAYER_WIRE_DIR")
    if not configured:
        pytest.skip("set PHOTO_WALL_PUBLISHED_PLAYER_WIRE_DIR to opt into published package checks")
    directory = Path(configured).resolve(strict=True)
    for player in PLAYERS:
        assert _verified_package(directory / player.tag / player.filename, player)
        assert (package_root(directory, player) / "player/service.py").is_file()
    return directory


def package_wire(root: Path, mode: str, body: dict | str | bytes) -> dict:
    wire = (json.dumps(body).encode() if isinstance(body, dict)
            else body.encode() if isinstance(body, str) else body)
    result = subprocess.run([sys.executable, "-I", str(SCRIPT), "child", str(root), mode],
                            input=wire, capture_output=True, check=True, timeout=15)
    received = json.loads(result.stdout)
    assert received["source"] == str(root / "player/service.py")
    return received


@pytest.mark.parametrize("player", PLAYERS, ids=lambda value: value.tag)
@pytest.mark.parametrize("cold", (False, True), ids=("warm", "cold-reenrollment"))
@pytest.mark.parametrize("bound", (False, True), ids=("unbound", "bound-active-plan"))
def test_published_player_accepts_central_legacy_transport_matrix(
        published_directory, registry, player, cold, bound):
    root = package_root(published_directory, player)
    identity, key, enrollment = enroll(registry)
    if cold:
        identity, _, _ = enroll(registry, key=key, device_id=enrollment.device_id)
        assert identity["authority_epoch"] == 2
    if bound:
        frame(registry, "frame-0")
        registry.bind("frame-0", identity["player_id"], "HDMI-A-1",
                      expected_generation=0)
        registry.calibrate("frame-0", "commit", 1, Calibration(),
                           expected_generation=1)
    # A new Central app instance serves an already-issued warm token or a freshly
    # re-enrolled epoch through the same database-backed compatibility projection.
    app = create_app(registry.db, registry.clock, ADMIN)
    if bound:
        schedule(app.state.coordinator, ("frame-0",), starts=1010)
        registry.clock.advance(6)
    headers = {"Authorization": "Bearer " + identity["token"]}
    with TestClient(app) as client:
        rest = client.get("/v1/player/state", headers=headers)
        assert rest.status_code == 200
        body = rest.json()
        assert set(body) == STATE_FIELDS
        parsed_rest = package_wire(root, "rest", rest.content)
        assert parsed_rest["accepted"] and parsed_rest["has_plan"] is bound
        with client.websocket_connect("/v1/player/session", headers=headers) as socket:
            raw_message = socket.receive_text()
            message = json.loads(raw_message)
            assert message["type"] == "state" and set(message) == STATE_FIELDS | {"type"}
            parsed_ws = package_wire(root, "websocket", raw_message)
            assert parsed_ws["accepted"] and parsed_ws["has_plan"] is bound
        if bound:
            plan = body["plan"]
            due = [layer["assignment_id"] for layer in plan["layers"]
                   if layer["start"] == 1010]
            assert due
            readiness = package_wire(root, "readiness", {
                "plan_id": plan["plan_id"], "revision": plan["revision"],
                "authority_epoch": identity["authority_epoch"], "sequence": 1,
                "secured": due, "prepared": due, "capacity_ok": True,
                "observed_at": registry.clock.utc(), "clock_uncertainty": .01,
            })["body"]
            reported = client.post("/v1/player/readiness", json=readiness, headers=headers)
            assert reported.status_code == 200 and reported.json() == {"accepted": True}
            committed = client.get("/v1/player/state", headers=headers)
            assert committed.status_code == 200
            parsed_commit = package_wire(root, "rest", committed.content)
            assert parsed_commit["accepted"] and parsed_commit["commits"] >= 1


@pytest.mark.parametrize("player", PLAYERS, ids=lambda value: value.tag)
def test_published_player_extra_field_negative_control(published_directory, player):
    root = package_root(published_directory, player)
    state = {"configuration": {"player_id": "p-" + "a" * 32,
                               "authority_epoch": 1, "configuration_revision": 1,
                               "bindings": []},
             "plan": None, "commits": [], "revocations": []}
    assert package_wire(root, "rest", state)["accepted"]
    assert not package_wire(root, "rest", {**state, "delivery_id": "unexpected"})[
        "accepted"]
    identify = package_wire(root, "rest", {**state, "identify_output": None})
    assert identify["accepted"] is (player.tag == "v0.13.0")
