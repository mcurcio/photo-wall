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
from appliance.apps.lifecycle_storage import FileEffectJournal, FileRelaunchPacing, primitive
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
            return {"ActiveState": "active", "InvocationID": DISPLAY}
        return dict(self.rows)


@pytest.fixture
def broker_turn(tmp_path, monkeypatch):
    """A broker turn on a cold start whose Player runs (pid 321), on a Weston that has since
    restarted (the display took the app, so its relaunch is not paced)."""
    loop, _feed, _session, _driver, _running, store = loop_for(tmp_path, monkeypatch)
    command = ColdStart(uuid4(), uuid4(), uuid4(), environment("a"))
    running = RunningApp(command.environment, NodeProcessIdentity(321, 1234, uuid4()), 1, command.operation_id)
    driver = linux.SystemdAppProcessDriver(tmp_path / "roots", store, base_abi="base-v2",
                                           graphics_abi="graphics-v2", plugin_abi="plugins-v2",
                                           proc=tmp_path / "proc", cgroups=tmp_path / "cgroups")
    unit = Unit(driver, running)
    monkeypatch.setattr(linux, "systemctl_show", unit.show)
    monkeypatch.setattr(linux, "read_proc_start_ticks", lambda proc, pid: unit.ticks.get(pid))
    monkeypatch.setattr(linux, "process_root_matches", lambda *args: True)
    monkeypatch.setattr(linux.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args, 0, "\n", ""))                                              # `systemctl show --property=Job`
    store.write("launch", {"environment": primitive(command.environment), "operation_id": str(command.operation_id),
                           "epoch": 1, "running": primitive(running), "display": str(uuid4()), "started_ms": 0})

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
    pacing = FileRelaunchPacing(store)
    loop.broker = AppEffectBroker(boot_id=command.kernel_boot_id, offer_id=command.offer_id,
                                  authorized_environment=command.environment, journal=journal,
                                  driver=driver, pacing=pacing, now_ms=lambda: 50_000)
    loop.driver = driver
    loop.online.broker.record = None                                 # the cold start owns the app
    yield loop, unit, journal, pacing
    loop.probes.close()
    store.close()


def test_a_weston_restart_relaunches_the_player_once_it_is_gone_and_the_turn_never_raises(broker_turn):
    """Acceptance 1: active -> deactivating -> inactive (MainPID 0) -> collected: every turn
    returns, nothing is relaunched while the unit settles, and the Player is relaunched exactly
    once, after its unit is gone. Mutation probe: raise the plain
    ValueError("app_process_ambiguous") for 'deactivating' -> the turn raises."""
    loop, unit, journal, _pacing = broker_turn
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
    running, no exit is paced and nothing is launched again. Mutation probe: treat 'activating'
    as absent -> the broker judges the run exited (record `exited`, an exit paced)."""
    loop, unit, journal, pacing = broker_turn
    unit.state("activating")
    for _ in range(3):
        loop.turn()
    assert journal.current().phase == "running" and journal.current().fault is None
    assert pacing.get() is None and unit.launches == []
    unit.state("active")
    loop.turn()
    assert journal.current().phase == "running" and unit.launches == []


@pytest.mark.parametrize("change", [{"RootDirectory": "/elsewhere"}, {"ActiveState": "failed"},
                                    {"ControlGroup": "/system.slice/other.service"}])
def test_a_unit_that_is_not_ours_is_still_ambiguous(broker_turn, change):
    """Acceptance 3: the safety check is kept: a unit that is active but not ours (or failed with a
    main process) raises app_process_ambiguous, never the settling wait. Mutation probe: let
    AppUnitSettling cover it -> the error is the wait."""
    loop, unit, _journal, _pacing = broker_turn
    unit.rows.update(change)
    with pytest.raises(ValueError, match="^app_process_ambiguous$") as raised:
        loop.driver.current()
    assert not isinstance(raised.value, linux.AppUnitSettling)


@pytest.mark.parametrize("state", sorted(linux.SETTLING_STATES))
def test_each_state_between_is_the_settling_wait(broker_turn, state):
    _loop, unit, _journal, _pacing = broker_turn
    unit.state(state)
    with pytest.raises(linux.AppUnitSettling) as raised:
        _loop.driver.current()
    assert raised.value.state == state
