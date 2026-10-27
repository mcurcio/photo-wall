"""The app broker waits out an app unit between states, so a Weston and Player restart never crashes
it (display tuning, decision 0024: every signal range change restarts Weston).

Seen on the test Pi (journal of photo-wall-app-broker, 2026-10-13, during the design's Weston
restart): BrokerLoop.turn -> AppEffectBroker.reconcile -> SystemdAppProcessDriver.current ->
_observe raised ValueError("app_process_ambiguous") for the Player's unit while it was
deactivating, the broker exited 1 and systemd restarted it.

The real BrokerLoop turn runs the real AppEffectBroker over the real SystemdAppProcessDriver;
only its edges are scripted: `systemctl show` rows, /proc and the spawn (`driver.start`).
"""
from __future__ import annotations

import subprocess
from uuid import uuid4

import pytest
from node.test_node_linux_adapters import store as boot_store
from node.test_node_probe_broker import loop_for
from test_node_boot import environment

from appliance.apps import process_linux as linux
from appliance.apps.broker import AppEffectBroker, ColdStart, EffectRecord, RunningApp
from appliance.apps.lifecycle_storage import FileEffectJournal, primitive
from appliance.kernel.app_facts import APP_OBSERVATION_FAILED
from contracts.node_protocol import NodeProcessIdentity

GROUP = "/photowallapp.slice/" + linux.UNIT
DISPLAY = str(uuid4())


class Unit:
    """The Player's unit as `systemctl show` answers, and the processes /proc holds."""

    def __init__(self, driver, running: RunningApp) -> None:
        self.driver = driver
        self.rows: dict[str, str] = {}
        self.ticks: dict[int, int] = {}
        self.launches: list[str] = []          # the unit's state at each spawn
        self.active(running)

    def active(self, running: RunningApp) -> None:
        pid = running.process.pid
        self.rows = dict(LoadState="loaded", ActiveState="active", SubState="running", MainPID=str(pid),
                         InvocationID=str(running.process.invocation_id), ControlGroup=GROUP,
                         RootDirectory=str(self.driver.roots / running.environment.environment_sha256 / "rootfs"))
        self.ticks[pid] = running.process.start_ticks
        (self.driver.proc / str(pid)).mkdir(parents=True, exist_ok=True)
        (self.driver.proc / str(pid) / "cgroup").write_text(f"0::{GROUP}\n")

    def state(self, active: str, *, pid: str | None = None, load: str = "loaded") -> None:
        self.rows = {**self.rows, "ActiveState": active, "LoadState": load,
                     "MainPID": self.rows["MainPID"] if pid is None else pid}
        if self.rows["MainPID"] == "0":
            self.rows.update(InvocationID="", ControlGroup="", RootDirectory="")
            self.ticks.clear()

    def show(self, unit, properties=linux.APP_PROPERTIES):
        if unit == linux.DISPLAY_UNIT:
            return {"ActiveState": "active", "InvocationID": DISPLAY, "Job": ""}
        return dict(self.rows)


