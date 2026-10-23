"""Issue #125: nothing restarts the Player on its own. Three causes start a run (cold start,
Display's purposeful Weston restart, a release a person applies); an exit, a missed ping, a Weston
crash or a foreign unit never does, and a run that exits under its own Weston is `down` until a
person reboots. The owner, 2026-10-20: "a human. dont do automated reboots yet. i want to see
every time it fails for now"; "No auto-restarts at all".

The cold-start owner (`AppEffectBroker`) runs over the demo's recording driver; Display's mark is
read by the real `SystemdAppProcessDriver.purposeful_restart` from a file, and the unit that
writes it is read from appliance/systemd. The switch's own cases are in test_node_online_broker.py,
the kill's in test_node_probe_broker.py, the deadline reboot's in test_node_stop_operation.py.
"""
from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from node.test_node_linux_adapters import store as boot_store
from support.repo import REPO

from appliance.apps import process_linux as linux
from appliance.apps.broker import (
    AppEffectBroker,
    ColdStart,
    EffectRecord,
    Launch,
    PurposefulRestart,
    restarted_on_purpose,
)
from appliance.apps.lifecycle_storage import FileEffectJournal
from appliance.kernel.display_paths import DISPLAY_UNIT, PURPOSEFUL_RESTART, SIGNAL_RANGE_UNIT
from contracts.app_environment import AppEnvironmentRefV2
from scripts.node_control_demo import MemoryEffectJournal, RecordingAppDriver

# Weston incarnations as systemctl shows an InvocationID; the driver records a UUID's text.
WESTON_1, WESTON_2, WESTON_3 = "a1" * 16, "b2" * 16, "c3" * 16


def env() -> AppEnvironmentRefV2:
    return AppEnvironmentRefV2("a" * 64, 100, "b" * 64, "player", "1.0", "arm64",
                               "c" * 64, "d" * 64, "/usr/bin/player", "base", "gfx", "plugin")


def cold(display: str = str(UUID(WESTON_1))):
    """A cold start under `display` (as the systemd driver records it: a UUID's text)."""
    driver = RecordingAppDriver()
    driver.display = display
    broker = AppEffectBroker(boot_id=UUID(int=1), offer_id=UUID(int=2), authorized_environment=env(),
                             journal=MemoryEffectJournal(), driver=driver)
    assert broker.cold_start(ColdStart(UUID(int=3), UUID(int=1), UUID(int=2), env())).phase == "running"
    return broker, driver


def starts(driver) -> int:
    return driver.calls.count("start_simulated_process")


def turns(broker, count: int = 10):
    return [broker.reconcile() for _ in range(count)][-1]


# -- the one rule ------------------------------------------------------------------------------


def test_a_purposeful_restart_is_32_lowercase_hex_digits():
    assert PurposefulRestart(WESTON_1).restarted == WESTON_1
    for bad in ("", WESTON_1.upper(), WESTON_1[:-1], WESTON_1 + "0", str(UUID(WESTON_1)), None, 1):
        with pytest.raises(ValueError, match="^purposeful_restart_invalid$"):
            PurposefulRestart(bad)


def test_restarted_on_purpose_needs_every_condition():
    launch = Launch(1, str(UUID(WESTON_1)), 0)
    mark = PurposefulRestart(WESTON_1)
    assert restarted_on_purpose(launch, WESTON_2, mark, exited_under=None)
    assert restarted_on_purpose(launch, str(UUID(WESTON_2)), mark, exited_under=WESTON_2)
    assert not restarted_on_purpose(None, WESTON_2, mark, exited_under=None)
    assert not restarted_on_purpose(launch, None, mark, exited_under=None)        # no Weston up
    assert not restarted_on_purpose(launch, WESTON_1, mark, exited_under=None)    # the same Weston
    assert not restarted_on_purpose(launch, WESTON_2, None, exited_under=None)    # no mark: a crash
    assert not restarted_on_purpose(launch, WESTON_2, PurposefulRestart(WESTON_3),
                                    exited_under=None)                             # another Weston's
    assert not restarted_on_purpose(launch, WESTON_2, mark,
                                    exited_under=str(UUID(WESTON_1)))              # died first


# -- the cold-start owner ------------------------------------------------------------------------


def test_an_exit_under_its_own_weston_stays_down():
    """The removed relaunch after an exit: a run that exits while its own Weston lives is `down`
    (fault process_exited) and nothing starts it, on later turns, after a Weston Display restarts
    on purpose, or after a Weston crash. Mutation probe: let restarted_on_purpose ignore the mark
    and `exited_under` (display != launch.display) -> a start after the Weston change."""
    broker, driver = cold()
    driver.running = None                                   # kill -9, SIGSEGV, OOM: its own exit
    record = turns(broker)
    assert (record.phase, record.fault, record.exited_under) == ("down", "process_exited",
                                                                 str(UUID(WESTON_1)))
    driver.display, driver.purposeful = WESTON_2, PurposefulRestart(WESTON_1)  # even on purpose
    assert turns(broker).phase == "down"
    driver.display, driver.purposeful = WESTON_3, None      # or a crash
    assert turns(broker).phase == "down"
    assert starts(driver) == 1


