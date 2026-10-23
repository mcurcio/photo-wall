"""Single-writer cold-start effect boundary; online withdrawal is deliberately absent.

Ports are base-owned. Their observations must describe actual local process/root
state; a successful spawn response alone is not a RunningApp. The journal is
restart-safe within this boot and externally enforces one writer. No Linux adapter
or production command route is supplied by this portable module.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from threading import RLock
from typing import Literal, Protocol
from uuid import UUID

from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_protocol import NodeProcessIdentity, counter, identifier


@dataclass(frozen=True)
class RunningApp:
    environment: AppEnvironmentRefV2
    process: NodeProcessIdentity
    app_epoch: int
    operation_id: UUID

    def __post_init__(self) -> None:
        identifier(self.operation_id)
        counter(self.app_epoch, 1)
        if (type(self.environment) is not AppEnvironmentRefV2
                or type(self.process) is not NodeProcessIdentity):
            raise ValueError("running_app_identity_invalid")


@dataclass(frozen=True)
class ColdStart:
    operation_id: UUID
    kernel_boot_id: UUID
    offer_id: UUID
    environment: AppEnvironmentRefV2

    def __post_init__(self) -> None:
        identifier(self.operation_id)
        identifier(self.kernel_boot_id)
        identifier(self.offer_id)
        if type(self.environment) is not AppEnvironmentRefV2:
            raise ValueError("cold_environment_invalid")


@dataclass(frozen=True)
class EffectRecord:
    command: ColdStart
    phase: str
    running: RunningApp | None = None
    fault: str | None = None
    # The display incarnation up on the turn the broker first saw the run absent (None while
    # none was up); kept until a run starts again (`restarted_on_purpose`).
    exited_under: str | None = None


class AppUnitSettling(ValueError):
    """The app unit is between states (activating, deactivating, reloading): nothing is known about
    its process this turn; observe again next turn. Never a reason to start, stop or exit.
    `AppProcessDriver.current` raises it (process_linux.SystemdAppProcessDriver); every caller on
    the broker's turn treats it as an unknown turn (a Weston restart takes the Player down this
    way, display tuning, decision 0024)."""

    def __init__(self, state: str) -> None:
        super().__init__("app_unit_settling")
        self.state = state


class ForeignAppUnit(ValueError):
    """A unit under the app's name that is not the current launch's (another root, or outside the
    app slice), or one with no launch at all: never adopted, never waited out. The broker's loop
    stops it (`AppProcessDriver.stop_foreign`) and starts nothing in its place."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)


class EffectJournal(Protocol):
    def get(self, operation_id: UUID) -> EffectRecord | None: ...
    def current(self) -> EffectRecord | None: ...
    def put(self, record: EffectRecord) -> None:
        """Persist before returning; preserve old operation identity records."""
        ...


class AppProcessDriver(Protocol):
    def current(self) -> RunningApp | None:
        """None means observed absent. Unavailable/ambiguous raises; a unit between states raises
        AppUnitSettling (an unknown turn); a unit that is not the launch's raises ForeignAppUnit.
        A run of the launch's root, cgroup and environment that the broker did not spawn is
        adopted as the current launch (a new epoch), never an error."""
        ...

    def stop_foreign(self) -> None:
        """Ask for whatever runs under the app's unit name to stop, without waiting."""
        ...

    def verify(self, environment: AppEnvironmentRefV2) -> bool:
        """Verify exact staged bytes and local base/graphics/plugin ABI."""
        ...

    def select(self, environment: AppEnvironmentRefV2) -> None: ...
    def start(self, environment: AppEnvironmentRefV2, operation_id: UUID) -> RunningApp:
        """Spawn; the launch records the display incarnation current at the spawn."""
        ...

    def display_incarnation(self) -> str | None:
        """The running display's incarnation (Weston's InvocationID); None while it is down."""
        ...

    def launched(self) -> Launch | None:
        """The current launch (its boot-store `launch` record); None before the first start."""
        ...

    def unit_collected(self) -> bool:
        """The app unit's name is free: no process, no job, unloaded (a start can begin now)."""
        ...

    def purposeful_restart(self) -> PurposefulRestart | None:
        """Display's mark, or None when absent or malformed; never raises."""
        ...


