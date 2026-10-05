"""Broker runner: probe publication and the owed relink each turn, the node feed socket, the
broker closure."""
import json
import os
import socket
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_node_boot import environment
from test_node_linux_adapters import store as boot_store

from appliance.feed import Feed
from appliance.node import app_link, broker_runner
from appliance.node.broker import RunningApp
from appliance.node.broker_runner import BrokerLoop, FeedListener
from appliance.node.probe import AppRunKey, ProbeTiming
from appliance.node.probe_channel import ProbeThread
from contracts.node_app_link import parse_node_probe_channel_message
from contracts.node_protocol import NodeProcessIdentity, NodeProducerV2

REPO = Path(__file__).resolve().parents[1]
FAST = ProbeTiming(period_ms=50, miss_limit=2, startup_ms=100, kill_after_ms=300)
LINUX = sys.platform.startswith("linux")


def monotonic_ms():
    return time.monotonic_ns() // 1_000_000


# -- the feed socket -----------------------------------------------------------------------


@pytest.fixture
def listener(tmp_path):
    directory = tmp_path / "app-feed"
    directory.mkdir(mode=0o750)
    feed = Feed(512)
    readers = {"uid": 0}
    served = FeedListener(feed, directory / "feed.sock", owner_uid=os.getuid(), group=os.getgid(),
                          peer=lambda connection: readers["uid"],
                          kind=socket.SOCK_SEQPACKET if LINUX else socket.SOCK_STREAM)
    yield served, feed, readers
    served.close()


def read(served, request, *, refused=False):
    client = socket.socket(socket.AF_UNIX, served.listener.type)
    client.settimeout(2)
    try:
        client.connect(str(served.path))
        client.sendall(request)
        served.serve()
        try:
            raw = client.recv(65536)
        except ConnectionResetError:
            # A refused reader's connection is closed with its request unread; Linux AF_UNIX
            # then reports ECONNRESET instead of EOF. Either way it received no byte.
            if not refused:
                raise
            raw = b""
    finally:
        client.close()
    return json.loads(raw) if raw else None


