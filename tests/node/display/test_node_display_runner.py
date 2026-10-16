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
from appliance.display_host.bus import DISPLAY_SLICE
from appliance.display_host.domain import OutputState
from appliance.display_host.runner import Controller, OutputReporter
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


class Reports:
    """A ReportSink that keeps what was put."""

    def __init__(self) -> None:
        self.puts: list = []

    def put_report(self, report) -> None:
        self.puts.append(report)

    def emit_attempt(self, attempt) -> None:
        raise AssertionError("D1 emits no power attempt")


def test_the_controller_reports_outputs_at_start_and_after_each_hotplug():
    backend = FakeBackend()
    host = backend.host
    backend.states = (OutputState(key(host, "HDMI-A-1"), connected=False),)
    sink = Reports()
    controller = Controller(backend, reports=OutputReporter(sink, read=lambda output_id: None))
    assert [(r.output_id, r.connected) for r in sink.puts] == [("HDMI-A-1", False)]
    plugged = OutputKey(host.boot_id, host.incarnation_id, "HDMI-A-1", 3, 3)
    backend.states = (OutputState(plugged),)
    backend.queue.append(plugged)                      # Weston's hotplug event
    controller.observe()
    assert [(r.output_id, r.connected) for r in sink.puts] == [("HDMI-A-1", False), ("HDMI-A-1", True)]


