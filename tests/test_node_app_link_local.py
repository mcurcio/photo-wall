"""The broker's probe socket (`appliance.apps.probe_listener.ProbeListener`), the listener half of
the retired app link (decision 0034, S4c): its one admitted first packet is `probe_open` from the
app uid whose pid is the current run's, handed to the probe thread; anything else (the old app
link's `begin` among them) is refused locally with a bare `refused`, and the journal names why.
No Central, no session, no outbox.
"""
import socket
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_node_boot import environment

from appliance.apps import probe_listener
from appliance.apps.broker import RunningApp
from appliance.apps.probe import AppRunKey
from appliance.apps.probe_listener import ProbeListener
from contracts.node_app_link import encode_node_app_link_result, encode_node_probe_open
from contracts.node_protocol import NodeProcessIdentity

PID, APP = 396, 10004
REFUSED = encode_node_app_link_result("refused")   # {"schema": 2, "kind": "result", "status": "refused"}
# The old app link's first packet: a Player of a release before this one would send it.
BEGIN = (b'{"authority_epoch":1,"control_receipt":"{\\"authority_epoch\\":1}","kind":"begin",'
         b'"player_id":"p-' + b"a" * 32 + b'","schema":2}')


class Connection:
    def __init__(self):
        self.sent, self.closed = [], False

    def setsockopt(self, *args):
        pass

    def settimeout(self, value):
        pass

    def send(self, packet):
        self.sent.append(packet)
        return len(packet)

    def close(self):
        self.closed = True


def listener(monkeypatch, first, *, running=True):
    """A ProbeListener whose one accepted connection's first packet is `first` ((pid, uid, gid),
    raw), or whose receive raises `first` when it is an exception."""
    run = RunningApp(environment("a"), NodeProcessIdentity(PID, 31751781, uuid4()), 1, uuid4())
    connection, adopted = Connection(), []

    def receive(*args, **kwargs):
        if isinstance(first, BaseException):
            raise first
        return first

    monkeypatch.setattr(socket, "SO_PASSCRED", 16, raising=False)  # Linux's; macOS lacks it
    monkeypatch.setattr(probe_listener, "receive_credential_packet", receive)
    served = ProbeListener.__new__(ProbeListener)
    served.runs = SimpleNamespace(current=lambda: run if running else None)
    served.probes = SimpleNamespace(adopt=lambda c, key: adopted.append((c, key)))
    served.listener = SimpleNamespace(accept=lambda: (connection, None))
    return served, connection, adopted, run


def test_probe_open_from_the_current_runs_pid_is_handed_to_the_probe_thread(monkeypatch):
    served, connection, adopted, run = listener(monkeypatch, ((PID, APP, APP), encode_node_probe_open()))
    served.serve_one()
    assert adopted == [(connection, AppRunKey.of(run))]
    assert connection.sent == [] and not connection.closed  # no reply; the thread owns it now


@pytest.mark.parametrize("first", [
    ((PID + 1, APP, APP), encode_node_probe_open()),   # the app uid, not the current run's pid
    ((PID, 10003, 10003), encode_node_probe_open()),   # not the app uid
    ((PID, APP, APP), BEGIN),                          # the retired app link's first packet
    ((PID, APP, APP), b"not json"),
])
def test_anything_but_the_current_runs_probe_open_is_refused(monkeypatch, first):
    """Mutation probe: admit any pid of the app uid (drop the pid check) -> the first case fails."""
    served, connection, adopted, _ = listener(monkeypatch, first)
    served.serve_one()
    assert adopted == [] and connection.sent == [REFUSED] and connection.closed


def test_probe_open_with_no_running_app_is_refused(monkeypatch):
    served, connection, adopted, _ = listener(monkeypatch, ((PID, APP, APP), encode_node_probe_open()),
                                              running=False)
    served.serve_one()
    assert adopted == [] and connection.sent == [REFUSED] and connection.closed


@pytest.mark.parametrize(("first", "logged"), [
    (((PID + 1, APP, APP), encode_node_probe_open()), "node_link_peer"),
    (((PID, APP, APP), BEGIN), "probe_channel_message"),
    # Not a bare code (raw text, a path): the journal gets the exception type alone.
    (OSError(5, "I/O error", "/run/photo-wall/secret"), "OSError"),
    (ValueError("Expecting value: line 1 column 1"), "ValueError"),
])
def test_a_local_refusal_is_logged_with_its_code(monkeypatch, caplog, first, logged):
    """The peer gets a bare `refused`; the journal names why, and never raw exception text.
    Mutation probes: drop the log; log the message whether or not it is a code."""
    served, connection, _, _ = listener(monkeypatch, first)
    with caplog.at_level("WARNING", logger=probe_listener.__name__):
        served.serve_one()
    assert connection.sent == [REFUSED]
    assert caplog.messages == [f"broker: probe channel refused locally ({logged})"]


def test_an_idle_listener_returns_at_once(monkeypatch):
    served, connection, adopted, _ = listener(monkeypatch, ((PID, APP, APP), encode_node_probe_open()))
    served.listener = SimpleNamespace(accept=lambda: (_ for _ in ()).throw(TimeoutError()))
    served.serve_one()
    assert adopted == [] and connection.sent == []


def test_the_broker_has_no_app_link_left():
    """The broker's loop serves the probe listener alone: no session, no outbox, no Central."""
    from appliance.apps import broker_runner
    source = broker_runner.__loader__.get_source(broker_runner.__name__)
    for retired in ("NodeSession", "NodeHTTP", "app-link-outbox", "deliver_app_link", "/v2/node/"):
        assert retired not in source, retired
