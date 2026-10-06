"""Kill after K behind the Q1 predicate: the recovery guard, the host acknowledgement, the
main loop's kill consumer and the driver's identity-checked SIGKILL."""
import os
import signal
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_node_boot import environment
from test_node_linux_adapters import store as boot_store
from test_node_online_broker import Driver as SwitchDriver
from test_node_online_broker import stage
from test_node_probe_broker import DUE, FAST, loop_for, turns

from appliance.apps.broker import RunningApp
from appliance.apps.broker_runner import BrokerLoop
from appliance.apps.online_broker import OnlineEffectBroker
from appliance.apps.probe import (
    KILL_AFTER_MS,
    PROBE_PERIOD_MS,
    RECOVERY_ACKNOWLEDGED,
    RECOVERY_ARMED,
    AppRunKey,
    KillDue,
    ProbeClock,
    recovery_may_be_armed,
)
from appliance.feed import Feed
from contracts.node_protocol import NodeProcessIdentity, NodeProducerV2


def events(feed, kind):
    taken, after = [], 0
    while True:
        page = feed.read(after, incarnation=None, limit=8)
        if not page.events:
            return [e.value for e in taken if e.kind == kind]
        taken += page.events
        after = page.events[-1].sequence


# -- the predicate (pure) ------------------------------------------------------------------


def test_recovery_may_be_armed_only_for_an_unacknowledged_obligation():
    operation = str(uuid4())
    named = {"operation_id": operation}
    record = {"phase": "running", "recovery": named}
    assert recovery_may_be_armed(None, None, None) is False  # cold start: nothing armed
    assert recovery_may_be_armed({"phase": "running"}, None, None) is False
    assert recovery_may_be_armed(record, named, None) is True
    assert recovery_may_be_armed(record, named, {"operation_id": str(uuid4())}) is True  # older
    assert recovery_may_be_armed(record, named, named) is False
    for unreadable in ({}, {"operation_id": None}, None, "x"):
        assert recovery_may_be_armed({"phase": "running", "recovery": unreadable}, named,
                                     named) is True
    for unreadable in ({}, {"operation_id": None}, {"operation_id": ""}, "x"):
        assert recovery_may_be_armed(None, unreadable, named) is True
    assert recovery_may_be_armed("x", None, None) is True


def test_the_last_armed_obligation_fences_a_record_that_no_longer_names_it():
    """E-B8-7: the online record is replaceable before control is acknowledged, so the fence
    reads the last armed obligation from its own key, not from the record."""
    older, newer = {"operation_id": str(uuid4())}, {"operation_id": str(uuid4())}
    assert recovery_may_be_armed({"phase": "running"}, older, None) is True
    assert recovery_may_be_armed(None, older, None) is True
    assert recovery_may_be_armed({"phase": "running", "recovery": newer}, older, newer) is True
    assert recovery_may_be_armed({"phase": "running", "recovery": newer}, newer, older) is True
    assert recovery_may_be_armed({"phase": "fallback_running", "recovery": newer}, newer,
                                 newer) is False


@pytest.mark.parametrize("phase", ["preparing", "intent_stop", "stopped", "target_intent",
                                   "target_failed", "fallback_intent", "effect_unknown"])
def test_a_record_that_is_not_settled_counts_as_armed(phase):
    named = {"operation_id": str(uuid4())}
    assert recovery_may_be_armed({"phase": phase}, None, None) is True
    assert recovery_may_be_armed({"phase": phase, "recovery": named}, named, named) is True


def test_overdue_is_a_level_from_k_until_an_answer():
    run = AppRunKey(uuid4(), 4242, 31751781, 1)
    clock = ProbeClock(run, 0)
    now = 0
    while now + PROBE_PERIOD_MS < KILL_AFTER_MS:
        now += PROBE_PERIOD_MS
        clock.turn(now, late=False)
        assert not clock.overdue, now
    now += PROBE_PERIOD_MS
    assert [f.kind for f in clock.turn(now, late=False)][-1] == "probe_kill_due"
    for _ in range(3):  # the fact is once; the level stays
        now += PROBE_PERIOD_MS
        assert "probe_kill_due" not in [f.kind for f in clock.turn(now, late=False)]
        assert clock.overdue and clock.unanswered_ms >= KILL_AFTER_MS
    clock.sent("a" * 64, now)
    assert clock.answered("a" * 64, now + 5) and not clock.overdue


