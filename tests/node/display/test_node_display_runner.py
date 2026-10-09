"""The display controller publishes its observations through the shared node feed, with an
`outputs` snapshot, to the root ingress and to the node feed socket (root and pw-health)."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from support.repo import REPO

from appliance import feed_socket
from appliance.display_host import runner
from appliance.display_host.domain import OutputState
from appliance.display_host.runner import Controller
from appliance.display_host.weston import MAX_PACKET, CompositorPresentation
from contracts.node_display import Surface
from contracts.node_protocol import NodeProcessIdentity, OutputKey

LINUX = sys.platform.startswith("linux")
KIND = socket.SOCK_SEQPACKET if LINUX else socket.SOCK_STREAM


@dataclass(frozen=True)
class Observed:
    index: int


class FakeBackend:
    def __init__(self, states: tuple[OutputState, ...] = ()):
        self.states = states
        self.host = SimpleNamespace(boot_id=uuid4(), incarnation_id=uuid4(),
                                    states=lambda: self.states)
        self.queue: list[object] = []

    def initialize(self):
        return self.host

    def dispatch(self):
        return self.queue.pop(0)


def controller_with(count: int) -> Controller:
    backend = FakeBackend()
    controller = Controller(backend)
    backend.queue.extend(Observed(index) for index in range(count))
    backend.queue.insert(0, None)  # An inert dispatch publishes nothing.
    for _ in range(count + 1):
        controller.observe()
    return controller


def test_events_op_pages_the_feed_and_keeps_compositor_identity():
    controller = controller_with(3)
    page = controller.receive({"op": "events", "after": 0})
    assert [(e["sequence"], e["kind"], e["value"]) for e in page["events"]] == [
        (1, "Observed", {"index": 0}),
        (2, "Observed", {"index": 1}),
        (3, "Observed", {"index": 2}),
    ]
    assert page["stream_gap"] is False
    assert page["boot_id"] == controller.host.boot_id
    assert page["incarnation_id"] == controller.host.incarnation_id
    assert page["publisher_incarnation"] == str(controller.feed.incarnation)


def test_overflow_gap_is_per_read_not_sticky():
    controller = controller_with(300)
    incarnation = str(controller.feed.incarnation)
    behind = controller.receive({"op": "events", "after": 0, "incarnation": incarnation})
    assert behind["stream_gap"] is True and behind["events"][0]["sequence"] == 45
    assert behind["dropped_total"] == 44
    caught_up = controller.receive({"op": "events", "after": 300, "incarnation": incarnation})
    assert caught_up["stream_gap"] is False and caught_up["events"] == []


def test_restarted_controller_reports_gap_to_an_old_incarnation():
    controller = controller_with(2)
    page = controller.receive({"op": "events", "after": 40, "incarnation": str(uuid4())})
    assert page["stream_gap"] is True and [e["sequence"] for e in page["events"]] == [1, 2]


def test_events_op_refuses_a_bad_cursor():
    with pytest.raises(ValueError, match="feed_read_request"):
        controller_with(0).receive({"op": "events", "after": -1})


# -- the outputs snapshot ------------------------------------------------------------------


def key(host, output_id="Virtual-1") -> OutputKey:
    return OutputKey(host.boot_id, host.incarnation_id, output_id, 2, 3)


def surface(output: OutputKey, *, frame_id="frame-1", pid=4242) -> Surface:
    return Surface(output, NodeProcessIdentity(pid, 777, uuid4()), 5, 1, 2, frame_id)


def test_every_events_read_carries_each_output_and_its_admission():
    backend = FakeBackend()
    controller = Controller(backend)
    host = backend.host
    admitted = surface(key(host))
    backend.states = (
        OutputState(key(host), admitted=admitted, diagnostic="released", fault=None),
        OutputState(key(host, "HDMI-A-2"), connected=False, diagnostic="unknown",
                    fault="output_disconnected"),
    )
    expected = [
        {"output_id": "Virtual-1", "connected": True,
         "admitted": {"pid": 4242, "start_ticks": 777,
                      "invocation_id": str(admitted.process.invocation_id),
                      "app_epoch": 5, "frame_id": "frame-1"},
         "diagnostic": "released", "fault": None},
        {"output_id": "HDMI-A-2", "connected": False, "admitted": None,
         "diagnostic": "unknown", "fault": "output_disconnected"},
    ]
    # Ingress and feed socket alike; with no event at all (a reader that lapped the ring, or a
    # restarted judge, still learns the Output set from one read).
    for page in (controller.receive({"op": "events", "after": 0}),
                 controller.feed_events({"after": 0, "incarnation": None})):
        assert page["events"] == [] and page["outputs"] == expected
    assert json.loads(json.dumps(expected)) == expected  # JSON-native: no UUID objects


def test_the_feed_socket_answers_events_and_nothing_else():
    controller = controller_with(2)
    page = controller.feed_events({"op": "events", "after": 0, "incarnation": None})
    assert [e["sequence"] for e in page["events"]] == [1, 2] and page["outputs"] == []
    for operation in ("outputs", "candidate", "withdraw", "handoff", "unknown", None):
        with pytest.raises(ValueError, match="^feed_read_request$"):
            controller.feed_events({"op": operation, "after": 0})


def admitted_states(host, count):
    return tuple(
        OutputState(key(host, f"{'O' * 120}-{index:02d}"),
                    admitted=surface(key(host, f"{'O' * 120}-{index:02d}"),
                                     frame_id="F" * 96, pid=2**31 - 1),
                    diagnostic="released", fault="surface_lease_or_process_lost")
        for index in range(count)
    )


def worst_page(count):
    """`count` admitted Outputs with maximal identifiers and a full page of presentations."""
    backend = FakeBackend()
    controller = Controller(backend)
    backend.states = admitted_states(backend.host, count)
    for state in backend.states:
        fact = state.admitted.fact("presented_to_compositor", "B" * 128)
        controller.feed.append("CompositorPresentation",
                               asdict(CompositorPresentation(fact, uuid4(), "T" * 96, 2**40)))
    return controller


@pytest.fixture
def display_feed(tmp_path):
    def make(controller):
        directory = tmp_path / "display-feed"
        directory.mkdir(mode=0o750)
        uid = {"value": 0}
        served = runner.feed_listener(controller, directory / "feed.sock", group=os.getgid(),
                                      peer=lambda connection: uid["value"], kind=KIND)
        made.append(served)
        return served, uid

    made = []
    yield make
    for served in made:
        served.close()


def read(served, request: bytes, *, refused=False):
    client = socket.socket(socket.AF_UNIX, served.listener.type)
    client.settimeout(2)
    try:
        client.connect(str(served.path))
        client.sendall(request)
        # Served while the client reads: the macOS stream stand-in buffers less than a packet.
        serving = threading.Thread(target=served.serve)
        serving.start()
        raw = b""
        try:
            # One packet on Linux (SEQPACKET); the stream stand-in may split it.
            while chunk := client.recv(1 << 20):
                raw += chunk
                if client.type == socket.SOCK_SEQPACKET:
                    break
        except ConnectionResetError:  # closed unread (E-B7-6): no byte either way
            if not refused:
                raise
        serving.join()
    finally:
        client.close()
    return raw


def test_only_root_and_the_judge_read_the_display_feed(display_feed):
    served, uid = display_feed(controller_with(3))
    assert served.readers == frozenset({0, 10006})
    request = b'{"op":"events","after":0,"incarnation":null}'
    for reader in (0, 10006):
        uid["value"] = reader
        page = json.loads(read(served, request))
        assert page["accepted"] is True and [e["sequence"] for e in page["events"]] == [1, 2, 3]
        assert "outputs" in page and page["incarnation_id"]
    # pw-display itself (the overlay client and Weston), the Player, the Manager, nobody.
    for other in (10005, 10004, 10003, 65534, os.getuid() if os.getuid() else 1000):
        uid["value"] = other
        assert read(served, request, refused=True) == b""


def test_the_display_feed_refuses_other_operations_and_bad_cursors(display_feed):
    served, _ = display_feed(controller_with(1))
    for request in (b'{"op":"outputs"}', b'{"op":"handoff","surface":{}}',
                    b'{"op":"events","after":-1}', b'{"op":"events"}'):
        assert json.loads(read(served, request)) == {"accepted": False,
                                                     "reason": "feed_read_request"}


def test_a_full_page_with_every_output_admitted_fits_one_packet(display_feed):
    controller = worst_page(16)  # DisplayHost's shipped max_outputs
    served, _ = display_feed(controller)
    wire = read(served, b'{"op":"events","after":0,"incarnation":null}')
    page = json.loads(wire)
    assert page["accepted"] is True and len(page["outputs"]) == 16 and len(page["events"]) == 8
    assert len(wire) <= MAX_PACKET


def test_an_oversized_reply_is_response_bound(display_feed):
    served, _ = display_feed(worst_page(40))
    wire = read(served, b'{"op":"events","after":0,"incarnation":null}')
    assert json.loads(wire) == {"accepted": False, "reason": "response_bound"}


def test_the_controller_serves_the_kernel_listener_at_the_display_feed_path():
    assert runner.FeedListener is feed_socket.FeedListener
    assert runner.FEED_SOCKET == Path("/run/photo-wall-display-feed/feed.sock")
    assert runner.feed_listener.__defaults__ == (runner.FEED_SOCKET,)
    assert runner.feed_listener.__kwdefaults__ == {"owner_uid": None,
                                                   "group": feed_socket.FEEDS_GROUP}


# -- packaging -----------------------------------------------------------------------------


def test_the_base_declares_the_display_feed_directory_and_group(tmp_path):
    from scripts.build_node_base_deb import POLICIES, stage_tree
    from scripts.module_closure import closure_for

    modules = set(closure_for(POLICIES["display-controller"], repo=REPO).modules)
    assert {"appliance.feed", "appliance.feed_socket"} <= modules
    assert not [module for module in modules if module.startswith("appliance.node")]
    root = tmp_path / "package"
    stage_tree(REPO, root)
    tmpfiles = (root / "usr/lib/tmpfiles.d/photo-wall-node.conf").read_text().splitlines()
    assert "d /run/photo-wall-display-feed 0750 pw-display pw-node-feeds -" in tmpfiles
    users = (root / "usr/lib/sysusers.d/photo-wall-node.conf").read_text().splitlines()
    # The group comes from the controller's unit only: Weston (PAMName=login) and the overlay
    # client would inherit a sysusers membership (E-AP1-2).
    assert not [line for line in users if line.startswith("m pw-display ")]
    unit = (root / "lib/systemd/system/photo-wall-display-controller.service").read_text()
    lines = unit.splitlines()
    assert "SupplementaryGroups=pw-node-feeds" in lines
    assert any(line.startswith("ReadWritePaths=")
               and "/run/photo-wall-display-feed" in line.split("=", 1)[1].split()
               for line in lines)


def test_the_display_paths_are_the_ones_the_base_units_and_tmpfiles_declare(tmp_path):
    """appliance.kernel.display_paths is the one home the broker and the controller read; the
    packaged units and tmpfiles.d must say the same, so a unit edit cannot strand a reader."""
    from appliance.kernel import display_paths as paths
    from scripts.build_node_base_deb import stage_tree

    root = tmp_path / "package"
    stage_tree(REPO, root)
    units = root / "lib/systemd/system"
    display = (units / paths.DISPLAY_UNIT).read_text().splitlines()
    (execstart,) = [line for line in display if line.startswith("ExecStart=")]
    arguments = execstart.split()
    assert f"XDG_RUNTIME_DIR={paths.RUNTIME}" in arguments
    assert f"--socket={paths.WAYLAND_SOCKET}" in arguments
    assert f"RuntimeDirectory={paths.RUNTIME.name}" in display
    (writable,) = [line for line in display if line.startswith("ReadWritePaths=")]
    assert {str(paths.RUNTIME), str(paths.WAYLAND_DIRECTORY)} <= set(writable.split("=", 1)[1].split())
    tmpfiles = (root / "usr/lib/tmpfiles.d/photo-wall-node.conf").read_text().splitlines()
    assert f"d {paths.WAYLAND_DIRECTORY} 0750 pw-display pw-display -" in tmpfiles
    controller = (units / "photo-wall-display-controller.service").read_text().splitlines()
    assert f"BindsTo={paths.DISPLAY_UNIT}" in controller
    assert f"After={paths.DISPLAY_UNIT}" in controller
