"""Health judge unit: draining the broker and display feeds by cursor, the root-only `status`
op, the pw-display-only `overlay` op, packaging."""
import json
import os
import socket
import stat
import sys
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from appliance import feed_socket
from appliance.display_host import runner as display_runner
from appliance.display_host.domain import OutputState
from appliance.display_host.overlay.instruction import (
    INSTRUCTION_STALE_MS,
    PresentedReport,
    encode_presented_report,
    parse_overlay_instruction,
)
from appliance.feed import Feed, answer_feed_read
from appliance.health import runner
from appliance.health.judge import APP_UNRESPONSIVE
from appliance.health.runner import (
    DisplayFeedReader,
    FeedReader,
    HealthRunner,
    HealthSocket,
    OverlayClients,
    status_document,
)
from appliance.node.broker_runner import feed_listener
from appliance.node.probe import AppRunKey
from contracts.node_display import Surface
from contracts.node_faults import FAULTS, catalogue_digest
from contracts.node_protocol import NodeProcessIdentity, OutputKey

REPO = Path(__file__).resolve().parents[1]
LINUX = sys.platform.startswith("linux")
KIND = socket.SOCK_SEQPACKET if LINUX else socket.SOCK_STREAM
RUN = AppRunKey(uuid4(), 101, 7, 1).document()
RAISE = FAULTS[APP_UNRESPONSIVE].raise_window_ms


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


def local_reader(feed, *, name="broker"):
    """A FeedReader whose transport is the publisher's own wire handler (no socket)."""
    reader = FeedReader(name, Path("/nonexistent/feed.sock"))
    reader.calls = 0

    def read(request):
        reader.calls += 1
        return {"accepted": True, **answer_feed_read(reader.feed, request)}

    reader.feed = feed
    reader._read = read
    return reader


def snapshot_output(name="Virtual-1", run=RUN, *, connected=True):
    admitted = None if run is None else {**run, "frame_id": "frame-1"}
    return {"output_id": name, "connected": connected, "admitted": admitted,
            "diagnostic": "released", "fault": "app_absent"}


def local_display_reader(feed, outputs):
    """Display's feed as the controller answers it: every page carries `outputs` (B10a)."""
    reader = DisplayFeedReader("display", Path("/nonexistent/display.sock"))
    reader.feed, reader.outputs, reader.calls = feed, outputs, 0

    def read(request):
        reader.calls += 1
        return {"accepted": True, **answer_feed_read(reader.feed, request),
                "outputs": reader.outputs}

    reader._read = read
    return reader


# -- reading the feed ----------------------------------------------------------------------


def test_a_turn_drains_the_feed_while_pages_come_back_full():
    feed = Feed(512)
    for _ in range(100):
        feed.append("probe_answered", {"run": RUN, "rtt_ms": 3})
    reader = local_reader(feed)
    clock = Clock()
    HealthRunner(runner.shipped_judge(), (reader,), clock=clock).turn()
    assert reader.cursor.after == 100 and reader.calls == 13 and reader.gaps == 0


def test_a_turn_reads_at_most_32_pages_and_resumes_next_turn():
    feed = Feed(512)
    for _ in range(300):
        feed.append("probe_answered", {"run": RUN, "rtt_ms": 3})
    reader = local_reader(feed)
    health = HealthRunner(runner.shipped_judge(), (reader,), clock=Clock())
    health.turn()
    assert reader.cursor.after == 256 and reader.calls == 32
    health.turn()
    assert reader.cursor.after == 300 and reader.gaps == 0


def test_starvation_on_the_feed_raises_the_condition():
    feed = Feed(512)
    reader = local_reader(feed)
    clock = Clock()
    health = HealthRunner(runner.shipped_judge(), (reader,), clock=clock)
    feed.append("probe_channel", {"run": RUN, "state": "open"})
    feed.append("probe_unanswered", {"run": RUN, "unanswered_ms": 10000, "misses": 5})
    health.turn()
    clock.now = RAISE
    feed.append("probe_unanswered", {"run": RUN, "unanswered_ms": 15000, "misses": 7})
    health.turn()
    status = health.answer("status")
    assert [(c["code"], c["state"], c["run"]) for c in status["verdict"]["conditions"]] == [
        (APP_UNRESPONSIVE, "raised", RUN)]
    assert [(entry["state"], entry["reason"], entry["age_ms"]) for entry in status["ring"]] == [
        ("pending", "unanswered", RAISE), ("raised", "window_elapsed", 0)]
    assert status["catalogue"] == catalogue_digest()
    assert status["feeds"]["broker"]["after"] == 3