# -- the host acknowledgement (online_broker.service) --------------------------------------


@pytest.fixture
def switched(tmp_path, monkeypatch):
    """An online switch that completed: the record carries the recovery obligation."""
    import appliance.apps.online_broker as module
    monkeypatch.setattr(module, "boottime_ms", lambda: 1000)
    monkeypatch.setattr(module, "memory_values", lambda: (8 * 1024**3, 7 * 1024**3))
    producer = NodeProducerV2("site", "device-" + "a" * 64, 1, uuid4(), "app_effect_broker", uuid4())
    old = RunningApp(environment("a"), NodeProcessIdentity(100, 200, uuid4()), 1, uuid4())
    command = stage(producer, old, environment("b"), old.environment, offer_id=uuid4())
    journal = boot_store(tmp_path, producer.kernel_boot_id)
    session = SimpleNamespace(grant=SimpleNamespace(producer=producer, session_id=uuid4(),
        offer_id=command.offer_id), claim=None, request=lambda *args, **kwargs: (200, b"{}"))
    advanced = []
    recovery = SimpleNamespace(arm=lambda obligation: obligation.receipt,
                               advance=lambda obligation, progress: advanced.append(progress))
    broker = OnlineEffectBroker(journal, SwitchDriver(old), session, recovery)
    broker.accept(command)
    broker.execute()
    assert broker.record["phase"] == "running" and "recovery" in broker.record
    yield broker, journal, command, recovery, advanced
    journal.close()


def armed(broker, journal):
    return recovery_may_be_armed(broker.record, journal.read(RECOVERY_ARMED),
                                 journal.read(RECOVERY_ACKNOWLEDGED))


def acknowledge(broker, journal, command):
    journal.write("local-app-control", {"operation_id": str(command.operation_id),
                                        "progress": {"kind": "controlled"}})
    broker.service()


def next_stage(broker, command):
    current = broker.driver.current()
    return stage(command.producer, current, environment("c"), current.environment,
                 offer_id=command.offer_id)


def test_each_successful_arm_records_the_armed_obligation(switched):
    broker, journal, command, recovery, _ = switched
    assert journal.read(RECOVERY_ARMED) == {"operation_id": str(command.operation_id)}
    refused = next_stage(broker, command)
    acknowledge(broker, journal, command)
    recovery.arm = lambda obligation: "not-the-receipt"
    broker.accept(refused)
    with pytest.raises(ValueError, match="online_recovery_receipt"):
        broker.execute()
    assert journal.read(RECOVERY_ARMED) == {"operation_id": str(command.operation_id)}
    assert armed(broker, journal) is True  # the refused switch's record is mid-switch


def test_a_newer_stage_replacing_an_unacknowledged_running_record_stays_armed(switched):
    """E-B8-7: accept() replaces the `running` record (and its `recovery`) before the host
    acknowledged the old obligation's control; the obligation is still armed."""
    broker, journal, command, _, _ = switched
    assert armed(broker, journal) is True
    broker.accept(next_stage(broker, command))
    assert broker.record["phase"] == "preparing" and "recovery" not in broker.record
    assert armed(broker, journal) is True
    # Even were the replaced record settled, the armed key alone keeps the fence closed.
    assert recovery_may_be_armed({**broker.record, "phase": "running"},
                                 journal.read(RECOVERY_ARMED),
                                 journal.read(RECOVERY_ACKNOWLEDGED)) is True


def test_a_first_stage_in_preparation_counts_as_armed(switched):
    broker, journal, command, _, _ = switched
    acknowledge(broker, journal, command)
    assert armed(broker, journal) is False
    broker.accept(next_stage(broker, command))
    assert broker.record["phase"] == "preparing" and armed(broker, journal) is True


def test_an_acknowledgement_of_an_older_obligation_does_not_disarm_a_newer_one(switched):
    broker, journal, command, _, _ = switched
    acknowledge(broker, journal, command)
    assert armed(broker, journal) is False
    newer = next_stage(broker, command)
    broker.accept(newer)
    broker.execute()
    assert broker.record["phase"] == "running"
    assert journal.read(RECOVERY_ARMED) == {"operation_id": str(newer.operation_id)}
    assert journal.read(RECOVERY_ACKNOWLEDGED) == {"operation_id": str(command.operation_id)}
    assert armed(broker, journal) is True  # a stale acknowledgement
    acknowledge(broker, journal, newer)
    assert armed(broker, journal) is False


