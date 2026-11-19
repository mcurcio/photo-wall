"""A Player that exits before the broker's first turn still gets its card (issues #123, #125).

Seen in CI's PID1 `failure` leg (2026-11-19): the cold start spawned the Player, which exited at
once; the broker's first turn found nothing current, so its run facts (`app_started`, `app_exited`)
were never written, the health judge never raised `app_absent`, and the Frame stayed dark with no
card. The rule (`appliance.kernel.app_facts`): a run has started once the launch names it, and a
started run a known turn finds not current has exited; each is stated once, and every feed page
carries the run state, so a reader's feed gap loses neither.

The real BrokerLoop turn runs the real AppEffectBroker (its real cold start) over the real
SystemdAppProcessDriver (its real `start`); only its edges are scripted: `systemctl show` rows,
/proc and the spawn. The facts it writes go through the real HealthJudge to the card.
"""
from __future__ import annotations

import itertools
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from node.apps.test_app_unit_settling import Unit
from node.test_node_linux_adapters import store as boot_store
from node.test_node_probe_broker import loop_for
from test_node_boot import environment

from appliance.apps import process_linux as linux
from appliance.apps.broker import AppEffectBroker, ColdStart, RunningApp
from appliance.apps.broker_runner import answer_broker_feed
from appliance.apps.lifecycle_storage import FileEffectJournal
from appliance.apps.probe import SHIPPED_TIMING, UNSEEN_INVOCATION, AppRunKey
from appliance.display_host.overlay.instruction import PULSE_DEADLINE_MS
from appliance.health.judge import APP_ABSENT, HealthJudge
from appliance.health.runner import BrokerFeedReader
from appliance.kernel.app_facts import APP_EXITED, APP_STARTED
from contracts.node_faults import FAULTS
from contracts.node_protocol import NodeProcessIdentity

RAISE = FAULTS[APP_ABSENT].raise_window_ms
OUTPUT = "Virtual-1"


class Wall:
    """The broker's turn and the judge reading its feed, on one clock (`loop.now`)."""

    def __init__(self, loop, feed) -> None:
        self.loop, self.feed, self.taken = loop, feed, []
        self.reader = BrokerFeedReader("broker", Path("/nonexistent/feed.sock"))
        self.reader._read = self._read
        self.judge = HealthJudge(period_ms=SHIPPED_TIMING.period_ms, miss_limit=SHIPPED_TIMING.miss_limit,
                                 startup_ms=SHIPPED_TIMING.startup_ms,
                                 pulse_deadline_ms=PULSE_DEADLINE_MS, catalogue=FAULTS)
        # Display's snapshot: the Output on, nothing admitted (no Player ever held it).
        self.judge.observe_outputs([{"output_id": OUTPUT, "connected": True, "admitted": None,
                                     "holding": False, "power": "on"}], 0)

    def turn(self, *, gap: bool = False) -> list[tuple[str, dict]]:
        """One broker turn, then the judge reads the feed until caught up, through the health
        runner's reader and the broker's own answer (`gap`: before the read, as many newer facts
        as the ring holds evict what this turn wrote). The run facts read."""
        self.loop.turn()
        while gap and self.facts():
            self.feed.append("unrelated", {})
        self.taken = []
        while self.reader.cursor.after < self.feed.read(0, incarnation=None, limit=1).latest:
            self.reader.drain(self.judge, lambda: self.loop.now)
        return self.taken

    def _read(self, request: dict) -> dict:
        page = answer_broker_feed(self.feed, self.loop.facts, request)
        self.taken.extend((each["kind"], each["value"]["run"]) for each in page["events"]
                          if each["kind"] in (APP_STARTED, APP_EXITED))
        return {"accepted": True, **page}

    def facts(self) -> list[tuple[str, dict]]:
        """Every run fact the broker wrote."""
        return [(event.kind, event.value["run"]) for event in self.feed.read(0, incarnation=None, limit=1024).events
                if event.kind in (APP_STARTED, APP_EXITED)]

    def card(self, now: int) -> tuple[str, str]:
        (instruction,) = self.judge.instructions(now, wall_ms=now)
        assert instruction.tint, instruction
        return instruction.lines


