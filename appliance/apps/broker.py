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
from typing import Final, Literal, Protocol
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


class AppUnitSettling(ValueError):
    """The app unit is between states (activating, deactivating, reloading): nothing is known about
    its process this turn; observe again next turn. Never a reason to relaunch, kill or exit.
    `AppProcessDriver.current` raises it (process_linux.SystemdAppProcessDriver); every caller on
    the broker's turn treats it as an unknown turn (a Weston restart takes the Player down this
    way, display tuning, decision 0024)."""

    def __init__(self, state: str) -> None:
        super().__init__("app_unit_settling")
        self.state = state


class ForeignAppUnit(ValueError):
    """A unit under the app's name that is not the current launch's (another root, or outside the
    app slice), or one with no launch at all: never adopted, never waited out. The broker's loop
    stops it (`AppProcessDriver.stop_foreign`) and relaunches its own app on its pacing."""

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


# -- 1b P1b: the paced relaunch (frozen; bodies in P1b) ----------------------------------------
# The Player's death becomes a timely restart with one owner, the broker, paced like Kubernetes'
# CrashLoopBackOff: consecutive short runs wait 1 s, 5 s, 30 s, then 5 minutes each; a run that
# lasted STABLE_RUN_MS starts the count again. systemd's Restart= is not used, and no dependency
# lets a job on another unit start the app (`process_linux.app_unit_properties`): either would
# start runs the broker never launched (a second supervisor; incident 2026-10-13).
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
        if not (type(self.epoch) is int and self.epoch >= 1
                and (self.display is None or (type(self.display) is str and self.display))
                and type(self.started_ms) is int and self.started_ms >= 0):
            raise ValueError("launch_invalid")


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
        if not all(type(value) is int for value in (self.epoch, self.exits, self.not_before_ms)) \
                or self.epoch < 1 or self.exits < 0 or self.not_before_ms < 0:
            raise ValueError("relaunch_pacing_invalid")


class RelaunchPacingStore(Protocol):
    def get(self) -> RelaunchPacing | None: ...

    def put(self, pacing: RelaunchPacing) -> None:
        """Persist before returning."""
        ...


# -- Issue #125: no automatic Player restart (frozen; bodies in the tracer slice) ---------------
# The owner, 2026-10-20: "a human. dont do automated reboots yet. i want to see every time it fails
# for now"; asked whether that covers the Player program alone: "No auto-restarts at all". Three
# causes start a run, and nothing else does: an exit, a missed ping, open files, a Weston crash, a
# failed release or a foreign unit never starts one (a foreign unit is stopped, nothing in its place).
StartCause = Literal[
    "cold_start",                    # once per boot (AppEffectBroker.cold_start)
    "display_restarted_on_purpose",  # Display restarted Weston itself (a signal range change) under a live run
    "release_switch",                # a person applied a release: the new release starts once (online_broker)
]


@dataclass(frozen=True)
class PurposefulRestart:
    """Display's mark that it restarted Weston itself (appliance/kernel/display_paths.py
    PURPOSEFUL_RESTART, written by photo-wall-display-signal.service before its restart): the
    InvocationID of the Weston incarnation it stopped."""

    restarted: str

    def __post_init__(self) -> None:
        """ValueError("purposeful_restart_invalid") unless `restarted` is 32 lowercase hex digits
        (a systemd InvocationID)."""
        ...


def restarted_on_purpose(launch: Launch | None, display: str | None,
                         purposeful: PurposefulRestart | None, *, exited_under: str | None) -> bool:
    """PURE. The one rule for starting a run again within a boot: True iff the absent run is the
    victim of Display's own Weston restart, not of its own failure. All of: `launch` is not None;
    `display` (the Weston up now, `AppProcessDriver.display_incarnation`) is not None and differs
    from `launch.display`; `purposeful` is not None and names `launch.display`; and the run's exit
    was not first seen under its own Weston (`exited_under`, the incarnation up when the broker
    first saw the run absent, None while none was up, is not `launch.display`). A run that died
    while its Weston lived stays down whatever Display does later; a person recovers it."""
    ...


