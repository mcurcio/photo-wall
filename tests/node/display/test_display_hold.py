"""The shell's hold, as Display Host hears it (issue #123): a `hold` event becomes a `HoldFact` and
the Output's `holding`, which the feed snapshot carries to the health judge.

The shell's side (a ground-only app loses the screen to the holding slate; a picture plane, a
pattern or a trial keeps it) is held on a real headless Weston by tests/native_screen_harness.py.
Here the shell is played over the control socket's SOCK_SEQPACKET pair, as
test_display_picture_controls.py plays it for the `picture` op.
"""
from __future__ import annotations

import json
import os
import socket
import sys
from uuid import uuid4

import pytest

from appliance.display_host.weston import HoldFact, WestonBackend

OUTPUT = "HDMI-A-1"

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="the shell's control socket is SOCK_SEQPACKET with SO_PEERCRED (Linux)")


@pytest.fixture
def shell():
    """(backend, send, output key document): a backend initialized against a played shell that
    announced OUTPUT connected."""
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    boot, incarnation = uuid4(), uuid4()
    output = {"kernel_boot_id": str(boot), "display_host_incarnation": str(incarnation),
              "output_id": OUTPUT, "connection_generation": 1, "mode_generation": 1}

    def send(value):
        theirs.send(json.dumps(value).encode())

    send({"event": "hello", "protocol": 1, "boot_id": str(boot), "incarnation_id": str(incarnation)})
    send({"event": "output", "output": output, "connected": True, "width": 640, "height": 480,
          "power": "on"})
    with ours, theirs:
        backend = WestonBackend(ours, display_uid=os.getuid(), compositor_pid=os.getpid(),
                                verify_process=lambda grant: True)
        backend.initialize()
        backend.dispatch()
        yield backend, send, output


def test_a_hold_event_is_a_hold_fact_and_the_outputs_holding(shell):
    backend, send, output = shell
    assert backend.host.state(OUTPUT).holding is False
    send({"event": "hold", "output": output, "holding": True, "evidence": "picture_plane"})
    assert backend.dispatch() == HoldFact(OUTPUT, True, "picture_plane")
    assert backend.host.state(OUTPUT).holding is True
    send({"event": "hold", "output": output, "holding": False, "evidence": None})
    assert backend.dispatch() == HoldFact(OUTPUT, False, None)
    assert backend.host.state(OUTPUT).holding is False
    send({"event": "hold", "output": output, "holding": True, "evidence": "display_drawing"})
    assert backend.dispatch() == HoldFact(OUTPUT, True, "display_drawing")


def test_a_hold_of_a_replaced_output_key_is_ignored(shell):
    backend, send, output = shell
    replaced = {**output, "connection_generation": 2, "mode_generation": 2}   # a mode change
    send({"event": "output", "output": replaced, "connected": True, "width": 480, "height": 640,
          "power": "on"})
    backend.dispatch()
    send({"event": "hold", "output": output, "holding": True, "evidence": "picture_plane"})
    assert backend.dispatch() is None and backend.host.state(OUTPUT).holding is False


@pytest.mark.parametrize(("holding", "evidence"), [
    (True, None), (False, "picture_plane"), (1, "picture_plane"), (True, "ground")])
def test_a_malformed_hold_is_refused(shell, holding, evidence):
    backend, send, output = shell
    send({"event": "hold", "output": output, "holding": holding, "evidence": evidence})
    with pytest.raises(ValueError, match="^display_message_invalid$"):
        backend.dispatch()
    assert backend.host.state(OUTPUT).holding is False
