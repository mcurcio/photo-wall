"""The broker's main loop reports each app run's start and exit, and samples its open files,
for the health judge (1b P1b; `appliance.kernel.app_facts`), and tells Display the current run
(decision 0034, S4b part 4). The broker speaks to no Central (S4c)."""
from types import SimpleNamespace
from uuid import uuid4

from node.apps.test_app_descriptors import proc_tree
from node.test_node_linux_adapters import store as boot_store
from test_node_boot import environment

from appliance.apps import broker_runner
from appliance.apps.broker import Launch, RunningApp
from appliance.apps.broker_runner import BrokerLoop
from appliance.apps.descriptors import DESCRIPTOR_SAMPLE_MS
from appliance.apps.probe import AppRunKey
from appliance.feed import Feed
from appliance.kernel.app_facts import APP_DESCRIPTORS, APP_EXITED, APP_STARTED, RUN_RESTATE_MS
from contracts.node_protocol import NodeProcessIdentity

TICKS = 31751781


def running_app(pid: int, epoch: int) -> RunningApp:
    return RunningApp(environment("a"), NodeProcessIdentity(pid, TICKS + pid, uuid4()), epoch, uuid4())


def broker_loop(tmp_path, monkeypatch):
    """One BrokerLoop over fakes and a fake /proc: `state["running"]` is each turn's app run and
    `state["ms"]` the loop's clock."""
    monkeypatch.setattr(broker_runner, "boottime_ms", lambda: 1000)
    store = boot_store(tmp_path / "broker", uuid4())
    state = {"running": None, "ms": 0}
    feed = Feed(512)
    told: list[AppRunKey | None] = []
    probes = SimpleNamespace(check=lambda: None, publish_run=lambda run: None, feed=feed)
    loop = BrokerLoop(broker=SimpleNamespace(reconcile=lambda: None), store=store,
                      driver=SimpleNamespace(current=lambda: state["running"], launched=lambda: None),
                      listener=SimpleNamespace(serve_one=lambda: None),
                      probes=probes, feeds=SimpleNamespace(serve=lambda: None),
                      display=SimpleNamespace(tell=lambda run: told.append(run) or True),
                      clock=lambda: state["ms"], proc=tmp_path / "proc")
    loop.told = told
    return loop, feed, state, store


def facts(feed, *kinds):
    return [(event.kind, event.value) for event in feed.read(0, incarnation=None, limit=64).events
            if event.kind in kinds]


def test_one_exit_and_one_start_per_run_change(tmp_path, monkeypatch):
    loop, feed, state, store = broker_loop(tmp_path, monkeypatch)
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


def test_every_known_turn_hands_display_the_current_run(tmp_path, monkeypatch):
    """`DisplayRuns.tell` gets each known turn's run (it alone decides whether to send)."""
    loop, _feed, state, store = broker_loop(tmp_path, monkeypatch)
    try:
        first = running_app(396, 1)
        for running in (None, first, first, None):
            state["running"] = running
            loop.turn()
        assert loop.told == [None, AppRunKey.of(first), AppRunKey.of(first), None]
    finally:
        store.close()


def test_open_files_are_sampled_once_per_sample_period(tmp_path, monkeypatch):
    loop, feed, state, store = broker_loop(tmp_path, monkeypatch)
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
    loop, feed, state, store = broker_loop(tmp_path, monkeypatch)
    try:
        proc_tree(loop.proc, 396, TICKS + 1, open_files=12)  # pid 396 was reused
        state["running"] = running_app(396, 1)
        loop.turn()
        state["ms"] = DESCRIPTOR_SAMPLE_MS
        loop.turn()
        assert facts(feed, APP_DESCRIPTORS) == []
        assert facts(feed, APP_STARTED)  # the run started; only its open files are not sampled
    finally:
        store.close()


def test_the_holding_run_state_is_restated_until_it_changes(tmp_path, monkeypatch):
    """A reader that missed a statement (a feed gap, a new broker incarnation, the ring's
    eviction) reads it again within RUN_RESTATE_MS: `app_started` while the run is current,
    `app_exited` once it is not. Mutation probe: drop the restatement -> one statement each."""
    loop, feed, state, store = broker_loop(tmp_path, monkeypatch)
    try:
        running = running_app(396, 1)
        loop.driver.launched = lambda: Launch(1, None, 0, running)  # the record outlives the run
        state["running"] = running
        for ms in range(0, 2 * RUN_RESTATE_MS, 1000):  # a turn each second
            state["ms"] = ms
            loop.turn()
        state["running"] = None
        for ms in range(2 * RUN_RESTATE_MS, 4 * RUN_RESTATE_MS, 1000):
            state["ms"] = ms
            loop.turn()
        run = AppRunKey.of(running).document()
        assert facts(feed, APP_STARTED, APP_EXITED) == [
            (APP_STARTED, {"run": run}), (APP_STARTED, {"run": run}),
            (APP_EXITED, {"run": run}), (APP_EXITED, {"run": run})]
    finally:
        store.close()
