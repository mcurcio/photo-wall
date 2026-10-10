"""Central's real REST/WebSocket bytes against pinned published Player `.deb` parsers.

Local pytest skips without PHOTO_WALL_PUBLISHED_PLAYER_WIRE_DIR. CI prepares the
downloaded assets and sets it, making this matrix a required Postgres gate.
"""

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_coordination import schedule
from test_player_control_protocol import CURRENT, IDENTIFY_ONLY, LEGACY, after_end_state
from test_registry import ADMIN, frame

from central.app import create_app
from contracts.models import Calibration
from scripts.published_player_wire import PLAYERS, _verified_package, package_root

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/published_player_wire.py"
STATE_FIELDS = {"configuration", "plan", "commits", "revocations"}
HANDSHAKE_SECONDS = 15


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
    assert process.stdin is not None and process.stdout is not None
    # A reader thread owns the buffered text stream: select() on the raw pipe cannot
    # see lines already pulled into Python's buffer by a previous readline().
    lines: queue.Queue[str] = queue.Queue()

    def pump(stream=process.stdout):
        for received in stream:
            lines.put(received)
        lines.put("")

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()

    def next_line(message: str) -> str:
        try:
            return lines.get(timeout=HANDSHAKE_SECONDS)
        except queue.Empty:
            raise AssertionError(message) from None

    try:
        for _ in range(rounds):
            for path in expected_paths:
                line = next_line("published Player enrollment timed out")
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
            line = next_line("published Player registration timed out")
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
        reader.join(timeout=5)  # the pipe is at EOF once the child is gone
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    assert len(registrations) == rounds
    if cold:
        assert registrations[0]["player_id"] == registrations[1]["player_id"]
        assert registrations[1]["authority_epoch"] == registrations[0]["authority_epoch"] + 1
    return registrations[-1]


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


@pytest.mark.parametrize("player", PLAYERS, ids=lambda value: value.tag)
def test_published_player_reads_each_after_state_as_its_keep_flag(published_directory, player):
    """A plan with a kept photo and an ending that keeps nothing reaches a published Player in
    the shape it parses (`retain_on_expiry`); the current shape, `after_end`, which no
    published Player offers to read, it refuses (contracts/player_control.py
    LAYER_AFTER_END)."""
    root = package_root(published_directory, player)
    sessions = [LEGACY] + ([IDENTIFY_ONLY] if player.tag != "v0.12.0" else [])
    for selection in sessions:
        parsed = package_wire(root, "rest", after_end_state(selection))
        assert parsed["accepted"] and parsed["has_plan"], parsed
    current = {key: value for key, value in after_end_state(CURRENT).items()
               if key != "identify_output"}
    assert not package_wire(root, "rest", current)["accepted"]
