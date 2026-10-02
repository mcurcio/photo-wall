"""Central's real REST/WebSocket bytes against pinned published Player `.deb` parsers.

Local pytest skips without PHOTO_WALL_PUBLISHED_PLAYER_WIRE_DIR. CI prepares the
downloaded assets and sets it, making this matrix a required Postgres gate.
"""

import json
import os
import selectors
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_coordination import schedule
from test_registry import ADMIN, frame

from central.app import create_app
from contracts.models import Calibration
from scripts.published_player_wire import PLAYERS, _verified_package, package_root

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/published_player_wire.py"
STATE_FIELDS = {"configuration", "plan", "commits", "revocations"}
# Each wait spans a cold published-Player interpreter start; under -n 4 on a 4-vCPU
# runner that exceeded 15 s. The assertion is that the request arrives, not its speed.
HANDSHAKE_SECONDS = 45


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


def enroll_from_published_package(client: TestClient, root: Path, *, cold: bool) -> dict:
    """Relay the released PlayerService.enroll calls to real Central HTTP routes."""
    rounds = 2 if cold else 1
    process = subprocess.Popen(
        [sys.executable, "-I", str(SCRIPT), "handshake", str(root), str(rounds)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    registrations = []
    expected_paths = ("/v1/enrollment/challenge", "/v1/enrollment/register")
    try:
        assert process.stdin is not None and process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            for _ in range(rounds):
                for path in expected_paths:
                    assert selector.select(timeout=HANDSHAKE_SECONDS), "published Player enrollment timed out"
                    line = process.stdout.readline()
                    assert line, "published Player enrollment exited before HTTP request"
                    request = json.loads(line)
                    assert request["event"] == "request"
                    assert request["source"] == str(root / "player/service.py")
                    assert (request["method"], request["path"], request["authenticated"]) == (
                        "POST", path, False)
                    response = client.post(path, json=request["body"])
                    assert response.status_code == 200, response.text
                    process.stdin.write(json.dumps({"status": response.status_code,
                                                    "body": response.json()}) + "\n")
                    process.stdin.flush()
                    if path.endswith("/register"):
                        registrations.append(response.json())
                assert selector.select(timeout=HANDSHAKE_SECONDS), "published Player registration timed out"
                line = process.stdout.readline()
                assert line, "published Player omitted parsed registration"
                registered = json.loads(line)
                assert registered == {"event": "registered",
                                      "source": str(root / "player/service.py"),
                                      "player_id": registrations[-1]["player_id"],
                                      "authority_epoch": registrations[-1]["authority_epoch"],
                                      "has_token": True}
        process.stdin.close()
        assert process.wait(timeout=15) == 0, process.stderr.read()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    assert len(registrations) == rounds
    if cold:
        assert registrations[0]["player_id"] == registrations[1]["player_id"]
        assert registrations[1]["authority_epoch"] == registrations[0]["authority_epoch"] + 1
    return registrations[-1]


# One worker runs the whole matrix so its cells do not contend for CPU with each other.
@pytest.mark.xdist_group("published_player_wire_matrix")
@pytest.mark.parametrize("player", PLAYERS, ids=lambda value: value.tag)
@pytest.mark.parametrize("cold", (False, True), ids=("warm", "cold-reenrollment"))
@pytest.mark.parametrize("bound", (False, True), ids=("unbound", "bound-active-plan"))
def test_published_player_accepts_central_legacy_transport_matrix(
        published_directory, registry, player, cold, bound):
    root = package_root(published_directory, player)
    with TestClient(create_app(registry.db, registry.clock, ADMIN)) as enrollment_client:
        identity = enroll_from_published_package(enrollment_client, root, cold=cold)
    # A new Central instance serves the retained token from a published
    # package's actual HTTP handshake and the same database-backed selection.
    app = create_app(registry.db, registry.clock, ADMIN)
    if bound:
        frame(registry, "frame-0")
        registry.bind("frame-0", identity["player_id"], "HDMI-A-1",
                      expected_generation=0)
        registry.calibrate("frame-0", "commit", 1, Calibration(),
                           expected_generation=1)
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
        late_hello = client.post("/v1/player/hello", json={
            "authority_epoch": identity["authority_epoch"], "schemas": [1, 2],
            "capabilities": ["identify_output"],
        }, headers=headers)
        assert late_hello.status_code == 409
        assert late_hello.json() == {"error": "control_negotiation_closed"}
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