@pytest.fixture
def broker_turn(tmp_path, monkeypatch):
    """A broker turn on a cold start whose Player runs (pid 321), on a Weston that Display has
    since restarted on purpose (its mark names the Weston the Player was launched under, so a
    Player that Weston took down starts again once)."""
    loop, _feed, _session, _driver, _running, store = loop_for(tmp_path, monkeypatch)
    command = ColdStart(uuid4(), uuid4(), uuid4(), environment("a"))
    running = RunningApp(command.environment, NodeProcessIdentity(321, 1234, uuid4()), 1, command.operation_id)
    launched_under = uuid4()
    (tmp_path / "restarted").write_text(launched_under.hex + "\n")  # Display's mark
    driver = linux.SystemdAppProcessDriver(tmp_path / "roots", store, base_abi="base-v2",
                                           graphics_abi="graphics-v2", plugin_abi="plugins-v2",
                                           proc=tmp_path / "proc", cgroups=tmp_path / "cgroups",
                                           purposeful=tmp_path / "restarted")
    unit = Unit(driver, running)
    monkeypatch.setattr(linux, "systemctl_show", unit.show)
    monkeypatch.setattr(linux, "read_proc_start_ticks", lambda proc, pid: unit.ticks.get(pid))
    monkeypatch.setattr(linux, "process_root_matches", lambda *args: True)
    monkeypatch.setattr(linux, "boottime_ms", lambda: 50_000)       # CLOCK_BOOTTIME is Linux-only
    monkeypatch.setattr(linux.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args, 0, "\n", ""))                                              # `systemctl show --property=Job`
    store.write("launch", {"environment": primitive(command.environment), "operation_id": str(command.operation_id),
                           "epoch": 1, "running": primitive(running), "display": str(launched_under),
                           "started_ms": 0})

    def start(environment, operation_id):
        unit.launches.append(f"{unit.rows['LoadState']}/{unit.rows['ActiveState']}")
        launch = store.read("launch")
        again = RunningApp(environment, NodeProcessIdentity(400 + len(unit.launches), 5678, uuid4()),
                           launch["epoch"] + 1, operation_id)
        store.write("launch", {**launch, "epoch": again.app_epoch, "running": primitive(again), "display": DISPLAY})
        unit.active(again)
        return again
    driver.start = start
    journal = FileEffectJournal(boot_store(tmp_path / "journal", command.kernel_boot_id))
    journal.put(EffectRecord(command, "running", running))
    loop.broker = AppEffectBroker(boot_id=command.kernel_boot_id, offer_id=command.offer_id,
                                  authorized_environment=command.environment, journal=journal,
                                  driver=driver)
    loop.driver = driver
    loop.online.broker.record = None                                 # the cold start owns the app
    yield loop, unit, journal
    loop.probes.close()
    store.close()


def test_a_weston_restart_relaunches_the_player_once_it_is_gone_and_the_turn_never_raises(broker_turn):
    """Acceptance 1: Display restarts Weston on purpose: active -> deactivating -> inactive
    (MainPID 0) -> collected: every turn returns, nothing is started while the unit settles, and
    the Player is started exactly once, after its unit is gone. Mutation probe: raise the plain
    ValueError("app_process_ambiguous") for 'deactivating' -> the turn raises."""
    loop, unit, journal = broker_turn
    loop.turn()
    unit.state("deactivating")                                       # Weston stops the Player
    for _ in range(3):
        loop.turn()
    assert unit.launches == [] and journal.current().phase == "running"
    unit.state("inactive", pid="0")                                  # stopped, not yet collected
    loop.turn()
    assert unit.launches == []
    unit.state("inactive", pid="0", load="not-found")                # --collect unloaded it
    loop.turn()
    loop.turn()
    assert unit.launches == ["not-found/inactive"]
    record = journal.current()
    assert record.phase == "running" and record.running.process.pid == 401


def test_activating_right_after_a_launch_is_waited_out_never_judged_an_exit(broker_turn):
    """Acceptance 2: a unit still activating after its launch is an unknown turn: the record stays
    running, no exit is judged and nothing is launched again. Mutation probe: treat 'activating'
    as absent -> the broker judges the run exited (record `exited`, exited_under recorded)."""
    loop, unit, journal = broker_turn
    unit.state("activating")
    for _ in range(3):
        loop.turn()
    assert journal.current().phase == "running" and journal.current().fault is None
    assert journal.current().exited_under is None and unit.launches == []
    unit.state("active")
    loop.turn()
    assert journal.current().phase == "running" and unit.launches == []


def systemd_started_it_again(unit: Unit, pid: int = 777) -> NodeProcessIdentity:
    """systemd's own restart of the Player's unit (the 2026-10-13 incident's try-restart): the same
    unit, root and cgroup, a new process and InvocationID the broker never launched."""
    process = NodeProcessIdentity(pid, 9999, uuid4())
    unit.rows.update(MainPID=str(pid), InvocationID=str(process.invocation_id))
    unit.ticks[pid] = process.start_ticks
    (unit.driver.proc / str(pid)).mkdir(parents=True, exist_ok=True)
    (unit.driver.proc / str(pid) / "cgroup").write_text(f"0::{GROUP}\n")
    return process