def test_a_gap_or_a_new_publisher_makes_the_judge_forget_pending_state():
    feed = Feed(4)
    reader = local_reader(feed)
    clock = Clock()
    health = HealthRunner(runner.shipped_judge(), (reader,), clock=clock)
    feed.append("probe_unanswered", {"run": RUN, "unanswered_ms": 10000, "misses": 5})
    health.turn()
    assert health.judge.verdict(0).conditions[0].state == "pending"
    for _ in range(10):  # laps the 4-event ring
        feed.append("probe_channel", {"run": RUN, "state": "open"})
    health.turn()
    assert reader.gaps == 1 and health.judge.verdict(RAISE).conditions == ()
    reader.feed = Feed(8)  # the broker restarted: a new incarnation
    reader.feed.append("probe_unanswered", {"run": RUN, "unanswered_ms": 10000, "misses": 5})
    health.turn()
    assert reader.gaps == 2 and reader.cursor.incarnation == reader.feed.incarnation


@pytest.mark.parametrize("failure", [FileNotFoundError(), TimeoutError(),
                                     ValueError("feed_read_refused")])
def test_an_unreadable_feed_leaves_the_verdict_and_cursor_as_they_were(failure):
    feed = Feed(8)
    reader = local_reader(feed)
    health = HealthRunner(runner.shipped_judge(), (reader,), clock=Clock())
    feed.append("probe_unanswered", {"run": RUN, "unanswered_ms": 10000, "misses": 5})
    health.turn()
    before = health.judge.verdict(0)

    def broken(request):
        raise failure

    reader._read = broken
    health.turn()
    assert health.judge.verdict(0) == before and reader.cursor.after == 1
    assert reader.failures == 1 and reader.document()["last_failure"]


def test_a_malformed_page_is_a_failure_not_a_crash():
    reader = local_reader(Feed(8))
    reader._read = lambda request: {"accepted": True, "events": "x"}
    HealthRunner(runner.shipped_judge(), (reader,), clock=Clock()).turn()
    assert reader.failures == 1 and reader.document()["last_failure"] == "feed_page"


def test_the_reader_drains_the_brokers_real_feed_socket(tmp_path):
    """End to end over the broker's own feed socket (the judge as uid pw-health)."""
    directory = tmp_path / "app-feed"
    directory.mkdir(mode=0o750)
    feed = Feed(512)
    for _ in range(20):
        feed.append("probe_answered", {"run": RUN, "rtt_ms": 3})
    listener = feed_listener(feed, directory / "feed.sock", owner_uid=os.getuid(), group=os.getgid(),
                             peer=lambda connection: 10006, kind=KIND)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            listener.serve()
            stop.wait(0.005)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        reader = FeedReader("broker", listener.path, kind=KIND)
        HealthRunner(runner.shipped_judge(), (reader,), clock=Clock()).turn()
    finally:
        stop.set()
        thread.join()
        listener.close()
    assert reader.cursor.after == 20 and reader.reads == 3 and reader.failures == 0
    assert reader.cursor.incarnation == feed.incarnation


def test_an_absent_broker_is_a_counted_failure(tmp_path):
    reader = FeedReader("broker", tmp_path / "missing.sock", kind=KIND)
    HealthRunner(runner.shipped_judge(), (reader,), clock=Clock()).turn()
    assert reader.failures == 1 and reader.reads == 0


# -- health.sock ---------------------------------------------------------------------------


@pytest.fixture
def server(tmp_path):
    directory = tmp_path / "health"
    directory.mkdir(mode=0o755)
    peers = {"uid": 0}
    health = HealthRunner(runner.shipped_judge(), (local_reader(Feed(8)),), clock=Clock())
    served = HealthSocket(directory / "health.sock", health.answer, stream=health.stream,
                          peer=lambda connection: peers["uid"], kind=KIND)
    yield served, peers, health
    health.overlay.close()
    served.close()


