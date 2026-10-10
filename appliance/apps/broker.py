"""Single-writer cold-start effect boundary; online withdrawal is deliberately absent.

Ports are base-owned. Their observations must describe actual local process/root
state; a successful spawn response alone is not a RunningApp. The journal is
restart-safe within this boot and externally enforces one writer. No Linux adapter
or production command route is supplied by this portable module.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from threading import RLock
from typing import Final, Protocol
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


class EffectJournal(Protocol):
    def get(self, operation_id: UUID) -> EffectRecord | None: ...
    def current(self) -> EffectRecord | None: ...
    def put(self, record: EffectRecord) -> None:
        """Persist before returning; preserve old operation identity records."""
        ...


class AppProcessDriver(Protocol):
    def current(self) -> RunningApp | None:
        """None means observed absent. Unavailable/ambiguous raises."""
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

    def launched_display(self) -> str | None:
        """The display incarnation the current launch recorded (None: none was running)."""
        ...

    def unit_collected(self) -> bool:
        """The app unit's name is free: no process, no job, unloaded (a start can begin now)."""
        ...


def relaunch_for_display(driver: AppProcessDriver, environment: AppEnvironmentRefV2,
                         operation_id: UUID, *, intent: Callable[[], None] = lambda: None,
                         ) -> RunningApp | None:
    """The one relaunch rule, for whichever launch is current (the boot's or a switch's).

    The app runs bound to one display incarnation (`BindsTo=` Weston) and stops with it. Its
    owner, holding a settled record that says `environment` should be running, calls this when
    the app is absent: if a newer display incarnation is active, the same environment and
    operation start again. Every launch records the incarnation it was spawned under, so this
    is once per incarnation, never a retry loop. Until the old unit is collected nothing starts
    and None is returned: the owner's next turn asks again. `intent` runs just before the
    spawn (the owner's durable intent). A failed start raises; the owner records it.
    """
    display = driver.display_incarnation()
    if display is None or display == driver.launched_display() or not driver.unit_collected():
        return None
    intent()
    running = driver.start(environment, operation_id)
    if running.environment != environment or running.operation_id != operation_id:
        raise ValueError("started_environment_mismatch")
    return running


# -- 1b P1b: the paced relaunch (frozen; bodies in P1b) ----------------------------------------
# The Player's death becomes a timely restart with one owner, the broker, paced like Kubernetes'
# CrashLoopBackOff: consecutive short runs wait 1 s, 5 s, 30 s, then 5 minutes each; a run that
# lasted STABLE_RUN_MS starts the count again. systemd's Restart= is not used: it would start
# runs the broker never journaled (a second supervisor, and a launch record whose process no
# longer matches: `app_process_incarnation_mismatch`).
RELAUNCH_BACKOFF_MS: Final[tuple[int, ...]] = (1_000, 5_000, 30_000, 300_000)
STABLE_RUN_MS: Final = 600_000


@dataclass(frozen=True)
class Launch:
    """The driver's record of the current launch (its boot-store `launch`): the app epoch it
    started, the display incarnation current at the spawn, and the spawn's boottime in ms
    (`appliance.kernel.clock.boottime_ms`, the clock every `now_ms` here is read on)."""

    epoch: int
    display: str | None
    started_ms: int

    def __post_init__(self) -> None:
        """ValueError("launch_invalid") unless epoch >= 1, display is None or a non-empty str,
        and started_ms is an int >= 0."""
        raise NotImplementedError


@dataclass(frozen=True)
class RelaunchPacing:
    """When the next relaunch may start. Durable for this boot (the boot store), one for the app
    unit, shared by the cold-start and the online owner."""

    epoch: int  # the launch whose exit this pacing judged (an exit is counted once)
    exits: int  # consecutive short exits counted, >= 0
    not_before_ms: int  # boottime ms before which no relaunch starts

    def __post_init__(self) -> None:
        """ValueError("relaunch_pacing_invalid") unless epoch >= 1, exits >= 0 and
        not_before_ms >= 0, all ints."""
        raise NotImplementedError


class RelaunchPacingStore(Protocol):
    def get(self) -> RelaunchPacing | None: ...

    def put(self, pacing: RelaunchPacing) -> None:
        """Persist before returning."""
        ...


def display_took_app(launch: Launch | None, display: str | None) -> bool:
    """PURE. True iff a display is up (`display` not None) and it is not the incarnation
    `launch` was spawned under (the app is bound to its Weston and stopped with it). False when
    `launch` is None."""
    raise NotImplementedError


def pace_relaunch(prior: RelaunchPacing | None, launch: Launch, *, display_changed: bool,
                  now_ms: int) -> RelaunchPacing:
    """PURE. The one pacing rule, judging the exit of `launch` first seen at `now_ms`.

    - `prior` judged this launch (`prior.epoch == launch.epoch`): `prior`, unchanged.
    - `display_changed`: the display took the app, not its own failure: exits stay
      `prior.exits` (0 without a prior), not_before_ms = now_ms.
    - the run lasted at least STABLE_RUN_MS (`now_ms - launch.started_ms`): exits = 1.
    - otherwise: exits = (prior.exits if prior else 0) + 1.
    Then not_before_ms = now_ms + RELAUNCH_BACKOFF_MS[min(exits, len) - 1] (exits >= 1).
    """
    raise NotImplementedError


def relaunch_absent(driver: AppProcessDriver, pacing: RelaunchPacingStore,
                    environment: AppEnvironmentRefV2, operation_id: UUID, *,
                    now_ms: Callable[[], int], intent: Callable[[], None] = lambda: None,
                    ) -> RunningApp | None:
    """The one relaunch rule (replaces `relaunch_for_display`), for whichever launch is current.

    Its owner holds a settled record saying `environment` should be running and calls this every
    turn the app is absent. In order: no display up (`driver.display_incarnation()` None) ->
    None; no launch recorded (`driver.launched()` None) -> ValueError("relaunch_without_launch");
    the exit is paced (`pace_relaunch` with display_changed = `display_took_app(...)`, stored
    through `pacing` when it differs from `pacing.get()`); the old unit not yet collected, or
    `now_ms()` before `not_before_ms` -> None (the owner's next turn asks again); else `intent`
    runs, then `driver.start(environment, operation_id)`, whose identity must match
    (ValueError("started_environment_mismatch")). A failed start raises; the owner records it,
    and its next turn paces that launch's exit like any other.
    """
    raise NotImplementedError


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
        """Observe ambiguous effects without starting, stopping or selecting again, except the
        one display relaunch (`relaunch_for_display`) of a settled launch found absent."""
        with self._lock:
            record = self.journal.current()
            if record is None:
                return None
            running = self.driver.current()
            if (running is not None and running.environment == record.command.environment
                    and running.operation_id == record.command.operation_id
                    and (record.running is None or running.process == record.running.process)):
                record = replace(record, phase="running", running=running, fault=None)
            elif running is None and record.phase in ("running", "exited"):
                command = record.command
                intent = replace(record, phase="intent_start", running=None, fault=None)
                try:
                    running = relaunch_for_display(self.driver, command.environment,
                                                   command.operation_id,
                                                   intent=lambda: self.journal.put(intent))
                except Exception:
                    record = replace(intent, phase="effect_unknown", fault="relaunch_outcome_unknown")
                else:
                    record = (replace(record, phase="exited", fault="process_exited") if running is None
                              else replace(intent, phase="running", running=running))
            else:
                # Absence after intent cannot prove that no short-lived process ran.
                record = replace(record, phase="effect_unknown", fault="reconciliation_required")
            self.journal.put(record)
            return record