def test_main_starts_the_display_session_without_the_central_config(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(runner, "_unit", lambda unit: {"MainPID": "1"})
    monkeypatch.setattr(runner, "display_session",
                        lambda digest: SimpleNamespace(start=lambda: started.append(digest)))
    monkeypatch.setattr(sys, "argv", ["display-controller", "--runtime", str(tmp_path)])
    monkeypatch.setattr(os, "umask", lambda mask: 0)
    with pytest.raises(OSError):                       # no compositor control socket here
        runner.main()
    assert started == [DISPLAY_SLICE.digest]


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


def test_the_base_declares_the_display_feed_directory_and_group():
    from node.launcher_closures import closure

    modules = set(closure("display-controller").modules)
    assert {"appliance.feed", "appliance.feed_socket"} <= modules
    assert not [module for module in modules if module.startswith("appliance.node")]
    tmpfiles = (REPO / "debian/photo-wall-node.tmpfiles").read_text().splitlines()
    assert "d /run/photo-wall-display-feed 0750 pw-display pw-node-feeds -" in tmpfiles
    users = (REPO / "debian/photo-wall-node.sysusers").read_text().splitlines()
    # The group comes from the controller's unit only: Weston (PAMName=login) and the overlay
    # client would inherit a sysusers membership (E-AP1-2).
    assert not [line for line in users if line.startswith("m pw-display ")]
    unit = (REPO / "appliance/systemd/photo-wall-display-controller.service").read_text()
    lines = unit.splitlines()
    (groups,) = [line for line in lines if line.startswith("SupplementaryGroups=")]
    assert "pw-node-feeds" in groups.split("=", 1)[1].split()
    assert any(line.startswith("ReadWritePaths=")
               and "/run/photo-wall-display-feed" in line.split("=", 1)[1].split()
               for line in lines)


def test_the_display_paths_are_the_ones_the_base_units_and_tmpfiles_declare():
    """appliance.kernel.display_paths is the one home the broker and the controller read; the
    packaged units and tmpfiles.d must say the same, so a unit edit cannot strand a reader."""
    from appliance.kernel import display_paths as paths

    units = REPO / "appliance/systemd"
    display = (units / paths.DISPLAY_UNIT).read_text().splitlines()
    (execstart,) = [line for line in display if line.startswith("ExecStart=")]
    arguments = execstart.split()
    assert f"XDG_RUNTIME_DIR={paths.RUNTIME}" in arguments
    assert f"--socket={paths.WAYLAND_SOCKET}" in arguments
    assert f"RuntimeDirectory={paths.RUNTIME.name}" in display
    (writable,) = [line for line in display if line.startswith("ReadWritePaths=")]
    assert {str(paths.RUNTIME), str(paths.WAYLAND_DIRECTORY)} <= set(writable.split("=", 1)[1].split())
    tmpfiles = (REPO / "debian/photo-wall-node.tmpfiles").read_text().splitlines()
    assert f"d {paths.WAYLAND_DIRECTORY} 0750 pw-display pw-display -" in tmpfiles
    controller = (units / "photo-wall-display-controller.service").read_text().splitlines()
    assert f"BindsTo={paths.DISPLAY_UNIT}" in controller
    assert f"After={paths.DISPLAY_UNIT}" in controller
    assert {f"RuntimeDirectory={paths.CONTROLLER_STATE.name}", "RuntimeDirectoryPreserve=yes"} <= set(controller)


def test_the_signal_range_request_and_its_units_say_the_same():
    """Display tuning (decision 0024), acceptance 8: the privileged step before Weston, the path
    unit watching the request the controller writes, the oneshot that restarts Weston without
    spending its start limit, and the target that wants the path unit. Mutation probe: rename the
    request file in the path unit -> fails."""
    from node.launcher_closures import entries

    from appliance.display_host import signal_range
    from appliance.kernel import display_paths as paths

    units = REPO / "appliance/systemd"
    display = (units / paths.DISPLAY_UNIT).read_text().splitlines()
    assert "ExecStartPre=+-/usr/bin/python3 -I -B /usr/lib/photo-wall/node/display-signal-range" in display
    assert entries()["display-signal-range"] == signal_range.__name__
    assert f"TTYPath={signal_range.TTY}" in display               # the tty the step blanks is Weston's
    assert paths.SIGNAL_RANGE_REQUEST.parent == paths.CONTROLLER_STATE
    watch = (units / "photo-wall-display-signal.path").read_text().splitlines()
    assert f"PathChanged={paths.SIGNAL_RANGE_REQUEST}" in watch
    assert f"Unit={paths.SIGNAL_RANGE_UNIT}" in watch
    oneshot = (units / paths.SIGNAL_RANGE_UNIT).read_text().splitlines()
    assert "Type=oneshot" in oneshot
    # A range change restarts the display unit and every unit bound to it (the controller): each
    # one's start count is flushed first, so no run of range changes spends any unit's crash start
    # limit. Derived from the units, so a new BindsTo= unit fails here until the oneshot resets it.
    bound = sorted(unit.name for unit in units.glob("*.service")
                   if f"BindsTo={paths.DISPLAY_UNIT}" in unit.read_text().splitlines())
    assert bound == ["photo-wall-display-controller.service"]
    assert [line for line in oneshot if line.startswith("ExecStart")] == [
        f"ExecStart=-/usr/bin/systemctl reset-failed {' '.join([paths.DISPLAY_UNIT, *bound])}",
        f"ExecStart=/usr/bin/systemctl --no-block restart {paths.DISPLAY_UNIT}"]
    target = (units / "photo-wall-node.target").read_text().splitlines()
    (wants,) = [line for line in target if line.startswith("Wants=")]
    assert "photo-wall-display-signal.path" in wants.split("=", 1)[1].split()
    installs = {tuple(line.split()) for line in (REPO / "debian/photo-wall-node.install").read_text().splitlines()}
    assert {("appliance/launchers/display-signal-range", "usr/lib/photo-wall/node"),
            ("appliance/systemd/photo-wall-*.path", "usr/lib/systemd/system")} <= installs


# -- power (1b D2) ----------------------------------------------------------------------------


def test_the_controller_unit_reaches_the_cec_i2c_and_drm_devices_and_nothing_else():
    lines = (REPO / "appliance/systemd/photo-wall-display-controller.service").read_text().splitlines()
    assert "PrivateDevices=yes" not in lines
    assert [line for line in lines if line.startswith(("DevicePolicy=", "DeviceAllow="))] == [
        "DevicePolicy=closed", "DeviceAllow=char-cec rw", "DeviceAllow=char-i2c rw",
        "DeviceAllow=char-drm rw"]           # drm: the connector's signal range, read (display tuning)
    (groups,) = [line for line in lines if line.startswith("SupplementaryGroups=")]
    # video: /dev/cecN (root:video 0660); i2c: /dev/i2c-N (root:i2c 0660, E-1B-D2-1). Without the
    # group DeviceAllow=char-i2c opens nothing and DDC/CI never answers.
    assert {"pw-node-feeds", "video", "i2c"} <= set(groups.split("=", 1)[1].split())
    # A group the root lacks fails the unit at start (216/GROUP): the base declares every one the
    # unit names beyond the distribution's own (video).
    users = (REPO / "debian/photo-wall-node.sysusers").read_text().splitlines()
    declared = {line.split()[1] for line in users if line.startswith(("g ", "u "))}
    assert set(groups.split("=", 1)[1].split()) - {"video"} <= declared


def test_power_reports_carry_what_the_output_reporter_saw():
    """The power controller is the one writer of the report: the EDID half from the reporter,
    the power half from its own looks."""
    from appliance.display_host.output_power import OutputPowerController
    from appliance.display_host.runner import PowerObservations

    sink = Reports()
    power = OutputPowerController({}, sink, SimpleNamespace(monotonic=lambda: 0.0, time=lambda: 0.0,
                                                          wait=lambda condition, seconds: condition.wait(seconds)),
                                  SimpleNamespace(show=lambda *args, **kwargs: True, shown=lambda output_id: False))
    seen = []
    picture = SimpleNamespace(output=lambda output_id, **observed: seen.append((output_id, observed)))
    backend = FakeBackend()
    backend.states = (OutputState(key(backend.host, "HDMI-A-1")),)
    Controller(backend, reports=OutputReporter(PowerObservations(power, picture), read=lambda output_id: None))
    (report,) = sink.puts
    assert (report.output_id, report.connected, report.answers, report.method) == ("HDMI-A-1", True, (), None)
    assert seen == [("HDMI-A-1", {"connected": True, "identity": None})]   # the picture controller saw it too


def test_one_ddc_channel_feeds_both_the_power_and_the_picture_adapters(tmp_path):
    """One DDC/CI channel per Pi: the power controller's DDC/CI adapter and the picture
    controller's adapter both run ddcutil through it (so one port lock serialises them), and the
    backend hands the shell's picture events to the picture controller."""
    import subprocess

    from appliance.display_host.ddc import DdcChannel
    from contracts.node_output import PowerMethod

    (tmp_path / "card1-HDMI-A-1").mkdir()
    (tmp_path / "card1-HDMI-A-1" / "ddc").symlink_to(tmp_path / "i2c-13")
    ran = []

    def run(argv, timeout):
        ran.append(list(argv))
        return subprocess.CompletedProcess(argv, 1, "", "")

    ddc = DdcChannel(run, drm_root=tmp_path, cache_dir=tmp_path / "cache")
    backend = SimpleNamespace(picture_events=None)
    power, _cec = runner.power_controller(backend, Reports(), ddc)
    picture = runner.picture_controller(backend, SimpleNamespace(put_picture=lambda report: None), ddc,
                                        applied=tmp_path)
    power._adapters[PowerMethod.DDC_CI].read("HDMI-A-1", timeout=1)
    picture._ddc.read("HDMI-A-1", "10", timeout=1)
    assert picture._ask.ask("HDMI-A-1") == ()        # every batch finished, none answered: offers none
    assert [argv[1:] for argv in ran][:3] == [["--bus", "13", "--noconfig", "getvcp", "D6", "--terse"],
                                              ["--bus", "13", "--noconfig", "getvcp", "10", "--terse"],
                                              ["--bus", "13", "--noconfig", "getvcp", "0C", "10", "12", "14", "--terse"]]
    assert backend.picture_events is picture
    # The signal range's restart takes the same channel's ports; its request lives beside the
    # applied pictures (display_paths.SIGNAL_RANGE_REQUEST's name).
    from appliance.kernel.display_paths import SIGNAL_RANGE_REQUEST

    assert picture._signal._ddc is ddc
    assert picture._signal._requests._path == tmp_path / SIGNAL_RANGE_REQUEST.name


@pytest.mark.skipif(not LINUX, reason="the shell's control socket is SOCK_SEQPACKET with SO_PEERCRED (Linux)")
def test_the_backend_asks_the_shell_for_output_power_from_another_thread():
    """WestonBackend over a real SEQPACKET pair, the far end playing the shell's protocol: the
    power worker's `output_power` gets the shell's `output_power` event, while the main loop's
    dispatch never blocks on an event the worker's request already took."""
    from appliance.display_host.weston import WestonBackend
    from contracts.node_output import Power

    ours, shell = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    boot, incarnation = uuid4(), uuid4()
    output = {"kernel_boot_id": str(boot), "display_host_incarnation": str(incarnation),
              "output_id": "HDMI-A-1", "connection_generation": 1, "mode_generation": 1}

    def send(value):
        shell.send(json.dumps(value).encode())

    send({"event": "hello", "protocol": 1, "boot_id": str(boot), "incarnation_id": str(incarnation)})
    send({"event": "output", "output": output, "connected": True, "width": 640, "height": 480,
          "power": "on"})
    asked = []

    def play_shell():
        request = json.loads(shell.recv(16384))
        asked.append(request)
        send({"event": "output_power", "output_id": request["output_id"], "power": request["power"]})
        send({"event": "response", "request_id": request["request_id"], "accepted": True})

    with ours, shell:
        backend = WestonBackend(ours, display_uid=os.getuid(), compositor_pid=os.getpid(),
                                verify_process=lambda grant: True)
        backend.initialize()
        assert backend.output_powered("HDMI-A-1") is None          # not seen yet
        assert backend.dispatch() == OutputKey(boot, incarnation, "HDMI-A-1", 1, 1)
        assert backend.output_powered("HDMI-A-1") is Power.ON
        playing = threading.Thread(target=play_shell)
        playing.start()
        done = []
        worker = threading.Thread(target=lambda: done.append(
            backend.output_power("HDMI-A-1", Power.OFF, timeout=2)))
        worker.start()
        worker.join(5)
        playing.join(5)
        assert done == [True] and backend.output_powered("HDMI-A-1") is Power.OFF
        assert (asked[0]["op"], asked[0]["power"], asked[0]["output"]) == ("output_power", "off", output)
        assert backend.dispatch() is None                           # nothing left: no block
        assert backend.output_power("HDMI-A-2", Power.OFF, timeout=1) is False   # no such Output


def test_the_document_feed_routes_each_document_by_its_key_and_ignores_the_rest():
    """`output-…` to the power controller, `picture-…` to the picture controller (decision 0022);
    a document under another Output's key, under a key of neither kind, or that does not decode
    reaches neither."""
    from appliance.display_host.runner import document_feed
    from contracts.node_output import (
        BEST_DETECTED,
        Darkness,
        OutputDocument,
        OutputPower,
        Power,
        encode_output_document,
    )
    from contracts.node_picture import (
        NEUTRAL_SOFTWARE,
        UPGRADE_DISPLAY_LIGHT,
        PictureDocument,
        encode_picture_document,
    )
    from nodeapi.documents import Document

    got, pictures = [], []
    feed = document_feed(SimpleNamespace(document=lambda output_id, document: got.append((output_id, document))),
                         SimpleNamespace(document=lambda output_id, document: pictures.append((output_id, document))))
    wanted = OutputDocument("HDMI-A-1", 2, OutputPower(Power.ON, (), "2099-01-01T00:00:00Z", Darkness.DISPLAY_OFF, None),
                            BEST_DETECTED, False, False)
    picture = PictureDocument("HDMI-A-1", 3, {"10": 46}, NEUTRAL_SOFTWARE, light=UPGRADE_DISPLAY_LIGHT)
    feed("output-HDMI-A-1", Document(encode_output_document(wanted), "central", None))
    feed("output-HDMI-A-2", Document(encode_output_document(wanted), "central", None))   # another key
    feed("output-HDMI-A-1", Document(b'{"output_id":"HDMI-A-1"}', "central", None))       # undecodable
    feed("picture-HDMI-A-1", Document(encode_picture_document(picture), "central", None))
    feed("picture-HDMI-A-2", Document(encode_picture_document(picture), "central", None))  # another key
    feed("picture-HDMI-A-1", Document(encode_output_document(wanted), "central", None))    # not a picture
    feed("layout-HDMI-A-1", Document(encode_picture_document(picture), "central", None))   # neither kind
    assert got == [("HDMI-A-1", wanted)]
    assert pictures == [("HDMI-A-1", picture)]