def test_controlled_progress_acknowledges_the_obligation_once(switched):
    broker, journal, command, _, advanced = switched
    assert armed(broker, journal) is True
    broker.service()  # no proof yet: the stopped branch is not an acknowledgement
    assert advanced == [{"kind": "stopped"}] and armed(broker, journal) is True
    progress = {"kind": "controlled", "detail": 1}
    journal.write("local-app-control", {"operation_id": str(command.operation_id),
                                        "progress": progress})
    writes = []
    real_write = journal.write
    journal.write = lambda name, value: (writes.append(name), real_write(name, value))[1]
    broker.service()
    broker.service()
    assert advanced[-2:] == [progress, progress]
    assert journal.read(RECOVERY_ACKNOWLEDGED) == {"operation_id": str(command.operation_id)}
    assert writes == [RECOVERY_ACKNOWLEDGED] and armed(broker, journal) is False


def test_a_refused_or_foreign_proof_acknowledges_nothing(switched):
    broker, journal, command, recovery, _ = switched
    journal.write("local-app-control", {"operation_id": str(uuid4()), "progress": {}})
    broker.service()
    assert journal.read(RECOVERY_ACKNOWLEDGED) is None
    journal.write("local-app-control", {"operation_id": str(command.operation_id), "progress": {}})

    def refused(obligation, progress):
        raise ValueError("recovery_receipt")

    recovery.advance = refused
    with pytest.raises(ValueError, match="recovery_receipt"):
        broker.service()
    assert journal.read(RECOVERY_ACKNOWLEDGED) is None and armed(broker, journal) is True


# -- the main loop's kill consumer (real probe thread, FAST timing, stepped by turn) -------


def killer(tmp_path, monkeypatch, **kwargs):
    loop, feed, session, driver, running, store = loop_for(tmp_path, monkeypatch, **kwargs)
    return loop, feed, driver, running, store


def test_cold_start_kills_after_k_once_without_a_grant(tmp_path, monkeypatch):
    """No online record (cold start): nothing guards the app, so a starved run is killed at K
    and not before; no grant is needed; a killed run is never signalled again."""
    loop, feed, driver, running, store = killer(tmp_path, monkeypatch, granted=False)
    loop.online.broker.record = None
    try:
        turns(loop, DUE)  # kill-due at the last pass: no turn has taken it yet
        assert driver.kills == []
        turns(loop, DUE)  # the fake app lives on: still overdue, still published
        assert driver.kills == [running]
        [killed] = events(feed, "app_killed")
        assert killed["run"] == AppRunKey.of(running).document()
        assert killed["reason"] == "unresponsive" and killed["unanswered_ms"] >= FAST.kill_after_ms
        assert events(feed, "kill_withheld") == []
    finally:
        loop.probes.close()
        store.close()


def test_an_armed_recovery_withholds_the_kill(tmp_path, monkeypatch):
    loop, feed, driver, running, store = killer(tmp_path, monkeypatch)
    loop.online.broker.record = {"phase": "running", "recovery": {"operation_id": str(uuid4())}}
    try:
        turns(loop, 2 * DUE)
        assert driver.kills == []
        assert events(feed, "probe_kill_due")
        assert events(feed, "kill_withheld") == [
            {"run": AppRunKey.of(running).document(), "reason": "recovery_armed"}]
        assert events(feed, "app_killed") == []
        assert loop.probes.recovery_may_be_armed is True
    finally:
        loop.probes.close()
        store.close()


def test_an_armed_obligation_the_record_no_longer_names_withholds_the_kill(tmp_path, monkeypatch):
    """E-B8-7 at the main loop: the predicate reads the `recovery-armed` key every turn."""
    loop, feed, driver, running, store = killer(tmp_path, monkeypatch)
    loop.online.broker.record = {"phase": "running"}  # replaced; its obligation unacknowledged
    store.write(RECOVERY_ARMED, {"operation_id": str(uuid4())})
    try:
        turns(loop, 2 * DUE)
        assert driver.kills == [] and events(feed, "app_killed") == []
        assert events(feed, "kill_withheld") == [
            {"run": AppRunKey.of(running).document(), "reason": "recovery_armed"}]
    finally:
        loop.probes.close()
        store.close()