def test_a_weston_display_restarted_on_purpose_starts_the_run_once():
    """Display's signal range restart: Weston's restart job is pending when the run is first
    seen gone (no Weston up), then the new Weston comes up with Display's mark naming the run's;
    once PID1 unloaded the old unit the run starts once, as a new epoch for the new Weston."""
    broker, driver = cold()
    driver.running, driver.display = None, None             # the restart job is running
    record = turns(broker, 3)
    assert (record.phase, record.exited_under) == ("exited", None)
    driver.display, driver.purposeful, driver.collected = WESTON_2, PurposefulRestart(WESTON_1), False
    assert turns(broker, 3).phase == "exited" and starts(driver) == 1   # the old unit is still loaded
    driver.collected = True
    record = broker.reconcile()
    assert record.phase == "running" and record.exited_under is None and starts(driver) == 2
    assert driver.launched() == Launch(2, WESTON_2, 0)
    assert turns(broker).phase == "running" and starts(driver) == 2
    driver.running = None                                   # and then its own exit: down
    assert turns(broker).phase == "down" and starts(driver) == 2


def test_a_weston_crash_never_starts_the_run_again():
    """The removed relaunch after a Weston crash: no mark (or a stale one naming another Weston),
    so the run stays gone however many Westons follow."""
    broker, driver = cold()
    driver.running, driver.display = None, None             # Weston crashed and is restarting
    broker.reconcile()
    driver.display = WESTON_2
    assert turns(broker).phase == "exited"
    driver.purposeful = PurposefulRestart(WESTON_3)         # a later purposeful restart of another
    driver.display = WESTON_3
    assert turns(broker).phase == "exited"
    assert starts(driver) == 1


def test_a_failed_start_for_display_is_effect_unknown_and_never_retried():
    """The removed paced retry (E-1B-18): a start for Display's restart that raises is
    `effect_unknown`, and no later turn starts anything."""
    broker, driver = cold()
    driver.running, driver.display = None, None
    broker.reconcile()
    driver.display, driver.purposeful = WESTON_2, PurposefulRestart(WESTON_1)
    attempts = []

    def failing(environment, operation_id):
        attempts.append(operation_id)
        raise OSError("spawn failed")
    driver.start = failing
    record = broker.reconcile()
    assert (record.phase, record.fault) == ("effect_unknown", "start_outcome_unknown")
    del driver.start
    assert turns(broker).phase == "effect_unknown"
    assert attempts == [UUID(int=3)] and starts(driver) == 1


def test_the_journal_keeps_a_down_run_and_where_it_exited(tmp_path):
    journal = FileEffectJournal(boot_store(tmp_path, uuid4()))
    command = ColdStart(UUID(int=3), UUID(int=1), UUID(int=2), env())
    journal.put(EffectRecord(command, "down", None, "process_exited", str(UUID(WESTON_1))))
    reread = FileEffectJournal(journal.store)
    assert reread.current() == EffectRecord(command, "down", None, "process_exited", str(UUID(WESTON_1)))


# -- Display's mark ------------------------------------------------------------------------------


def driver_reading(path):
    return linux.SystemdAppProcessDriver(path.parent / "roots", None, base_abi="b", graphics_abi="g",
                                         plugin_abi="p", purposeful=path)


@pytest.mark.parametrize("text", [WESTON_1, WESTON_1 + "\n", "  " + WESTON_1 + "\n\n"])
def test_the_driver_reads_displays_mark(tmp_path, text):
    mark = tmp_path / "restarted"
    mark.write_text(text)
    assert driver_reading(mark).purposeful_restart() == PurposefulRestart(WESTON_1)


@pytest.mark.parametrize("case", ["absent", "empty", "uuid", "long", "binary", "symlink", "directory"])
def test_anything_but_a_small_regular_file_with_an_invocation_id_is_no_mark(tmp_path, case):
    mark = tmp_path / "restarted"
    if case == "empty":
        mark.write_text("\n")                       # Weston was down: systemctl showed nothing
    elif case == "uuid":
        mark.write_text(str(UUID(WESTON_1)))
    elif case == "long":
        mark.write_text(WESTON_1 + " " * 40 + "\n")  # 73 bytes: more than 64
    elif case == "binary":
        mark.write_bytes(b"\xff" * 32)
    elif case == "symlink":
        (tmp_path / "elsewhere").write_text(WESTON_1)
        os.symlink(tmp_path / "elsewhere", mark)
    elif case == "directory":
        mark.mkdir()
    assert driver_reading(mark).purposeful_restart() is None


def test_the_signal_range_restart_marks_the_weston_it_restarts_before_restarting_it():
    """photo-wall-display-signal.service writes Weston's InvocationID to PURPOSEFUL_RESTART
    (whole or not at all: a rename) before its restart, in its own preserved, world-readable
    RuntimeDirectory, and a failed mark never holds the restart back. Mutation probe: move the
    ExecStartPre= after the restart, or write the mark in place -> fails."""
    lines = (REPO / "appliance/systemd" / SIGNAL_RANGE_UNIT).read_text().splitlines()
    assert f"RuntimeDirectory={PURPOSEFUL_RESTART.parent.name}" in lines
    assert "RuntimeDirectoryPreserve=yes" in lines and "RuntimeDirectoryMode=0755" in lines
    (pre,) = [line for line in lines if line.startswith("ExecStartPre=")]
    partial = f"{PURPOSEFUL_RESTART}.partial"
    assert pre.startswith("ExecStartPre=-/bin/sh -ec '")
    assert (f"/usr/bin/systemctl show -p InvocationID --value {DISPLAY_UNIT} > {partial} && "
            f"/usr/bin/mv -f {partial} {PURPOSEFUL_RESTART}'") in pre
    restart = f"ExecStart=/usr/bin/systemctl --no-block restart {DISPLAY_UNIT}"
    assert lines.index(pre) < lines.index(restart)
