"""Bug-1 class: a late first POST plus a Central outage after accept still converges.

The broker's session claim is built at node start, its first POST happens after a
cold start of more than nine seconds, and Central is unreachable for longer than
the former 30 s stop-permit window once the stage was accepted. The switch must
complete locally, report every effect on reconnect, draw no refusal and hold
nothing at Central. Node and Central clocks deliberately disagree.
"""
import json
import os
from types import SimpleNamespace
from uuid import uuid4

from test_fleet_attempts import BOOT_ID, DEVICE_ID, SERIAL
from test_node_lifecycle import Rig
from test_node_online_broker import Driver

import appliance.node.online_broker as online_broker
import appliance.node.session as node_session
from appliance.node.broker import RunningApp
from appliance.node.online_runner import OnlineRunner
from appliance.node.session import NodeSession
from appliance.node.storage import BootStore
from central.fleet.node_sessions import NodeControlError
from contracts.node_commands import encode_session_grant, parse_session_claim
from contracts.node_lifecycle import parse_app_effect_event


class Central:
    """In-process node routes over the real owners; an outage is a transport failure."""

    def __init__(self, fixture):
        self.fixture, self.down, self.log = fixture, False, []

    def request(self, method, path, body=None, claim=None):
        if self.down:
            raise OSError("central unreachable")
        lifecycle, sessions = self.fixture.service, self.fixture.sessions
        try:
            if path == "/v2/node/sessions":
                result = encode_session_grant(sessions.enroll(parse_session_claim(body)))
            elif path == "/v2/node/app-commands":
                result = json.dumps(lifecycle.desired(claim.session_id, claim.credential, effects=True)).encode()
            elif path == "/v2/node/app-responses":
                result = json.dumps(lifecycle.response(claim.session_id, claim.credential, body)).encode()
            elif path == "/v2/node/app-effects":
                result = json.dumps(lifecycle.effect(claim.session_id, claim.credential, body)).encode()
            else:
                raise AssertionError(path)
        except NodeControlError as error:
            self.log.append((path, error.status, error.code))
            return error.status, json.dumps({"error": error.code}).encode()
        self.log.append((path, 200, None))
        return 200, result


def test_late_first_post_and_outage_after_accept_converge_and_report(registry, tmp_path, monkeypatch):
    fixture = Rig(registry)
    node_ms = {"v": 50_000}
    monkeypatch.setattr(node_session, "boottime_ms", lambda: node_ms["v"])
    monkeypatch.setattr(online_broker, "boottime_ms", lambda: node_ms["v"])
    monkeypatch.setattr(online_broker, "memory_values", lambda: (8 * 1024**3, 7 * 1024**3))
    central = Central(fixture)
    directory = tmp_path / "broker"
    directory.mkdir(mode=0o700)
    store = BootStore(directory, boot_id=BOOT_ID, policy={"owner": "app_effect_broker"}, owner_uid=os.getuid())
    # The claim is built at node start, for the broker producer already linked to the app.
    session = NodeSession(store, central, owner="app_effect_broker", serial=SERIAL,
                          offer_id=fixture.claim.offer_id, kernel_boot_id=BOOT_ID,
                          incarnation_id=fixture.claim.incarnation_id)
    node_ms["v"] += 9_500  # Cold start: the first POST is 9.5 s after the claim was built.
    registry.clock.advance(1)
    grant = session.ensure()
    assert grant is not None
    command = fixture.stage(session_id=grant.session_id)
    driver = Driver(RunningApp(command.old_environment, command.old_process, command.old_app_epoch, uuid4()))
    prepared = tmp_path / "prepared.json"
    runner = OnlineRunner(store, driver, session, SimpleNamespace(arm=lambda o: o.receipt, advance=lambda *a: None),
                          prepared=prepared)
    runner.worker = SimpleNamespace(advance=lambda command: None, ready=lambda command: True)

    runner.tick()  # Central reachable: the stage is accepted while preparation is still running.
    assert runner.broker.record["phase"] == "preparing" and driver.stop_calls == 0

    central.down = True  # Central drops for longer than the former permit window.
    node_ms["v"] += 40_000
    registry.clock.advance(40)
    prepared.write_text(json.dumps({"command_sha256": command.command_sha256,
                                    "operation_id": str(command.operation_id)}))
    try:
        runner.tick()
    except OSError:
        pass  # The exchange failed; the local switch did not wait for it.
    assert driver.stop_calls == 1 and driver.starts == [command.target]
    pending = [parse_app_effect_event(raw.encode()).phase for raw in runner.broker.record["pending"]]
    assert pending == ["intent_stop", "stopped", "starting_new", "running"]

    central.down = False
    runner.tick()
    assert not runner.broker.record["pending"] and not runner.broker.record["response_pending"], central.log
    assert all(status == 200 for _, status, _ in central.log), central.log
    status = fixture.service.status(DEVICE_ID)["operations"][0]
    assert status["state"] == "target_running" and status["latest_effect"]["sequence"] == 4
    assert status["command_response"]["decision"] == "accepted"
    # Nothing is held: the next stage is admitted at once from fresh output evidence.
    fixture.refresh_display(60_000)
    following = fixture.stage(1, session_id=session.grant.session_id)
    assert fixture.desired(session.claim) == [following]
    assert [item["state"] for item in fixture.service.status(DEVICE_ID)["operations"]] == [
        "staged", "target_running"]