def test_withheld_then_acknowledged_while_still_unanswered_is_killed(tmp_path, monkeypatch):
    """E-AP2-1: the kill-due level comes back after the acknowledgement."""
    operation = str(uuid4())
    loop, feed, driver, running, store = killer(tmp_path, monkeypatch)
    loop.online.broker.record = {"phase": "running", "recovery": {"operation_id": operation}}
    store.write(RECOVERY_ARMED, {"operation_id": operation})
    try:
        turns(loop, DUE + 2)
        assert driver.kills == [] and events(feed, "kill_withheld")
        store.write(RECOVERY_ACKNOWLEDGED, {"operation_id": operation})
        turns(loop, 1)  # the level is back on the next turn
        assert driver.kills == [running]
        assert len(events(feed, "app_killed")) == 1
    finally:
        loop.probes.close()
        store.close()


class Flaky:
    """A driver whose `current()` fails while `failing` is set."""

    def __init__(self, running):
        self.running, self.failing, self.kills = running, False, []

    def current(self):
        if self.failing:
            raise OSError("systemctl unavailable")
        return self.running

    def kill(self, expected):
        self.kills.append(expected)
        return True


def test_a_turn_whose_observation_failed_takes_no_kill(tmp_path, monkeypatch):
    loop, feed, _, running, store = killer(tmp_path, monkeypatch)
    loop.online.broker.record = None
    loop.driver = driver = Flaky(running)
    taken = []
    take = loop.probes.take_kill_due
    try:
        loop.turn()  # the run is published once; the probe thread keeps judging it
        loop.probes.take_kill_due = lambda: taken.append(1) or take()
        driver.failing = True
        turns(loop, 2 * DUE)  # well past K: kill due, but no turn knows the current run
        assert taken == [] and driver.kills == [] and events(feed, "probe_kill_due")
        driver.failing = False
        loop.turn()
        assert driver.kills == [running] and taken == [1]
    finally:
        loop.probes.close()
        store.close()


def scripted_loop(tmp_path, runs, dues, *, kill=True):
    """One BrokerLoop over fakes: turn i sees `runs[i]` and takes `dues[i]`."""
    producer = NodeProducerV2("site", "device-" + "a" * 64, 1, uuid4(), "app_effect_broker", uuid4())
    store = boot_store(tmp_path / "scripted", producer.kernel_boot_id)
    script = {"runs": list(runs), "dues": list(dues)}
    kills = []
    driver = SimpleNamespace(current=lambda: script["runs"].pop(0),
                             kill=lambda expected: kills.append(expected) or kill)
    probes = SimpleNamespace(check=lambda: None, publish_run=lambda run, recovery_may_be_armed: None,
                             owe_relink=lambda run: None, feed=Feed(64),
                             take_kill_due=lambda: script["dues"].pop(0))
    online = SimpleNamespace(broker=SimpleNamespace(record=None, service=lambda: None),
                             tick=lambda: None)
    loop = BrokerLoop(broker=SimpleNamespace(reconcile=lambda: None), online=online, store=store,
                      session=SimpleNamespace(ensure=lambda: None), driver=driver,
                      links=SimpleNamespace(serve_one=lambda: None, remember_grant=lambda: None),
                      probes=probes, feeds=SimpleNamespace(serve=lambda: None))
    return loop, probes.feed, kills, store


def running_app(pid):
    return RunningApp(environment("a"), NodeProcessIdentity(pid, 31751781 + pid, uuid4()), 1, uuid4())


def test_a_kill_due_for_a_run_that_is_not_this_turns_run_is_withheld(tmp_path):
    first, second = running_app(396), running_app(397)
    due = KillDue(AppRunKey.of(first), 400)
    loop, feed, kills, store = scripted_loop(tmp_path, [first, second, None], [None, due, due])
    try:
        for _ in range(3):
            loop.turn()
        assert kills == []
        assert events(feed, "kill_withheld") == [
            {"run": AppRunKey.of(first).document(), "reason": "run_changed"}]
        assert events(feed, "app_killed") == []
    finally:
        store.close()


