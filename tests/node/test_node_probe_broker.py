"""Broker runner: probe publication each turn, the node feed socket, the broker closure."""
import json
import os
import socket
import stat
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from node.test_node_linux_adapters import store as boot_store
from support.repo import REPO
from test_node_boot import environment

from appliance import feed_socket
from appliance.apps import broker_runner
from appliance.apps.broker import RunningApp
from appliance.apps.broker_runner import BrokerLoop
from appliance.apps.probe import AppRunKey, ProbeTiming
from appliance.apps.probe_channel import ProbeThread
from appliance.feed import Feed
from contracts.node_protocol import NodeProcessIdentity

FAST = ProbeTiming(period_ms=50, miss_limit=2, startup_ms=100)
# Turns until the unanswered run is reported (the first pass is at 0), with a margin.
DUE = FAST.startup_ms // FAST.period_ms + FAST.miss_limit + 2
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


class Driver:
    def __init__(self, running):
        self.running, self.calls, self.kills = running, 0, []

    def current(self):
        self.calls += 1
        return self.running

    def launched(self):  # the run is current whenever it is known: no launch record needed
        return None

    def kill(self, expected):  # nothing may call it (issue #125): a test asserts it never ran
        self.kills.append(expected)
        return True

    def stop_foreign(self):
        self.kills.append("stop_foreign")


def loop_for(tmp_path, monkeypatch, *, running=None, listener=None):
    """A turn over fakes, a real BootStore and a real probe thread (no channel: misses), not
    started: `turns` runs its passes on the test's clock, `loop.now`."""
    monkeypatch.setattr(broker_runner, "boottime_ms", monotonic_ms)  # CLOCK_BOOTTIME is Linux-only
    running = running or RunningApp(environment("a"), NodeProcessIdentity(396, 31751781, uuid4()), 1, uuid4())
    store = boot_store(tmp_path / "broker", uuid4())
    feed = Feed(512)
    probes = ProbeThread(feed, clock=lambda: loop.now, timing=FAST)
    probes.check = lambda: None  # alive by construction: the test runs its passes
    driver = Driver(running)
    listener = listener or SimpleNamespace(serve_one=lambda: None)
    loop = BrokerLoop(broker=SimpleNamespace(reconcile=lambda: None), store=store, driver=driver,
                      listener=listener, probes=probes, feeds=SimpleNamespace(serve=lambda: None),
                      display=SimpleNamespace(tell=lambda run: True), clock=lambda: loop.now)
    loop.now = 0
    return loop, feed, driver, running, store


def turns(loop, count):
    """`count` main-loop turns, each followed by one probe pass, a period apart: time is
    counted in turns, so a stalled test process changes nothing."""
    for _ in range(count):
        loop.turn()
        loop.probes.step(loop.now)
        loop.now += FAST.period_ms


def test_probe_facts_go_on_the_node_feed_alone(tmp_path, monkeypatch):
    """The broker makes no Central request (decision 0034, S4c): its loop has no session, and an
    unanswered run is a node-feed fact for the judge."""
    loop, feed, _, running, store = loop_for(tmp_path, monkeypatch)
    try:
        assert not {"online", "session", "links"} & set(vars(loop))
        turns(loop, DUE)
        page = feed.read(0, incarnation=None, limit=8)
        kinds = {event.kind for event in page.events}
        assert "probe_unanswered" in kinds, kinds
        assert page.events[0].value["run"] == AppRunKey.of(running).document()
        assert all(e.audience == "node" for e in page.events)
    finally:
        loop.probes.close()
        store.close()


def test_each_turn_asks_the_driver_once_and_publishes(tmp_path, monkeypatch):
    loop, feed, driver, running, store = loop_for(tmp_path, monkeypatch)
    try:
        turns(loop, 3)
        assert driver.calls == 3
        assert feed.read(0, incarnation=None).events[0].value["run"] == AppRunKey.of(running).document()
    finally:
        loop.probes.close()
        store.close()


def test_an_unanswered_run_is_never_killed(tmp_path, monkeypatch):
    """Issue #125, the removed kill after missed pings: a run that never answers is reported on
    the feed (the judge's card) for as long as it stays unanswered, and nothing signals, stops or
    restarts it. Mutation probe: put the kill-due latch and `_consume_kill_due` back -> fails."""
    loop, feed, driver, _, store = loop_for(tmp_path, monkeypatch)
    try:
        turns(loop, DUE * 20)
        kinds = {event.kind for event in feed.read(0, incarnation=None, limit=512).events}
        assert "probe_unanswered" in kinds, kinds
        assert not kinds & {"probe_kill_due", "app_killed", "kill_withheld"}, kinds
        assert driver.kills == [] and driver.calls == DUE * 20
    finally:
        loop.probes.close()
        store.close()


def test_a_dead_probe_thread_stops_the_broker(tmp_path, monkeypatch):
    loop, _, _, _, store = loop_for(tmp_path, monkeypatch)
    try:
        del loop.probes.check
        loop.probes.start()
        loop.probes.close()
        with pytest.raises(RuntimeError, match="probe_thread_stopped"):
            loop.turn()
    finally:
        store.close()


# -- packaging -----------------------------------------------------------------------------


def test_broker_closure_has_no_host_module_and_the_base_declares_the_feed():
    from node.launcher_closures import closure

    # Refused (ClosureError) when the broker reaches a host module.
    modules = closure("app-broker").modules
    assert {"appliance.apps.probe", "appliance.apps.probe_channel", "appliance.feed"} <= set(modules)
    users = (REPO / "debian/photo-wall-node.sysusers").read_text().splitlines()
    assert 'u pw-health 10006 "Photo Wall health judge" /nonexistent' in users
    assert "g pw-node-feeds 10007" in users and "m pw-health pw-node-feeds" in users
    tmpfiles = (REPO / "debian/photo-wall-node.tmpfiles").read_text().splitlines()
    assert "d /run/photo-wall-app-feed 0750 root pw-node-feeds -" in tmpfiles
    unit = (REPO / "appliance/systemd/photo-wall-app-broker.service").read_text()
    assert any(line.startswith("ReadWritePaths=") and "/run/photo-wall-app-feed" in line.split()
               for line in unit.splitlines())


def test_a_host_module_in_the_broker_closure_is_refused() -> None:
    from node.launcher_closures import FORBIDDEN

    from scripts.module_closure import ClosureError, compute_closure, first_party_packages

    with pytest.raises(ClosureError, match="appliance.host is forbidden here"):
        compute_closure(("appliance.apps.broker_runner", "appliance.host.host_linux"), repo=REPO,
                        first_party=first_party_packages(REPO), forbidden=FORBIDDEN["app-broker"],
                        third_party=None)
