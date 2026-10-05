"""Health judge unit: draining the broker feed by cursor, the root-only `status` op, packaging."""
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
from appliance.feed import Feed, answer_feed_read
from appliance.health import runner
from appliance.health.judge import APP_UNRESPONSIVE
from appliance.health.runner import FeedReader, HealthRunner, HealthSocket, status_document
from appliance.node.broker_runner import feed_listener
from appliance.node.probe import AppRunKey
from contracts.node_faults import FAULTS, catalogue_digest

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
    served = HealthSocket(directory / "health.sock", health.answer,
                          peer=lambda connection: peers["uid"], kind=KIND)
    yield served, peers, health
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
    assert status["verdict"] == {"sequence": 0, "conditions": []}
    assert status["ring"] == [] and status["ring_dropped"] == 0
    assert set(status["feeds"]) == {"broker"}
    assert status == {"accepted": True, **status_document(health.judge, health.readers, 0)}


@pytest.mark.parametrize("uid", [10006, 10005, 10004, 10003, 65534])
def test_status_is_for_root_only(server, uid):
    served, peers, _ = server
    peers["uid"] = uid
    assert ask(served, b'{"op":"status"}', refused=True) is None


@pytest.mark.parametrize("request_bytes", [b'{"op":"overlay"}', b'{"op":"status","x":1}', b"[]",
                                           b"not json", b'{"op":"events","after":0}'])
def test_a_bad_request_from_root_is_refused_without_detail(server, request_bytes):
    served, _, _ = server
    assert ask(served, request_bytes) == {"accepted": False, "reason": "health_request"}


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
