"""Independent HostCore policy; no app lifecycle, rendering or Player imports.

A protected transport supplies the current session. These domain operations do not
perform network authentication. Journals must persist writes before returning and
have exactly one HostCore writer for the boot. No production reboot adapter ships here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from threading import RLock
from typing import Protocol
from uuid import UUID, uuid4

from contracts.node_commands import RebootRequest
from contracts.node_protocol import (
    NodeCommandResponseV2,
    NodeEventV2,
    NodeProducerV2,
    RebootFact,
    counter,
    identifier,
)


@dataclass(frozen=True)
class RebootRecord:
    request: RebootRequest
    response: NodeCommandResponseV2
    invocation_started: bool = False
    event: NodeEventV2 | None = None
    effect_unknown: bool = False


class RebootJournal(Protocol):
    def get(self, command_id: UUID) -> RebootRecord | None: ...
    def put(self, record: RebootRecord) -> None: ...
    def next_sequence(self) -> int:
        """Allocate a persisted monotonic event sequence for this producer."""
        ...


class RebootDriver(Protocol):
    def initiate(self) -> bool:
        """True only if reboot initiation was observed; never means boot completed.

        False or exception is an unknown effect and must not be blindly retried.
        """
        ...


@dataclass(frozen=True)
class HostSample:
    producer: NodeProducerV2
    sampled_boottime_ms: int
    metrics: tuple[tuple[str, float, str], ...]


class HostSampler(Protocol):
    def sample(self) -> tuple[tuple[str, float, str], ...]: ...


class HostCore:
    def __init__(self, *, producer: NodeProducerV2, session_id: UUID, offer_id: UUID,
                 session_expires_boottime_ms: int, journal: RebootJournal,
                 driver: RebootDriver):
        identifier(session_id)
        identifier(offer_id)
        counter(session_expires_boottime_ms, 1)
        if type(producer) is not NodeProducerV2 or producer.owner != "host_core":
            raise ValueError("host_owner_required")
        self.producer = producer
        self.session_id = session_id
        self.offer_id = offer_id
        self.session_expires = session_expires_boottime_ms
        self.journal = journal
        self.driver = driver
        self._lock = RLock()

    def observe(self, sampler: HostSampler, *, now_ms: int) -> HostSample:
        """Independent read path: neither application nor reboot journal is required."""
        if type(now_ms) is not int or now_ms < 0:
            raise ValueError("host_clock_invalid")
        metrics = sampler.sample()
        if (not isinstance(metrics, tuple) or len(metrics) > 64
                or any(not isinstance(row, tuple) or len(row) != 3
                       or not isinstance(row[0], str) or not 1 <= len(row[0]) <= 64
                       or type(row[1]) not in (int, float) or not math.isfinite(row[1])
                       or not isinstance(row[2], str) or not 1 <= len(row[2]) <= 32
                       for row in metrics)):
            raise ValueError("host_metrics_bound")
        return HostSample(self.producer, now_ms, metrics)

    def _response(self, request: RebootRequest, decision: str,
                  reason: str) -> NodeCommandResponseV2:
        return NodeCommandResponseV2(
            producer=self.producer, command_id=request.command_id,
            command_sha256=request.command_sha256,
            command_session_id=request.command_session_id,
            scope="operator_reboot", decision=decision, reason=reason,
        )

    def _current(self, request: RebootRequest, now_ms: int) -> bool:
        return (request.producer == self.producer
                and request.command_session_id == self.session_id
                and request.offer_id == self.offer_id
                and type(now_ms) is int and now_ms >= 0
                and now_ms < min(request.expires_boottime_ms, self.session_expires))

    def receive(self, request: RebootRequest, *, now_ms: int) -> NodeCommandResponseV2:
        """Return admission alone; a caller may publish it before invoking the effect."""
        with self._lock:
            prior = self.journal.get(request.command_id)
            if prior is not None:
                if prior.request != request:
                    return self._response(request, "rejected", "command_identity_conflict")
                return prior.response
            if not self._current(request, now_ms):
                return self._response(request, "rejected", "reboot_scope_or_expiry")
            response = self._response(request, "accepted", "reboot_admitted")
            self.journal.put(RebootRecord(request, response))
            return response

    def initiate(self, command_id: UUID, *, now_ms: int) -> RebootRecord:
        with self._lock:
            record = self.journal.get(command_id)
            if record is None:
                raise ValueError("reboot_not_admitted")
            if record.invocation_started:
                return record
            if not self._current(record.request, now_ms):
                raise ValueError("reboot_scope_or_expiry")
            # Fence a crash between this write and invocation: recovery reports unknown.
            record = replace(record, invocation_started=True, effect_unknown=True)
            self.journal.put(record)
            try:
                initiated = self.driver.initiate()
            except Exception:
                return record
            if initiated is True:
                event = self._event("operator_command", "requester", now_ms, command_id)
                record = replace(record, event=event, effect_unknown=False)
                self.journal.put(record)
            return record

    def _event(self, trigger: str, source: str, now_ms: int,
               command_id: UUID | None) -> NodeEventV2:
        return NodeEventV2(
            producer=self.producer, event_id=uuid4(),
            sequence=self.journal.next_sequence(), occurred_boottime_ms=now_ms,
            facts=(RebootFact(trigger=trigger, source=source),),
            causative_command_id=command_id,
        )

    def observed_reboot_initiation(self, *, now_ms: int) -> NodeEventV2:
        """Called only by a base observer of an independently initiated reboot.

        No command or authorization is manufactured. The transport must durably
        enqueue the returned event; this method does not execute a reboot.
        """
        with self._lock:
            return self._event("unknown", "base_observer", now_ms, None)