@dataclass(frozen=True)
class Launch:
    """The driver's record of the current launch (its boot-store `launch`): the app epoch it
    started, the display incarnation current at the spawn, and the spawn's boottime in ms
    (`appliance.kernel.clock.boottime_ms`)."""

    epoch: int
    display: str | None
    started_ms: int

    def __post_init__(self) -> None:
        """ValueError("launch_invalid") unless epoch >= 1, display is None or a non-empty str,
        and started_ms is an int >= 0."""
        if not (type(self.epoch) is int and self.epoch >= 1
                and (self.display is None or (type(self.display) is str and self.display))
                and type(self.started_ms) is int and self.started_ms >= 0):
            raise ValueError("launch_invalid")


# -- Issue #125: no automatic Player restart ---------------------------------------------------
# The owner, 2026-10-20: "a human. dont do automated reboots yet. i want to see every time it fails
# for now"; asked whether that covers the Player program alone: "No auto-restarts at all". Three
# causes start a run, and nothing else does: an exit, a missed ping, open files, a Weston crash, a
# failed release or a foreign unit never starts one (a foreign unit is stopped, nothing in its place).
# systemd's Restart= is not used, and no dependency lets a job on another unit start the app
# (`process_linux.app_unit_properties`): either would start runs the broker never launched.
StartCause = Literal[
    "cold_start",                    # once per boot (AppEffectBroker.cold_start)
    "display_restarted_on_purpose",  # Display restarted Weston itself (a signal range change) under a live run
    "release_switch",                # a person applied a release: the new release starts once (online_broker)
]
_INVOCATION_ID = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class PurposefulRestart:
    """Display's mark that it restarted Weston itself (appliance/kernel/display_paths.py
    PURPOSEFUL_RESTART, written by photo-wall-display-signal.service before its restart): the
    InvocationID of the Weston incarnation it stopped."""

    restarted: str

    def __post_init__(self) -> None:
        """ValueError("purposeful_restart_invalid") unless `restarted` is 32 lowercase hex digits
        (a systemd InvocationID)."""
        if type(self.restarted) is not str or not _INVOCATION_ID.fullmatch(self.restarted):
            raise ValueError("purposeful_restart_invalid")


def _invocation(value: str | None) -> str | None:
    """An InvocationID in systemctl's form (32 lowercase hex digits), whichever form it was
    recorded in (the driver keeps a UUID's text); None stays None, and text that is no
    InvocationID is kept as it is (it then names no Weston the mark can name)."""
    if value is None:
        return None
    try:
        return UUID(value).hex
    except ValueError:
        return value


def restarted_on_purpose(launch: Launch | None, display: str | None,
                         purposeful: PurposefulRestart | None, *, exited_under: str | None) -> bool:
    """PURE. The one rule for starting a run again within a boot: True iff the absent run is the
    victim of Display's own Weston restart, not of its own failure. All of: `launch` is not None;
    `display` (the Weston up now, `AppProcessDriver.display_incarnation`) is not None and differs
    from `launch.display`; `purposeful` is not None and names `launch.display`; and the run's exit
    was not first seen under its own Weston (`exited_under`, the incarnation up when the broker
    first saw the run absent, None while none was up, is not `launch.display`). A run that died
    while its Weston lived stays down whatever Display does later; a person recovers it.

    InvocationIDs compare as systemd's 32 hex digits, whether recorded as a UUID's text or not."""
    if launch is None or display is None or purposeful is None:
        return False
    launched_under = _invocation(launch.display)
    return (_invocation(display) != launched_under
            and purposeful.restarted == launched_under
            and _invocation(exited_under) != launched_under)


def start_for_display(driver: AppProcessDriver, environment: AppEnvironmentRefV2,
                      operation_id: UUID, *, exited_under: str | None,
                      intent: Callable[[], None] = lambda: None) -> RunningApp | None:
    """The one start rule after cold start, for whichever launch is current: its owner holds a
    record saying `environment` should be running and calls this every turn the app is absent.

    None unless `restarted_on_purpose(driver.launched(), driver.display_incarnation(),
    driver.purposeful_restart(), exited_under=exited_under)` and `driver.unit_collected()` (the
    old unit not yet unloaded: the owner's next turn asks again). Otherwise `intent` runs, then
    `driver.start(environment, operation_id)`, whose identity must match
    (ValueError("started_environment_mismatch")). A failed start raises; the owner records it and
    never asks again (no retry: a person recovers)."""
    if not restarted_on_purpose(driver.launched(), driver.display_incarnation(),
                                driver.purposeful_restart(), exited_under=exited_under):
        return None
    if not driver.unit_collected():
        return None
    intent()
    running = driver.start(environment, operation_id)
    if running.environment != environment or running.operation_id != operation_id:
        raise ValueError("started_environment_mismatch")
    return running