def test_a_run_of_the_same_root_the_broker_did_not_start_is_adopted_and_the_turn_never_raises(broker_turn):
    """Incident 2026-10-13 (v0.35.0): a signal range change restarted Weston, systemd started the
    Player again itself, and every broker turn raised app_process_incarnation_mismatch until the
    broker's start limit left it failed until reboot. A run whose root, cgroup and environment
    match the launch is the broker's own Player: adopted as a new launch (its identity and the
    running Weston recorded), never relaunched, never fatal. Mutation probe: raise
    app_process_incarnation_mismatch again in SystemdAppProcessDriver.current -> the turn raises."""
    loop, unit, journal = broker_turn
    store = loop.driver.store
    loop.turn()
    epoch = store.read("launch")["epoch"]
    process = systemd_started_it_again(unit)
    for _ in range(3):
        loop.turn()
    launch = store.read("launch")
    assert launch["running"]["process"]["pid"] == 777
    assert launch["running"]["process"]["invocation_id"] == str(process.invocation_id)
    assert launch["display"] == DISPLAY and launch["epoch"] == epoch + 1   # the running Weston's
    record = journal.current()
    assert record.phase == "running" and record.fault is None and record.running.process == process
    assert unit.launches == []                                       # adopted, never relaunched
    assert loop.driver.current().process == process


def test_an_observation_fault_is_reported_and_backed_off_never_fatal(broker_turn):
    """A driver ValueError (a unit failed with a main process left: app_process_ambiguous) is a
    fault reported once on the node feed for the health judge, observed again on a backoff, and
    never the broker's exit; it clears once the unit is readable again. Mutation probe: let
    BrokerLoop.turn's reconcile raise -> the turn raises."""
    loop, unit, journal = broker_turn
    reconciles = []
    reconcile = loop.broker.reconcile
    loop.broker.reconcile = lambda: (reconciles.append(loop.now), reconcile())[1]
    unit.state("failed")                                             # MainPID still set
    for _ in range(10):                                              # 1 s of turns
        loop.turn()
        loop.now += 100
    faults = [e.value for e in loop.probes.feed.read(0, incarnation=None, limit=64).events
              if e.kind == APP_OBSERVATION_FAILED]
    assert faults == [{"reason": "app_process_ambiguous"}]
    assert reconciles == [0]                                         # backed off, not every turn
    assert journal.current().phase == "running"
    unit.state("active")
    for _ in range(30):
        loop.turn()
        loop.now += 100
    assert len(reconciles) >= 2 and reconciles[1] >= 1_000
    assert loop.observation_fault is None and journal.current().phase == "running"


@pytest.mark.parametrize("change", [{"RootDirectory": "/elsewhere"},
                                    {"ControlGroup": "/system.slice/other.service"}])
def test_a_unit_that_is_not_ours_is_stopped_and_nothing_starts_in_its_place(broker_turn, change,
                                                                           monkeypatch):
    """A unit under the Player's name that is not the launch's (another root, another cgroup) is
    never adopted and never the settling wait: the broker stops it and starts nothing in its
    place, though Display's mark is there (issue #125). Mutation probes: let AppUnitSettling cover
    it -> nothing is stopped; start the broker's own Player after the stop -> a launch."""
    loop, unit, _journal = broker_turn
    store = loop.driver.store
    store.write("launch", {**store.read("launch"), "display": DISPLAY})  # launched under this Weston
    unit.rows.update(change)
    with pytest.raises(linux.ForeignAppUnit):
        loop.driver.current()
    stops = []

    def systemctl(command, **kwargs):
        if "stop" in command:
            stops.append(command)
            unit.state("inactive", pid="0", load="not-found")
        return subprocess.CompletedProcess(command, 0, "\n", "")
    monkeypatch.setattr(linux.subprocess, "run", systemctl)
    for _ in range(20):
        loop.turn()
        loop.now += 100
    assert stops == [["/usr/bin/systemctl", "--no-block", "stop", linux.UNIT]]
    assert unit.launches == []
    assert loop.broker.journal.current().phase == "down"     # stopped under the running Weston


@pytest.mark.parametrize("state", sorted(linux.SETTLING_STATES))
def test_each_state_between_is_the_settling_wait(broker_turn, state):
    _loop, unit, _journal = broker_turn
    unit.state(state)
    with pytest.raises(linux.AppUnitSettling) as raised:
        _loop.driver.current()
    assert raised.value.state == state
