"""The broker's main loop reports each app run's start and exit, and samples its open files,
for the health judge (1b P1b; `appliance.kernel.app_facts`), Central session or not."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from node.apps.test_app_descriptors import proc_tree
from node.test_node_linux_adapters import store as boot_store
from node.test_node_probe_broker import Session
from test_node_boot import environment

from appliance.apps import broker_runner
from appliance.apps.broker import RunningApp
from appliance.apps.broker_runner import BrokerLoop
from appliance.apps.descriptors import DESCRIPTOR_SAMPLE_MS
from appliance.apps.probe import AppRunKey
from appliance.feed import Feed
from appliance.kernel.app_facts import APP_DESCRIPTORS, APP_EXITED, APP_STARTED
from contracts.node_protocol import NodeProcessIdentity, NodeProducerV2

TICKS = 31751781


def running_app(pid: int, epoch: int) -> RunningApp:
    return RunningApp(environment("a"), NodeProcessIdentity(pid, TICKS + pid, uuid4()), epoch, uuid4())


def broker_loop(tmp_path, monkeypatch, *, granted: bool):
    """One BrokerLoop over fakes and a fake /proc: `state["running"]` is each turn's app run and
    `state["ms"]` the loop's clock."""
    monkeypatch.setattr(broker_runner, "boottime_ms", lambda: 1000)  # process evidence's time
    producer = NodeProducerV2("site", "device-" + "a" * 64, 1, uuid4(), "app_effect_broker", uuid4())
    store = boot_store(tmp_path / f"broker-{granted}", producer.kernel_boot_id)
    state = {"running": None, "ms": 0}
    session = Session(producer, granted=granted)
    feed = Feed(512)
    probes = SimpleNamespace(check=lambda: None, publish_run=lambda run, recovery_may_be_armed: None,
                             owe_relink=lambda run: None, feed=feed, take_kill_due=lambda: None)
    online = SimpleNamespace(broker=SimpleNamespace(record=None, service=lambda: None),
                             tick=lambda: None)
    loop = BrokerLoop(broker=SimpleNamespace(reconcile=lambda: None), online=online, store=store,
                      session=session, driver=SimpleNamespace(current=lambda: state["running"]),
                      links=SimpleNamespace(serve_one=lambda: None, remember_grant=lambda: None),
                      probes=probes, feeds=SimpleNamespace(serve=lambda: None),
                      clock=lambda: state["ms"], proc=tmp_path / f"proc-{granted}")
    return loop, feed, state, store


def facts(feed, *kinds):
    return [(event.kind, event.value) for event in feed.read(0, incarnation=None, limit=64).events
            if event.kind in kinds]


@pytest.mark.parametrize("granted", [True, False])
def test_one_exit_and_one_start_per_run_change_with_or_without_a_grant(tmp_path, monkeypatch,
                                                                       granted):
    loop, feed, state, store = broker_loop(tmp_path, monkeypatch, granted=granted)
    try:
        first, second = running_app(396, 1), running_app(397, 2)
        for running in (None, first, first, first, None, None, second, second):
            state["running"] = running
            loop.turn()
        one, two = AppRunKey.of(first).document(), AppRunKey.of(second).document()
        assert facts(feed, APP_STARTED, APP_EXITED) == [
            (APP_STARTED, {"run": one}), (APP_EXITED, {"run": one}), (APP_STARTED, {"run": two})]
        state["running"] = running_app(398, 3)  # one turn sees the run replaced outright
        loop.turn()
        three = AppRunKey.of(state["running"]).document()
        assert facts(feed, APP_STARTED, APP_EXITED)[-2:] == [
            (APP_EXITED, {"run": two}), (APP_STARTED, {"run": three})]
    finally:
        store.close()


def test_open_files_are_sampled_once_per_sample_period(tmp_path, monkeypatch):
    loop, feed, state, store = broker_loop(tmp_path, monkeypatch, granted=False)
    try:
        running = running_app(396, 1)
        proc_tree(loop.proc, 396, TICKS + 396, open_files=12)
        state["running"] = running
        for ms in range(0, 2 * DESCRIPTOR_SAMPLE_MS + 1, 1000):  # a turn each second
            state["ms"] = ms
            loop.turn()
        run = AppRunKey.of(running).document()
        assert facts(feed, APP_DESCRIPTORS) == [
            (APP_DESCRIPTORS, {"run": run, "open": 12, "soft_limit": 1024})] * 3
    finally:
        store.close()


def test_a_pid_now_naming_another_process_is_not_sampled(tmp_path, monkeypatch):
    loop, feed, state, store = broker_loop(tmp_path, monkeypatch, granted=False)
    try:
        proc_tree(loop.proc, 396, TICKS + 1, open_files=12)  # pid 396 was reused
        state["running"] = running_app(396, 1)
        loop.turn()
        state["ms"] = DESCRIPTOR_SAMPLE_MS
        loop.turn()
        assert facts(feed, APP_DESCRIPTORS) == []
        assert [kind for kind, _ in facts(feed, APP_STARTED)] == [APP_STARTED]
    finally:
        store.close()
