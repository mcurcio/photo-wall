"""Single-writer cold-start effect boundary; online withdrawal is deliberately absent.

Ports are base-owned. Their observations must describe actual local process/root
state; a successful spawn response alone is not a RunningApp. The journal is
restart-safe within this boot and externally enforces one writer. No Linux adapter
or production command route is supplied by this portable module.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from threading import RLock
from typing import Protocol
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
    def start(self, environment: AppEnvironmentRefV2, operation_id: UUID) -> RunningApp: ...


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
        """Observe ambiguous effects without starting, stopping or selecting again."""
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
                record = replace(record, phase="exited", fault="process_exited")
            else:
                # Absence after intent cannot prove that no short-lived process ran.
                record = replace(record, phase="effect_unknown", fault="reconciliation_required")
            self.journal.put(record)
            return record
