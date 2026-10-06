"""Broker runner: probe publication and the owed relink each turn, the node feed socket, the
broker closure."""
import json
import os
import socket
import stat
import sys
import time
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from node.test_node_linux_adapters import store as boot_store
from support.packet_pair import packet_pair
from support.repo import REPO
from test_node_boot import environment

from appliance import feed_socket
from appliance.apps import broker_runner
from appliance.apps.broker import RunningApp
from appliance.apps.broker_runner import BrokerLoop
from appliance.apps.probe import AppRunKey, OwedRelink, ProbeTiming
from appliance.apps.probe_channel import ProbeThread
from appliance.feed import Feed
from appliance.node import app_link
from contracts.node_app_link import parse_node_probe_channel_message
from contracts.node_protocol import NodeProcessIdentity, NodeProducerV2

FAST = ProbeTiming(period_ms=50, miss_limit=2, startup_ms=100, kill_after_ms=300)
DUE = -(-FAST.kill_after_ms // FAST.period_ms) + 1  # turns until kill-due (the first pass is at 0)
LINUX = sys.platform.startswith("linux")


def monotonic_ms():
    return time.monotonic_ns() // 1_000_000


# -- the feed socket (the kernel listener's own tests: tests/test_feed_socket.py) -----------


@pytest.fixture
def listener(tmp_path):
    directory = tmp_path / "app-feed"
    directory.mkdir(mode=0o750)
    feed = Feed(512)
    readers = {"uid": 0}
    served = broker_runner.feed_listener(feed, directory / "feed.sock", owner_uid=os.getuid(),
                                         group=os.getgid(), peer=lambda connection: readers["uid"],
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


def test_the_broker_serves_its_feed_through_the_kernel_listener_not_a_copy(listener):
    served, _, _ = listener
    assert broker_runner.FeedListener is feed_socket.FeedListener
    assert type(served) is feed_socket.FeedListener
    assert not hasattr(broker_runner, "peer_uid")
    assert served.readers == feed_socket.FEED_READERS == frozenset({0, 10006})
    assert broker_runner.feed_listener.__defaults__ == (broker_runner.FEED_SOCKET,)
    assert broker_runner.feed_listener.__kwdefaults__ == {"owner_uid": 0,
                                                          "group": feed_socket.FEEDS_GROUP}


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
        self.running, self.calls, self.kills = running, 0, []

    def current(self):
        self.calls += 1
        return self.running

    def kill(self, expected):
        self.kills.append(expected)
        return True


def loop_for(tmp_path, monkeypatch, *, granted=True, producer=None, running=None, links=None):
    """A turn over fakes, a real BootStore and a real probe thread (no channel: misses), not
    started: `turns` runs its passes on the test's clock, `loop.now`."""
    monkeypatch.setattr(broker_runner, "boottime_ms", monotonic_ms)  # CLOCK_BOOTTIME is Linux-only
    producer = producer or NodeProducerV2("site", "device-" + "a" * 64, 1, uuid4(), "app_effect_broker",
                                          uuid4())
    running = running or RunningApp(environment("a"), NodeProcessIdentity(396, 31751781, uuid4()), 1, uuid4())
    store = boot_store(tmp_path / f"broker-{granted}", producer.kernel_boot_id)
    feed = Feed(512)
    probes = ProbeThread(feed, clock=lambda: loop.now, timing=FAST)
    probes.check = lambda: None  # alive by construction: the test runs its passes
    session, driver = Session(producer, granted=granted), Driver(running)
    online = SimpleNamespace(broker=SimpleNamespace(record={"phase": "running"}, service=lambda: None),
                             tick=lambda: None)
    links = links or SimpleNamespace(serve_one=lambda: None, remember_grant=lambda: None)
    loop = BrokerLoop(broker=SimpleNamespace(reconcile=lambda: None), online=online, store=store,
                      session=session, driver=driver, links=links, probes=probes,
                      feeds=SimpleNamespace(serve=lambda: None))
    loop.now = 0
    return loop, feed, session, driver, running, store


def turns(loop, count):
    """`count` main-loop turns, each followed by one probe pass, a period apart: time is
    counted in turns, so a stalled test process changes nothing."""
    for _ in range(count):
        loop.turn()
        loop.probes.step(loop.now)
        loop.now += FAST.period_ms


def test_probe_facts_never_reach_a_central_request(tmp_path, monkeypatch):
    loop, feed, session, _, running, store = loop_for(tmp_path, monkeypatch)
    try:
        turns(loop, DUE)
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
            turns(loop, 3)
            assert driver.calls == 3
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
        del loop.probes.check
        loop.probes.start()
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
    ours, theirs = packet_pair()
    theirs.settimeout(2)
    try:
        turns(loop, 2)
        pending.append((ours, run))  # the Player's responder reconnects to the new broker
        turns(loop, 2)
        first = parse_node_probe_channel_message(theirs.recv(9000))
        assert type(first).__name__ == "NodeRelinkV2"
        relinked = [e for e in feed.read(0, incarnation=None, limit=8).events if e.kind == "relink_sent"]
        assert [e.value for e in relinked] == [{"run": run.document()}]
    finally:
        loop.probes.close()
        theirs.close()
        store.close()


def test_every_refusal_on_a_long_lived_channel_is_relinked(tmp_path, monkeypatch):
    """B7c: one probe channel lives across two Central refusals of the same run's link (the
    Player re-proved in between): each refusal sends its own relink on that channel."""
    pending = []
    links = SimpleNamespace(remember_grant=lambda: None,
                            serve_one=lambda: pending and loop.probes.adopt(*pending.pop()))
    loop, feed, _, _, running, store = loop_for(tmp_path, monkeypatch, granted=False, links=links)
    run = AppRunKey.of(running)
    ours, theirs = packet_pair()
    theirs.settimeout(2)

    def relinks_received():
        kinds = []
        theirs.setblocking(False)
        try:
            while True:
                kinds.append(type(parse_node_probe_channel_message(theirs.recv(9000))).__name__)
        except BlockingIOError:
            pass
        return kinds.count("NodeRelinkV2")

    try:
        pending.append((ours, run))
        turns(loop, 2)
        for refusal in range(2):
            app_link._refused(store, SimpleNamespace(feed=feed), run, status=409, reason="central_refused")
            turns(loop, 3)  # restated every turn
            assert relinks_received() == 1, refusal
            store.write(app_link.OUTBOX, {"run": run.document(), "player_id": "p", "link": "{}"})
            turns(loop, 1)  # the Player re-proved: nothing owed until Central refuses again
        events = feed.read(0, incarnation=None, limit=8)
        kinds = []
        after = 0
        while events.events:
            kinds += [(e.kind, e.value.get("state")) for e in events.events
                      if e.kind in ("relink_sent", "probe_channel", "app_link_refused")]
            after = events.events[-1].sequence
            events = feed.read(after, incarnation=None, limit=8)
        assert kinds == [("probe_channel", "open"),
                         ("app_link_refused", None), ("relink_sent", None),
                         ("app_link_refused", None), ("relink_sent", None)]
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
        store.write(app_link.OUTBOX, {"relink": AppRunKey.of(running).document(), "episode": "e1"})
        loop.turn()
        store.write(app_link.OUTBOX, {"relink": AppRunKey(uuid4(), 1, 2, 3).document(), "episode": "e2"})
        loop.turn()
        assert owed == [None, OwedRelink(AppRunKey.of(running), "e1"), None]
    finally:
        loop.probes.close()
        store.close()


# -- packaging -----------------------------------------------------------------------------


def test_broker_closure_has_no_host_module_and_the_base_declares_the_feed(tmp_path):
    from scripts.build_node_base_deb import POLICIES, stage_tree
    from scripts.module_closure import closure_for

    modules = closure_for(POLICIES["app-broker"], repo=REPO).modules
    assert {"appliance.apps.probe", "appliance.apps.probe_channel", "appliance.feed"} <= set(modules)
    assert not [module for module in modules if module.startswith("appliance.host.host")]
    stage_tree(REPO, tmp_path / "package")  # the app-broker deny list refuses a host module itself
    root = tmp_path / "package"
    users = (root / "usr/lib/sysusers.d/photo-wall-node.conf").read_text().splitlines()
    assert 'u pw-health 10006 "Photo Wall health judge" /nonexistent' in users
    assert "g pw-node-feeds 10007" in users and "m pw-health pw-node-feeds" in users
    tmpfiles = (root / "usr/lib/tmpfiles.d/photo-wall-node.conf").read_text().splitlines()
    assert "d /run/photo-wall-app-feed 0750 root pw-node-feeds -" in tmpfiles
    unit = (root / "lib/systemd/system/photo-wall-app-broker.service").read_text()
    assert any(line.startswith("ReadWritePaths=") and "/run/photo-wall-app-feed" in line.split()
               for line in unit.splitlines())


def test_a_host_module_in_the_broker_closure_is_refused() -> None:
    from scripts.build_node_base_deb import POLICIES
    from scripts.module_closure import ClosureError, closure_for

    policy = POLICIES["app-broker"]
    with pytest.raises(ClosureError, match="appliance.host is forbidden here"):
        closure_for(replace(policy, roots=(*policy.roots, "appliance.host.host_linux")), repo=REPO)