def ask(served, request, *, refused=False):
    client = socket.socket(socket.AF_UNIX, served.listener.type)
    client.settimeout(2)
    try:
        client.connect(str(served.path))
        client.sendall(request)
        served.serve()
        try:
            raw = client.recv(1 << 20)
        except ConnectionResetError:
            if not refused:
                raise
            raw = b""
    finally:
        client.close()
    return json.loads(raw) if raw else None


def test_the_health_socket_is_world_connectable(server):
    served, _, _ = server
    info = served.path.lstat()
    assert stat.S_ISSOCK(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o666


def test_root_reads_the_status(server):
    served, _, health = server
    status = ask(served, b'{"op":"status"}')
    assert status["accepted"] is True
    assert status["verdict"] == {"sequence": 0, "conditions": [], "outputs": []}
    assert status["overlay"] == {"instructions": [], "clients": 0}
    assert status["ring"] == [] and status["ring_dropped"] == 0
    assert set(status["feeds"]) == {"broker"}
    assert status == {"accepted": True, **status_document(health.judge, health.readers, 0)}


@pytest.mark.parametrize("uid", [10006, 10005, 10004, 10003, 65534])
def test_status_is_for_root_only(server, uid):
    served, peers, _ = server
    peers["uid"] = uid
    assert ask(served, b'{"op":"status"}', refused=True) is None


@pytest.mark.parametrize("request_bytes", [b'{"op":"status","x":1}', b"[]", b"not json",
                                           b'{"op":"events","after":0}', b'{"op":"overlay","x":1}'])
def test_a_bad_request_from_root_is_refused_without_detail(server, request_bytes):
    served, _, _ = server
    assert ask(served, request_bytes) == {"accepted": False, "reason": "health_request"}


def test_overlay_is_not_roots(server):
    served, _, health = server
    assert ask(served, b'{"op":"overlay"}', refused=True) is None
    assert health.overlay.connections == []


def test_without_a_stream_handler_overlay_is_refused(tmp_path):
    directory = tmp_path / "health"
    directory.mkdir(mode=0o755)
    served = HealthSocket(directory / "health.sock", lambda operation: {},
                          peer=lambda connection: runner.PW_DISPLAY_UID, kind=KIND)
    try:
        assert ask(served, b'{"op":"overlay"}') == {"accepted": False, "reason": "health_request"}
    finally:
        served.close()


def test_serving_never_waits_for_an_absent_client(server):
    served, _, _ = server
    served.serve()  # nothing pending: returns at once (non-blocking accept)


def test_a_stale_socket_is_replaced_but_a_foreign_file_or_open_directory_is_not(tmp_path):
    directory = tmp_path / "health"
    directory.mkdir(mode=0o755)
    first = HealthSocket(directory / "health.sock", lambda operation: {}, kind=KIND)
    first.listener.close()  # crashed: the socket file stays behind
    HealthSocket(directory / "health.sock", lambda operation: {}, kind=KIND).close()
    (directory / "health.sock").write_text("not a socket")
    with pytest.raises(ValueError, match="^health_socket_ownership$"):
        HealthSocket(directory / "health.sock", lambda operation: {}, kind=KIND)
    directory.chmod(0o775)
    with pytest.raises(ValueError, match="^health_directory$"):
        HealthSocket(directory / "health.sock", lambda operation: {}, kind=KIND)


def test_health_sock_reads_peers_with_the_kernel_peer_uid():
    # One SO_PEERCRED reading for every node socket (tests/test_feed_socket.py), never a copy.
    assert runner.peer_uid is feed_socket.peer_uid
    assert HealthSocket.__init__.__kwdefaults__["peer"] is feed_socket.peer_uid


# -- packaging -----------------------------------------------------------------------------


def test_the_judge_closure_is_small_and_the_base_ships_its_unit(tmp_path):
    from scripts.build_node_base_deb import POLICIES, UNITS, stage_tree
    from scripts.module_closure import closure_for

    modules = set(closure_for(POLICIES["health-judge"], repo=REPO).modules)
    assert {"appliance.health.runner", "appliance.health.judge", "contracts.node_faults",
            "appliance.node.probe", "appliance.display_host.overlay.instruction"} <= modules
    # The judge pulls in no broker, no Central session and no compositor code.
    assert not [module for module in modules if module.startswith((
        "appliance.central_session", "appliance.node.broker", "appliance.node.probe_channel",
        "appliance.display_host.weston", "appliance.display_host.runner"))]
    assert "photo-wall-health.service" in UNITS
    root = tmp_path / "package"
    stage_tree(REPO, root)
    assert (root / "usr/lib/photo-wall-health-judge/__main__.py").is_file()
    unit = (root / "lib/systemd/system/photo-wall-health.service").read_text().splitlines()
    for line in ("User=pw-health", "Group=pw-health", "SupplementaryGroups=pw-node-feeds",
                 "RuntimeDirectory=photo-wall-health", "RuntimeDirectoryMode=0755",
                 "RestrictAddressFamilies=AF_UNIX", "ProtectSystem=strict", "PrivateDevices=yes",
                 "NoNewPrivileges=yes", "CapabilityBoundingSet=", "MemoryMax=64M",
                 "Restart=always", "RestartSec=2", "Slice=photowallbase.slice",
                 "ConditionKernelCommandLine=photowall.node=v2",
                 "After=photo-wall-app-broker.service",
                 "ExecStart=/usr/bin/python3 -I -B /usr/lib/photo-wall-health-judge"):
        assert line in unit, line
    target = (root / "lib/systemd/system/photo-wall-node.target").read_text()
    wants = next(line for line in target.splitlines() if line.startswith("Wants="))
    assert "photo-wall-health.service" in wants.split("=", 1)[1].split()
    assert runner.HEALTH_SOCKET.parent.name == "photo-wall-health"


# -- the display feed ----------------------------------------------------------------------


def test_the_judge_reads_displays_feed_at_displays_path():
    # The judge closure may not import the controller, so the path is named twice and pinned.
    assert runner.DISPLAY_FEED_SOCKET == display_runner.FEED_SOCKET


def test_a_judge_started_after_the_display_ring_lapped_still_projects_every_output():
    """~32 presentations/s lap the 256 ring in ~8 s (E-AP1-1): the snapshot carries the set."""
    feed = Feed(4)
    for index in range(50):
        feed.append("CompositorPresentation", {"index": index})
    outputs = [snapshot_output(), snapshot_output("HDMI-A-2", None),
               snapshot_output("HDMI-A-3", connected=False)]
    for _restart in range(2):  # started after the lap, then restarted (a fresh judge and cursor)
        display = local_display_reader(feed, outputs)
        health = HealthRunner(runner.shipped_judge(), (display,), clock=Clock())
        health.turn()
        assert display.gaps == 1 and display.failures == 0
        assert [(i.output, i.tint) for i in health.judge.instructions(0)] == [
            ("HDMI-A-2", False), ("Virtual-1", False)]
        status = health.answer("status")
        assert status["verdict"]["outputs"] == [
            {"output": "HDMI-A-2", "underlay": "slate", "codes": []},
            {"output": "Virtual-1", "underlay": "live", "codes": []}]
        assert [(i["output"], i["tint"]) for i in status["overlay"]["instructions"]] == [
            ("HDMI-A-2", False), ("Virtual-1", False)]


def test_a_display_gap_never_makes_the_judge_forget_probe_state():
    broker, display_events = Feed(64), Feed(4)
    clock = Clock()
    display = local_display_reader(display_events, [snapshot_output()])
    health = HealthRunner(runner.shipped_judge(), (local_reader(broker), display), clock=clock)
    broker.append("probe_unanswered", {"run": RUN, "unanswered_ms": 10000, "misses": 5})
    health.turn()
    for index in range(20):  # the display ring laps between two turns
        display_events.append("CompositorPresentation", {"index": index})
    clock.now = RAISE
    health.turn()
    assert display.gaps == 1
    verdict = health.judge.verdict(RAISE)
    assert [(c.state, c.run) for c in verdict.conditions] == [("raised", RUN)]
    assert [(o.output, o.underlay) for o in verdict.outputs] == [("Virtual-1", "held")]


def test_a_malformed_snapshot_is_a_counted_failure_and_the_cursor_stays():
    feed = Feed(8)
    feed.append("CompositorPresentation", {"index": 0})
    display = local_display_reader(feed, [{"output_id": "Virtual-1"}])
    health = HealthRunner(runner.shipped_judge(), (display,), clock=Clock())
    health.turn()
    assert display.failures == 1 and display.last_failure == "display_snapshot"
    assert display.cursor.after == 0 and health.judge.verdict(0).outputs == ()
    display.outputs = [snapshot_output()]
    health.turn()
    assert display.cursor.after == 1 and display.failures == 1


def test_a_response_bound_display_reply_is_a_counted_failure():
    display = local_display_reader(Feed(8), [snapshot_output()])
    display._read = lambda request: (_ for _ in ()).throw(ValueError("feed_read_refused"))
    health = HealthRunner(runner.shipped_judge(), (display,), clock=Clock())
    health.turn()
    assert display.failures == 1 and health.judge.verdict(0).outputs == ()


def test_the_reader_drains_the_display_controllers_real_feed_socket(tmp_path):
    """End to end over Display's own feed socket (the judge as uid pw-health)."""
    from test_node_display_runner import FakeBackend

    backend = FakeBackend()
    controller = display_runner.Controller(backend)
    host = backend.host
    key = OutputKey(host.boot_id, host.incarnation_id, "Virtual-1", 2, 3)
    process = NodeProcessIdentity(4242, 777, uuid4())
    backend.states = (OutputState(key, admitted=Surface(key, process, 5, 1, 2, "frame-1"),
                                  diagnostic="released", fault="app_absent"),)
    for index in range(20):
        controller.feed.append("Observed", {"index": index})
    directory = tmp_path / "display-feed"
    directory.mkdir(mode=0o750)
    listener = display_runner.feed_listener(controller, directory / "feed.sock",
                                            group=os.getgid(), peer=lambda connection: 10006,
                                            kind=KIND)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            listener.serve()
            stop.wait(0.005)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        reader = DisplayFeedReader("display", listener.path, kind=KIND)
        health = HealthRunner(runner.shipped_judge(), (reader,), clock=Clock())
        health.turn()
    finally:
        stop.set()
        thread.join()
        listener.close()
    assert reader.cursor.after == 20 and reader.failures == 0
    run = AppRunKey(process.invocation_id, 4242, 777, 5).document()
    assert health.judge._outputs["Virtual-1"].admitted == run
    assert [(o.output, o.underlay) for o in health.judge.verdict(0).outputs] == [
        ("Virtual-1", "live")]


# -- the overlay op ------------------------------------------------------------------------

PAIR = socket.SOCK_SEQPACKET if LINUX else socket.SOCK_DGRAM  # packet boundaries kept


def judged(outputs=None, *, clock=None):
    clock = clock or Clock()
    display = local_display_reader(Feed(8), outputs or [snapshot_output()])
    broker = local_reader(Feed(64))
    health = HealthRunner(runner.shipped_judge(), (broker, display), clock=clock)
    health.turn()
    return health, broker, display, clock


def received(connection):
    packets = []
    connection.setblocking(False)
    while True:
        try:
            packets.append(parse_overlay_instruction(connection.recv(4096)))
        except BlockingIOError:
            return packets


@pytest.fixture
def pairs():
    made = []

    def make():
        ours, theirs = socket.socketpair(socket.AF_UNIX, PAIR)
        made.append(theirs)
        return ours, theirs

    yield make
    for connection in made:
        connection.close()


def test_a_client_gets_every_outputs_instruction_when_it_connects(pairs):
    health, *_ = judged([snapshot_output(), snapshot_output("HDMI-A-2", None)])
    ours, theirs = pairs()
    health.stream("overlay", ours)
    assert [(i.output, i.tint, i.lines) for i in received(theirs)] == [
        ("HDMI-A-2", False, ("", "")), ("Virtual-1", False, ("", ""))]
    health.overlay.close()


def test_changes_are_pushed_each_turn_and_everything_every_v_over_3(pairs):
    health, broker, _display, clock = judged()
    ours, theirs = pairs()
    health.stream("overlay", ours)
    [first] = received(theirs)  # judged()'s turn already refreshed at 0
    health.turn()
    assert received(theirs) == []
    clock.now = 1000
    health.turn()
    assert received(theirs) == []  # nothing changed, refresh not due
    broker.feed.append("probe_unanswered", {"run": RUN, "unanswered_ms": 10000, "misses": 5})
    health.turn()
    clock.now = 1000 + RAISE  # raised; a refresh is also due here, so it counts from now
    health.turn()
    [tinted] = received(theirs)
    assert tinted.tint and tinted.serial > first.serial
    assert tinted.lines[0] == FAULTS[APP_UNRESPONSIVE].household_line
    clock.now = 2000 + RAISE  # a change between refreshes is pushed on its own turn
    broker.feed.append("app_link_accepted", {"run": RUN, "player_id": "player-1"})
    health.turn()
    [named] = received(theirs)
    assert named.serial > tinted.serial and "Player player-1" in named.lines[1]
    tinted = named
    clock.now = 1000 + RAISE + INSTRUCTION_STALE_MS // 3 - 1
    health.turn()
    assert received(theirs) == []
    clock.now = 1000 + RAISE + INSTRUCTION_STALE_MS // 3
    health.turn()
    assert received(theirs) == [tinted]  # re-pushed unchanged (same serial)
    health.overlay.close()


def test_a_reconnected_client_is_re_pushed_everything(pairs):
    health, _broker, _display, clock = judged()
    first, theirs = pairs()
    health.stream("overlay", first)
    pushed = received(theirs)
    theirs.close()
    clock.now = INSTRUCTION_STALE_MS // 3
    health.turn()  # the refresh cannot reach the closed client: dropped
    assert health.overlay.connections == []
    again, theirs = pairs()
    health.stream("overlay", again)
    assert received(theirs) == pushed
    health.overlay.close()


def test_presented_reports_are_kept_in_the_ring(pairs):
    health, *_ = judged()
    ours, theirs = pairs()
    health.stream("overlay", ours)
    [instruction] = received(theirs)
    theirs.send(encode_presented_report(PresentedReport("Virtual-1", instruction.serial)))
    health.overlay.receive(ours, 0)
    status = health.answer("status")
    assert [entry for entry in status["ring"] if entry["state"] == "presented"] == [
        {"sequence": health.judge.sequence, "state": "presented", "output": "Virtual-1",
         "serial": instruction.serial, "age_ms": 0}]
    assert status["overlay"]["clients"] == 1
    health.overlay.close()


@pytest.mark.parametrize("packet", [b"", b"not json", b'{"output":"Virtual-1"}', b"x" * 600])
def test_a_client_sending_anything_but_a_report_is_dropped(pairs, packet):
    health, *_ = judged()
    ours, theirs = pairs()
    health.stream("overlay", ours)
    if packet:
        theirs.send(packet)
    else:
        theirs.close()  # EOF
    health.overlay.receive(ours, 0)
    assert health.overlay.connections == []
    assert not [entry for entry in health.judge.transitions()]


def test_a_fifth_client_replaces_the_oldest(pairs):
    health, *_ = judged()
    connections = [pairs()[0] for _ in range(runner.MAX_OVERLAY_CLIENTS + 1)]
    for connection in connections:
        health.stream("overlay", connection)
    assert health.overlay.connections == connections[1:]
    assert connections[0].fileno() == -1
    health.overlay.close()


def test_overlay_clients_unregister_from_the_selector_when_dropped(pairs):
    import selectors

    health, *_ = judged()
    with selectors.DefaultSelector() as selector:
        health.overlay.attach(selector)
        ours, theirs = pairs()
        health.stream("overlay", ours)
        assert selector.get_key(ours).data == "overlay"
        theirs.close()
        health.overlay.receive(ours, 0)
        assert len(selector.get_map()) == 0
    assert isinstance(health.overlay, OverlayClients)


@pytest.mark.parametrize("uid", [0, 10004, 10006, 65534])
def test_only_pw_display_opens_overlay(server, uid):
    served, peers, health = server
    peers["uid"] = uid
    assert ask(served, b'{"op":"overlay"}', refused=True) is None
    assert health.overlay.connections == []


def test_pw_display_opens_overlay_and_the_connection_stays_open(server):
    served, peers, health = server
    peers["uid"] = runner.PW_DISPLAY_UID
    display = local_display_reader(Feed(8), [snapshot_output()])
    health.readers = (*health.readers, display)
    health.turn()
    client = socket.socket(socket.AF_UNIX, served.listener.type)
    client.settimeout(2)
    try:
        client.connect(str(served.path))
        client.sendall(b'{"op":"overlay"}')
        served.serve()
        instruction = parse_overlay_instruction(client.recv(4096))
        assert (instruction.output, instruction.tint) == ("Virtual-1", False)
        assert len(health.overlay.connections) == 1
        assert ask(served, b'{"op":"status"}', refused=True) is None  # pw-display: no status
    finally:
        client.close()

