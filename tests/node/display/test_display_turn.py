"""The screen turn through Display Host's controller (render redesign S1, bead B2):
`WestonBackend.turn` sends the shell's `turn` op with the turn's Wayland transform and answers
whether the shell's `turn` event confirms it, over a real SEQPACKET pair whose far end speaks the
shell's protocol (Linux, as tests/node/display/test_display_black_picture.py). The shell's own
turn (Weston's output transform, the client's wl_output) is proven in the display harness
(tests/native_display_smoke.py).
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
from uuid import uuid4

import pytest

LINUX = sys.platform.startswith("linux")
OUTPUT = "HDMI-A-1"
# contracts/display_turn.py's frozen table: a screen hung turned 90 degrees clockwise is
# wl_output.transform 90 (1), and so on.
TRANSFORMS = {0: 0, 90: 1, 180: 2, 270: 3}

pytestmark = pytest.mark.skipif(
    not LINUX, reason="the shell's control socket is SOCK_SEQPACKET with SO_PEERCRED (Linux)")


class Shell:
    """The far end of the control socket, playing the shell: `hello`, one connected Output, then
    each request answered by `answer(request)` (the events to send ahead of the response) and the
    response (`accepted`)."""

    def __init__(self, end: socket.socket) -> None:
        self.end, self.asked = end, []
        self.boot, self.incarnation = uuid4(), uuid4()
        self.output = {"kernel_boot_id": str(self.boot), "display_host_incarnation": str(self.incarnation),
                       "output_id": OUTPUT, "connection_generation": 1, "mode_generation": 1}
        self.send({"event": "hello", "protocol": 1, "boot_id": str(self.boot),
                   "incarnation_id": str(self.incarnation)})
        self.send({"event": "output", "output": self.output, "connected": True, "width": 640,
                   "height": 480, "power": "on", "black": False})

    def send(self, value: dict) -> None:
        self.end.send(json.dumps(value).encode())

    def play(self, times: int, answer, *, accepted: bool = True) -> threading.Thread:
        def serve() -> None:
            for _ in range(times):
                request = json.loads(self.end.recv(16384))
                self.asked.append(request)
                for event in answer(request):
                    self.send(event)
                self.send({"event": "response", "request_id": request["request_id"],
                           "accepted": accepted})
        thread = threading.Thread(target=serve)
        thread.start()
        return thread


def confirmed(request):
    return [{"event": "turn", "output_id": request["output_id"], "transform": request["transform"]}]


@pytest.fixture
def backend():
    from appliance.display_host.weston import WestonBackend

    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with ours, theirs:
        shell = Shell(theirs)
        subject = WestonBackend(ours, display_uid=os.getuid(), compositor_pid=os.getpid(),
                                verify_process=lambda grant: True)
        subject.initialize()
        yield subject, shell


def test_turn_sends_the_turns_wayland_transform_and_is_true_when_the_shells_event_confirms_it(backend):
    subject, shell = backend
    assert not subject.turn(OUTPUT, 90, timeout=1)              # the shell's Output not seen yet
    assert shell.asked == []
    subject.dispatch()
    playing = shell.play(4, confirmed)
    for turn in (90, 180, 270, 0):
        assert subject.turn(OUTPUT, turn, timeout=2)
    playing.join(5)
    assert [(r["op"], r["output_id"], r["output"], r["transform"]) for r in shell.asked] == [
        ("turn", OUTPUT, shell.output, TRANSFORMS[turn]) for turn in (90, 180, 270, 0)]


def test_turn_is_false_when_the_shell_refuses_or_confirms_another_transform(backend):
    subject, shell = backend
    subject.dispatch()
    refused = shell.play(1, lambda request: [], accepted=False)
    assert not subject.turn(OUTPUT, 90, timeout=2)
    refused.join(5)
    other = shell.play(1, lambda request: [{**confirmed(request)[0], "transform": 0}])
    assert not subject.turn(OUTPUT, 90, timeout=2)              # the screen did not turn
    other.join(5)
    silent = shell.play(1, lambda request: [])                  # accepted, but no `turn` event
    assert not subject.turn(OUTPUT, 270, timeout=2)
    silent.join(5)


def test_turn_is_false_on_a_timeout_and_for_an_output_the_shell_no_longer_has(backend):
    subject, shell = backend
    subject.dispatch()
    assert not subject.turn(OUTPUT, 180, timeout=0.2)           # nobody answers
    assert [r["op"] for r in shell.asked] == []
    shell.end.recv(16384)                                       # the unanswered request
    shell.send({"event": "output", "output": {**shell.output, "connection_generation": 2,
                                              "mode_generation": 2},
                "connected": False, "width": 0, "height": 0, "power": "on", "black": False})
    subject.dispatch()
    assert not subject.turn(OUTPUT, 90, timeout=1)


def test_a_turn_event_outside_a_request_is_taken_not_dispatched(backend):
    """The shell's `turn` event is the op's answer, never a Display Host event: one that arrives
    while no turn is asked (a late answer) is kept, not raised as an unexpected event."""
    subject, shell = backend
    subject.dispatch()
    shell.send({"event": "turn", "output_id": OUTPUT, "transform": 1})
    assert subject.dispatch() is None