def test_feed_socket_is_group_readable_only(listener):
    served, _, _ = listener
    info = served.path.lstat()
    assert stat.S_ISSOCK(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o660


def test_root_and_the_judge_read_the_feed_by_cursor(listener):
    served, feed, readers = listener
    run = AppRunKey(uuid4(), 1, 2, 1).document()
    for _ in range(10):
        feed.append("probe_answered", {"run": run, "rtt_ms": 3})
    for uid in (0, 10006):
        readers["uid"] = uid
        page = read(served, json.dumps({"op": "events", "after": 0, "incarnation": None}).encode())
        assert page["accepted"] and page["stream_gap"] is False
        assert [e["sequence"] for e in page["events"]] == list(range(1, 9))
        assert page["publisher_incarnation"] == str(feed.incarnation)
        assert {e["audience"] for e in page["events"]} == {"node"}
    page = read(served, json.dumps({"after": 8, "incarnation": str(feed.incarnation)}).encode())
    assert [e["sequence"] for e in page["events"]] == [9, 10] and page["stream_gap"] is False


def test_any_other_reader_gets_nothing(listener):
    served, feed, readers = listener
    feed.append("probe_answered", {"rtt_ms": 1})
    for uid in (10004, 10005, 10003, 65534):
        readers["uid"] = uid
        assert read(served, b'{"op":"events","after":0}', refused=True) is None


def test_a_bad_request_is_refused_without_detail(listener):
    served, _, _ = listener
    assert read(served, b'{"op":"events"}') == {"accepted": False, "reason": "feed_read_request"}
    assert read(served, b"[]") == {"accepted": False, "reason": "feed_read_request"}


def test_serving_never_waits_for_an_absent_reader(listener):
    served, _, _ = listener
    started = time.monotonic()
    served.serve()
    assert time.monotonic() - started < 0.05


def test_a_stale_socket_is_replaced_but_a_foreign_file_is_not(tmp_path):
    directory = tmp_path / "app-feed"
    directory.mkdir(mode=0o750)
    (directory / "feed.sock").write_text("not a socket")
    with pytest.raises(ValueError, match="feed_socket_ownership"):
        FeedListener(Feed(8), directory / "feed.sock", owner_uid=os.getuid(), group=os.getgid())
    directory.chmod(0o770)
    with pytest.raises(ValueError, match="feed_directory"):
        FeedListener(Feed(8), directory / "feed.sock", owner_uid=os.getuid(), group=os.getgid())


@pytest.mark.skipif(not LINUX, reason="SO_PEERCRED is Linux-only")
def test_peer_uid_is_the_kernel_credential():
    first, second = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with first, second:
        assert broker_runner.peer_uid(first) == os.getuid()


# -- the main-loop turn --------------------------------------------------------------------


class Session:
    """Records every Central request the broker makes."""

    def __init__(self, producer, *, granted=True):
        self.grant = SimpleNamespace(producer=producer) if granted else None
        self.requests = []

    def ensure(self):
        return self.grant

    def request(self, method, path, body=None):
        self.requests.append((method, path, body or b""))
        return 200, b"{}"


class Driver:
    def __init__(self, running):
        self.running, self.calls = running, 0

    def current(self):
        self.calls += 1
        return self.running


def loop_for(tmp_path, monkeypatch, *, granted=True, producer=None, running=None, links=None):
    """A turn over fakes, a real BootStore and a real probe thread (no channel: misses)."""
    monkeypatch.setattr(broker_runner, "boottime_ms", monotonic_ms)  # CLOCK_BOOTTIME is Linux-only
    producer = producer or NodeProducerV2("site", "device-" + "a" * 64, 1, uuid4(), "app_effect_broker",
                                          uuid4())
    running = running or RunningApp(environment("a"), NodeProcessIdentity(396, 31751781, uuid4()), 1, uuid4())
    store = boot_store(tmp_path / f"broker-{granted}", producer.kernel_boot_id)
    feed = Feed(512)
    probes = ProbeThread(feed, clock=monotonic_ms, timing=FAST)
    probes.start()
    session, driver = Session(producer, granted=granted), Driver(running)
    online = SimpleNamespace(broker=SimpleNamespace(record={"phase": "running"}, service=lambda: None),
                             tick=lambda: None)
    links = links or SimpleNamespace(serve_one=lambda: None, remember_grant=lambda: None)
    loop = BrokerLoop(broker=SimpleNamespace(reconcile=lambda: None), online=online, store=store,
                      session=session, driver=driver, links=links, probes=probes,
                      feeds=SimpleNamespace(serve=lambda: None))
    return loop, feed, session, driver, running, store


def turns(loop, seconds):
    end = time.monotonic() + seconds
    count = 0
    while time.monotonic() < end:
        loop.turn()
        count += 1
        time.sleep(0.02)
    return count


def test_probe_facts_never_reach_a_central_request(tmp_path, monkeypatch):
    loop, feed, session, _, running, store = loop_for(tmp_path, monkeypatch)
    try:
        turns(loop, 0.8)
        page = feed.read(0, incarnation=None, limit=8)
        kinds = {event.kind for event in page.events}
        assert {"probe_unanswered", "probe_kill_due"} <= kinds, kinds
        assert page.events[0].value["run"] == AppRunKey.of(running).document()
        assert [path for _, path, _ in session.requests] == ["/v2/node/evidence"]
        for _, _, body in session.requests:
            assert b"probe" not in body and b"rtt_ms" not in body and b"unanswered" not in body
        assert all(e.audience == "node" for e in page.events)
    finally:
        loop.probes.close()
        store.close()


def test_each_turn_asks_the_driver_once_and_publishes_with_or_without_a_session(tmp_path, monkeypatch):
    for granted in (True, False):
        loop, feed, session, driver, running, store = loop_for(tmp_path, monkeypatch, granted=granted)
        try:
            count = turns(loop, 0.5)
            assert driver.calls == count
            assert feed.read(0, incarnation=None).events[0].value["run"] == AppRunKey.of(running).document()
            assert loop.probes.recovery_may_be_armed is False
            if not granted:
                assert session.requests == []
        finally:
            loop.probes.close()
            store.close()


def test_a_dead_probe_thread_stops_the_broker(tmp_path, monkeypatch):
    loop, _, _, _, _, store = loop_for(tmp_path, monkeypatch)
    try:
        loop.probes.close()
        with pytest.raises(RuntimeError, match="probe_thread_stopped"):
            loop.turn()
    finally:
        store.close()


def test_a_relink_owed_before_a_broker_restart_reaches_the_next_channel(tmp_path, monkeypatch):
    """Refusal, then the broker restarts before any probe channel opens (E-B7-3): the boot
    store keeps the owed relink and the new broker's first known turn restates it."""
    producer = NodeProducerV2("site", "device-" + "a" * 64, 1, uuid4(), "app_effect_broker", uuid4())
    running = RunningApp(environment("a"), NodeProcessIdentity(396, 31751781, uuid4()), 1, uuid4())
    run = AppRunKey.of(running)
    store = boot_store(tmp_path / "broker-True", producer.kernel_boot_id)
    app_link._refused(store, SimpleNamespace(feed=Feed(8)), run, status=409, reason="central_refused")
    store.close()  # the broker exits with no probe channel ever open: nothing was sent
    pending = []
    links = SimpleNamespace(remember_grant=lambda: None,
                            serve_one=lambda: pending and loop.probes.adopt(*pending.pop()))
    loop, feed, _, _, _, store = loop_for(tmp_path, monkeypatch, producer=producer, running=running,
                                          links=links)
    ours, theirs = (socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET) if LINUX
                    else socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM))
    theirs.settimeout(2)
    try:
        turns(loop, 0.1)
        pending.append((ours, run))  # the Player's responder reconnects to the new broker
        turns(loop, 0.1)
        first = parse_node_probe_channel_message(theirs.recv(9000))
        assert type(first).__name__ == "NodeRelinkV2"
        relinked = [e for e in feed.read(0, incarnation=None, limit=8).events if e.kind == "relink_sent"]
        assert [e.value for e in relinked] == [{"run": run.document()}]
    finally:
        loop.probes.close()
        theirs.close()
        store.close()