def test_an_identity_mismatch_at_the_signal_kills_nothing(tmp_path):
    app = running_app(396)
    due = KillDue(AppRunKey.of(app), 400)
    loop, feed, kills, store = scripted_loop(tmp_path, [app, app], [due, due], kill=False)
    try:
        loop.turn()
        loop.turn()
        assert kills == [app, app]  # the driver re-checked and refused both times
        assert events(feed, "app_killed") == []
        assert events(feed, "kill_withheld") == [
            {"run": AppRunKey.of(app).document(), "reason": "run_changed"}]
    finally:
        store.close()


def test_an_unobservable_identity_sends_nothing_and_is_retried(tmp_path):
    app = running_app(396)
    due = KillDue(AppRunKey.of(app), 400)
    loop, feed, kills, store = scripted_loop(tmp_path, [app, app], [due, due])
    outcomes = [subprocess.TimeoutExpired("systemctl", 5), True]

    def kill(expected):
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        kills.append(expected)
        return outcome

    loop.driver.kill = kill
    try:
        loop.turn()
        assert kills == [] and events(feed, "app_killed") == [] and events(feed, "kill_withheld") == []
        loop.turn()
        assert kills == [app] and len(events(feed, "app_killed")) == 1
    finally:
        store.close()


# -- the driver's identity-checked SIGKILL -------------------------------------------------


@pytest.fixture
def signals(tmp_path, monkeypatch):
    from appliance.apps import process_linux as linux

    expected = running_app(321)
    driver = linux.SystemdAppProcessDriver(tmp_path / "roots", None, base_abi="base-v2",
        graphics_abi="graphics-v2", plugin_abi="plugins-v2", proc=tmp_path / "proc")
    state = {"ticks": expected.process.start_ticks, "calls": [], "sent": [], "fds": [],
             "rows": {"MainPID": "321", "InvocationID": str(expected.process.invocation_id)}}

    def pidfd_open(pid):
        state["calls"].append(("pidfd_open", pid))
        if state.get("gone"):
            raise ProcessLookupError(pid)
        fd = os.open(os.devnull, os.O_RDONLY)
        state["fds"].append(fd)
        return fd

    def start_ticks(proc, pid):
        state["calls"].append(("start_ticks", pid))
        return state["ticks"]

    def show(unit):
        state["calls"].append(("systemctl_show", unit))
        return dict(state["rows"])

    def send(fd, number):
        if state.get("exited"):
            raise ProcessLookupError(fd)
        state["sent"].append((fd, number))

    monkeypatch.setattr(linux.os, "pidfd_open", pidfd_open, raising=False)
    monkeypatch.setattr(linux.signal, "pidfd_send_signal", send, raising=False)
    monkeypatch.setattr(linux, "read_proc_start_ticks", start_ticks)
    monkeypatch.setattr(linux, "systemctl_show", show)
    return driver, expected, state


def closed(fd):
    try:
        os.fstat(fd)
    except OSError:
        return True
    return False


def test_kill_signals_the_pinned_process_after_its_identity_matches(signals):
    driver, expected, state = signals
    assert driver.kill(expected) is True
    [fd] = state["fds"]
    assert state["sent"] == [(fd, signal.SIGKILL)] and closed(fd)
    assert [name for name, _ in state["calls"]] == ["pidfd_open", "start_ticks", "systemctl_show"]


def test_a_reused_pid_is_never_signalled(signals):
    driver, expected, state = signals
    state["ticks"] = expected.process.start_ticks + 1  # same pid, a different process birth
    assert driver.kill(expected) is False
    assert state["sent"] == [] and all(closed(fd) for fd in state["fds"])


@pytest.mark.parametrize("field, value", [("MainPID", "322"), ("MainPID", "0"),
                                          ("InvocationID", "6b8cd57b-2a55-4ba0-89bc-456463455201")])
def test_another_unit_main_process_is_never_signalled(signals, field, value):
    driver, expected, state = signals
    state["rows"][field] = value
    assert driver.kill(expected) is False and state["sent"] == []


def test_a_process_already_gone_is_not_signalled(signals):
    driver, expected, state = signals
    state["gone"] = True
    assert driver.kill(expected) is False
    assert state["sent"] == [] and [name for name, _ in state["calls"]] == ["pidfd_open"]
    state["gone"], state["exited"] = False, True  # exits between the check and the signal
    assert driver.kill(expected) is False and all(closed(fd) for fd in state["fds"])