@pytest.fixture
def cold_start(tmp_path, monkeypatch):
    """`cold_start(spawned)`: the broker's real cold start, whose spawn runs `spawned(unit)`;
    returns the Wall and the record. The unit is then gone (the Player exited and `--collect`
    unloaded it) before the broker's first turn."""
    loop, feed, _driver, _running, store = loop_for(tmp_path, monkeypatch)
    driver = linux.SystemdAppProcessDriver(tmp_path / "roots", store, base_abi="base-v2",
                                           graphics_abi="graphics-v2", plugin_abi="plugins-v2",
                                           proc=tmp_path / "proc", cgroups=tmp_path / "cgroups",
                                           sysfs=tmp_path / "sys", purposeful=tmp_path / "restarted")
    driver.verify = lambda environment: True  # the root image's mount is not this test's
    placeholder = RunningApp(environment("a"), NodeProcessIdentity(1, 1, uuid4()), 1, uuid4())
    unit = Unit(driver, placeholder)
    unit.state("inactive", pid="0", load="not-found")
    monkeypatch.setattr(linux, "systemctl_show", unit.show)
    monkeypatch.setattr(linux, "read_proc_start_ticks", lambda proc, pid: unit.ticks.get(pid))
    monkeypatch.setattr(linux, "process_root_matches", lambda *args: True)
    monkeypatch.setattr(linux, "boottime_ms", lambda: 50_000)  # CLOCK_BOOTTIME is Linux-only
    monkeypatch.setattr(linux, "time", SimpleNamespace(monotonic=itertools.count().__next__,
                                                       sleep=lambda seconds: None))
    command = ColdStart(uuid4(), uuid4(), uuid4(), environment("a"))
    journal = FileEffectJournal(boot_store(tmp_path / "journal", command.kernel_boot_id))
    loop.broker = AppEffectBroker(boot_id=command.kernel_boot_id, offer_id=command.offer_id,
                                  authorized_environment=command.environment, journal=journal,
                                  driver=driver)
    loop.driver = driver

    def start(spawned):
        def run(command_line, *args, **kwargs):
            if command_line[0] == "/usr/bin/systemd-run":
                spawned(unit, command)
            return subprocess.CompletedProcess(command_line, 0, "\n", "")  # `systemctl show --property=Job`
        monkeypatch.setattr(linux.subprocess, "run", run)
        record = loop.broker.cold_start(command)
        unit.state("inactive", pid="0", load="not-found")  # exited; --collect unloaded it
        return Wall(loop, feed), record

    yield start
    loop.probes.close()
    store.close()


def seen(unit, command):
    """The spawn the driver's first look sees running (CI's case: pid 324)."""
    unit.active(RunningApp(command.environment, NodeProcessIdentity(324, 11195, uuid4()), 1,
                           command.operation_id))


def unseen(unit, command):
    """A spawn whose process exits before the driver's first look."""


def test_a_player_that_exits_before_the_first_turn_gets_its_card(cold_start):
    """Mutation probe: judge the facts from the turn's current run alone (`_launch_run` returns
    `run`) -> no fact is written and the card is never shown."""
    wall, record = cold_start(seen)
    assert record.phase == "running"
    run = AppRunKey.of(record.running).document()
    assert wall.turn() == [(APP_STARTED, run), (APP_EXITED, run)]
    wall.loop.now += RAISE
    assert wall.turn() == []
    assert wall.card(wall.loop.now)[1] == f"{APP_ABSENT} · Output {OUTPUT}"
    assert wall.loop.broker.journal.current().phase == "down"  # nothing starts it again (#125)


def test_a_player_no_look_saw_still_gets_its_card(cold_start):
    """The spawn's process exited before the driver's first look: the launch's epoch alone names
    the run. Mutation probe: `AppRunKey.of_launch` answers only a seen run (None otherwise) -> no
    fact, no card."""
    wall, record = cold_start(unseen)
    assert record.phase == "effect_unknown"
    run = AppRunKey(UNSEEN_INVOCATION, 0, 0, 1).document()
    assert wall.turn() == [(APP_STARTED, run), (APP_EXITED, run)]
    wall.loop.now += RAISE
    wall.turn()
    assert wall.card(wall.loop.now)[1] == f"{APP_ABSENT} · Output {OUTPUT}"


def test_a_feed_gap_before_the_window_loses_no_exit(cold_start):
    """The judge's read missed both facts (the ring lost them, or the broker began again): the
    page's run state stands for them, so the exit opens `app_absent` and the card shows, though
    the broker states nothing again. Mutation probe: the reader ignores the page's run state ->
    no card."""
    wall, _record = cold_start(seen)
    assert wall.turn(gap=True) == []
    wall.loop.now += RAISE
    assert wall.turn() == []  # the broker stated nothing again
    assert wall.card(wall.loop.now)[1] == f"{APP_ABSENT} · Output {OUTPUT}"