def test_a_turn_owes_nothing_when_the_slot_holds_no_relink(tmp_path, monkeypatch):
    loop, _, _, _, running, store = loop_for(tmp_path, monkeypatch, granted=False)
    owed = []
    loop.probes.owe_relink = owed.append
    try:
        loop.turn()
        store.write(app_link.OUTBOX, {"relink": AppRunKey.of(running).document()})
        loop.turn()
        store.write(app_link.OUTBOX, {"relink": AppRunKey(uuid4(), 1, 2, 3).document()})
        loop.turn()
        assert owed == [None, AppRunKey.of(running), None]
    finally:
        loop.probes.close()
        store.close()


# -- packaging -----------------------------------------------------------------------------


def test_broker_closure_has_no_host_module_and_the_base_declares_the_feed(tmp_path):
    from scripts.build_node_base_deb import POLICIES, stage_tree
    from scripts.module_closure import closure_for

    modules = closure_for(POLICIES["app-broker"], repo=REPO).modules
    assert {"appliance.node.probe", "appliance.node.probe_channel", "appliance.feed"} <= set(modules)
    assert not [module for module in modules if module.startswith("appliance.node.host")]
    stage_tree(REPO, tmp_path / "package")  # refuses app_import_boundary itself
    root = tmp_path / "package"
    users = (root / "usr/lib/sysusers.d/photo-wall-node.conf").read_text().splitlines()
    assert 'u pw-health 10006 "Photo Wall health judge" /nonexistent' in users
    assert "g pw-node-feeds 10007" in users and "m pw-health pw-node-feeds" in users
    tmpfiles = (root / "usr/lib/tmpfiles.d/photo-wall-node.conf").read_text().splitlines()
    assert "d /run/photo-wall-app-feed 0750 root pw-node-feeds -" in tmpfiles
    unit = (root / "lib/systemd/system/photo-wall-app-broker.service").read_text()
    assert any(line.startswith("ReadWritePaths=") and "/run/photo-wall-app-feed" in line.split()
               for line in unit.splitlines())


def test_a_host_module_in_the_broker_closure_is_refused(tmp_path, monkeypatch):
    from scripts import build_node_base_deb

    real = build_node_base_deb.closure_for

    def closure_for(policy, *, repo):
        closure = real(policy, repo=repo)
        if policy.name == "app-broker":
            return SimpleNamespace(**{**{name: getattr(closure, name) for name in ("files", "digest")},
                                      "modules": (*closure.modules, "appliance.node.host_linux")})
        return closure

    monkeypatch.setattr(build_node_base_deb, "closure_for", closure_for)
    with pytest.raises(ValueError, match="app_import_boundary"):
        build_node_base_deb.stage_tree(REPO, tmp_path / "package")