def display_took_app(launch: Launch | None, display: str | None) -> bool:
    """PURE. True iff a display is up (`display` not None) and it is not the incarnation
    `launch` was spawned under (the app is bound to its Weston and stopped with it). False when
    `launch` is None."""
    return launch is not None and display is not None and display != launch.display


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
    if prior is not None and prior.epoch == launch.epoch:
        return prior
    if display_changed:
        return RelaunchPacing(launch.epoch, prior.exits if prior else 0, now_ms)
    if now_ms - launch.started_ms >= STABLE_RUN_MS:
        exits = 1
    else:
        exits = (prior.exits if prior else 0) + 1
    return _paced(launch.epoch, exits, now_ms)


def pace_failed_start(paced: RelaunchPacing, launch: Launch, *, now_ms: int) -> RelaunchPacing:
    """PURE. A relaunch whose start raised at `now_ms` counts as one more short exit, judged for
    `launch` (the driver's launch after the failure: a new epoch when the start recorded one, the
    same as `paced.epoch` when it raised before that), so the next try waits its backoff and that
    launch's exit is not counted again."""
    return _paced(launch.epoch, paced.exits + 1, now_ms)


def _paced(epoch: int, exits: int, now_ms: int) -> RelaunchPacing:
    """`exits` (>= 1) consecutive short exits, the last judged at `now_ms`."""
    wait = RELAUNCH_BACKOFF_MS[min(exits, len(RELAUNCH_BACKOFF_MS)) - 1]
    return RelaunchPacing(epoch, exits, now_ms + wait)


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
    (ValueError("started_environment_mismatch")). A failed start is paced as one more short exit
    (`pace_failed_start`, stored before the error is raised again), whether or not it recorded a
    launch first; the owner records the failure and asks again on a later turn.
    """
    display = driver.display_incarnation()
    if display is None:
        return None
    launch = driver.launched()
    if launch is None:
        raise ValueError("relaunch_without_launch")
    now = now_ms()
    prior = pacing.get()
    paced = pace_relaunch(prior, launch, display_changed=display_took_app(launch, display),
                          now_ms=now)
    if paced != prior:
        pacing.put(paced)
    if not driver.unit_collected() or now < paced.not_before_ms:
        return None
    intent()
    try:
        running = driver.start(environment, operation_id)
    except Exception:
        pacing.put(pace_failed_start(paced, driver.launched() or launch, now_ms=now))
        raise
    if running.environment != environment or running.operation_id != operation_id:
        raise ValueError("started_environment_mismatch")
    return running


class AppEffectBroker:
    def __init__(self, *, boot_id: UUID, offer_id: UUID,
                 authorized_environment: AppEnvironmentRefV2,
                 journal: EffectJournal, driver: AppProcessDriver,
                 pacing: RelaunchPacingStore, now_ms: Callable[[], int]):
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
        self.pacing = pacing
        self.now_ms = now_ms
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
        paced relaunch (`relaunch_absent`) of a launch found absent: a settled one (`running`,
        `exited`) or one whose relaunch failed (`relaunch_outcome_unknown`, E-1B-18). A unit
        between states (AppUnitSettling) is an unknown turn: the record stays as it is, nothing is
        relaunched, and the next turn observes again. Any other observation fault raises to the
        loop (`broker_runner.BrokerLoop`), which reports it and observes again on a backoff."""
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
                record = replace(record, phase="running", running=running, fault=None)
            elif running is None and (record.phase in ("running", "exited") or (
                    record.phase == "effect_unknown" and record.fault == "relaunch_outcome_unknown")):
                command = record.command
                intent = replace(record, phase="intent_start", running=None, fault=None)
                try:
                    running = relaunch_absent(self.driver, self.pacing, command.environment,
                                              command.operation_id, now_ms=self.now_ms,
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