class AppEffectBroker:
    def __init__(self, *, boot_id: UUID, offer_id: UUID,
                 authorized_environment: AppEnvironmentRefV2,
                 journal: EffectJournal, driver: AppProcessDriver):
        identifier(boot_id)
        identifier(offer_id)
        if type(authorized_environment) is not AppEnvironmentRefV2:
            raise ValueError("cold_environment_invalid")
        # The composition root obtains these frozen facts from protected boot state.
        # Receiving a ColdStart from an AppManager is not an authorization mechanism.
        self.boot_id = boot_id
        self.offer_id = offer_id
        self.authorized_environment = authorized_environment
        self.journal = journal
        self.driver = driver
        self._lock = RLock()

    def cold_start(self, command: ColdStart) -> EffectRecord:
        with self._lock:
            if (command.kernel_boot_id != self.boot_id or command.offer_id != self.offer_id
                    or command.environment != self.authorized_environment):
                raise ValueError("cold_authority_mismatch")
            prior = self.journal.get(command.operation_id)
            if prior is not None:
                if prior.command != command:
                    raise ValueError("operation_identity_conflict")
                return prior
            current = self.journal.current()
            if current is not None and current.phase in ("intent_start", "effect_unknown"):
                raise ValueError("prior_effect_unreconciled")
            if self.driver.current() is not None:
                raise ValueError("cold_start_cannot_stop_healthy_app")
            if self.driver.verify(command.environment) is not True:
                raise ValueError("environment_unverified")
            record = EffectRecord(command, "intent_start")
            self.journal.put(record)
            try:
                self.driver.select(command.environment)
                running = self.driver.start(command.environment, command.operation_id)
                if (running.environment != command.environment
                        or running.operation_id != command.operation_id):
                    raise ValueError("started_environment_mismatch")
            except Exception:
                record = replace(record, phase="effect_unknown", fault="start_outcome_unknown")
            else:
                record = replace(record, phase="running", running=running)
            self.journal.put(record)
            return record

    def reconcile(self) -> EffectRecord | None:
        """Observe ambiguous effects without starting, stopping or selecting again. A settled run
        found absent (`running`, or `exited` while it waits on its Weston) records `exited_under`
        once, then asks `start_for_display`, the one start rule after cold start: only a run
        Display's own Weston restart took starts again. A run that exited while its own Weston
        was up is `down` (fault `process_exited`) for the rest of the boot; only a person
        (Hardware > Reboot) recovers it. Nothing is paced or retried: a start that raised is
        `effect_unknown`. A unit between states (AppUnitSettling) is an unknown turn: the record
        stays as it is and the next turn observes again. Any other observation fault raises to
        the loop (`broker_runner.BrokerLoop`), which reports it and observes again on a backoff."""
        with self._lock:
            record = self.journal.current()
            if record is None:
                return None
            try:
                running = self.driver.current()
            except AppUnitSettling:
                return record
            # The driver's run is the launch's, adopted when the broker did not spawn it
            # (`AppProcessDriver.current`), so a new process of the same command is this record's.
            if (running is not None and running.environment == record.command.environment
                    and running.operation_id == record.command.operation_id):
                record = replace(record, phase="running", running=running, fault=None,
                                 exited_under=None)
            elif running is None and record.phase == "down":
                return record
            elif running is None and record.phase in ("running", "exited"):
                exited_under = (record.exited_under if record.phase == "exited"
                                else self.driver.display_incarnation())
                command = record.command
                intent = replace(record, phase="intent_start", running=None, fault=None,
                                 exited_under=None)
                try:
                    running = start_for_display(self.driver, command.environment,
                                                command.operation_id, exited_under=exited_under,
                                                intent=lambda: self.journal.put(intent))
                except Exception:
                    record = replace(intent, phase="effect_unknown", fault="start_outcome_unknown")
                else:
                    if running is not None:
                        record = replace(intent, phase="running", running=running)
                    else:
                        launch = self.driver.launched()
                        own = launch is not None and exited_under is not None and (
                            _invocation(exited_under) == _invocation(launch.display))
                        record = replace(record, phase="down" if own else "exited",
                                         running=None, fault="process_exited",
                                         exited_under=exited_under)
            else:
                # Absence after intent cannot prove that no short-lived process ran.
                record = replace(record, phase="effect_unknown", fault="reconciliation_required")
            self.journal.put(record)
            return record
