"""Pure evidence projection: no authority, persistence, clock, or Run effects.

The application adapter must atomically persist original evidence, receipt time,
this projection and reconciliation work under accepted-generation locks. It owns
unique event IDs AND (producer, event sequence), rejects conflicting reuse, and
passes previously stored same-ID evidence on retry. No ingress is mounted yet.

A projection's producer was admitted by a separate authority owner. A different
boot/incarnation is historical, never elected by sequence or arrival time. A new
admitted incarnation starts a new projection; retain the old one as history.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from contracts.node_protocol import (
    NodeEventV2,
    NodeFact,
    NodeProducerV2,
    NodeSnapshotV2,
    counter,
    fact_key,
    validate_owned_fact,
)

Evidence = NodeEventV2 | NodeSnapshotV2


@dataclass(frozen=True, slots=True)
class ProjectedFact:
    fact: NodeFact
    sequence: int
    observed_boottime_ms: int
    received_at: float

    def __post_init__(self) -> None:
        fact_key(self.fact)
        counter(self.sequence)
        counter(self.observed_boottime_ms)
        _receipt_time(self.received_at)


@dataclass(frozen=True, slots=True)
class EvidenceProjection:
    producer: NodeProducerV2
    facts: tuple[ProjectedFact, ...] = ()

    def __post_init__(self) -> None:
        if type(self.producer) is not NodeProducerV2 or type(self.facts) is not tuple:
            raise ValueError("invalid_evidence_projection")
        keys = []
        for item in self.facts:
            if type(item) is not ProjectedFact:
                raise ValueError("invalid_projected_fact")
            validate_owned_fact(self.producer, item.fact)
            keys.append(fact_key(item.fact))
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate_projected_fact")


@dataclass(frozen=True, slots=True)
class Reconciliation:
    projection: EvidenceProjection
    disposition: Literal["applied", "historical", "duplicate"]
    updated_keys: tuple[tuple, ...] = ()


def _receipt_time(value: float) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid_evidence_receipt_time")


def evidence_identity(message: Evidence) -> tuple:
    if type(message) is NodeEventV2:
        return (message.producer, "event", message.event_id)
    if type(message) is NodeSnapshotV2:
        return (message.producer, "snapshot", message.snapshot_id)
    raise ValueError("invalid_evidence_message")


def reconcile_evidence(
    projection: EvidenceProjection,
    message: Evidence,
    *,
    received_at: float,
    previous: Evidence | None = None,
) -> Reconciliation:
    """Advance each reported fact independently; preserve original evidence age.

    A snapshot's cut covers only its explicitly listed facts. Omission never
    deletes a fact. An equal/older watermark is historical. Stream-gap metadata
    stays on durable evidence, and is never interpreted as a missed transition.
    Command responses deliberately cannot enter this evidence reducer.
    """
    key = evidence_identity(message)
    _receipt_time(received_at)
    if previous is not None:
        if evidence_identity(previous) != key or previous != message:
            raise ValueError("conflicting_evidence_identity")
        return Reconciliation(projection, "duplicate")
    if message.producer != projection.producer:
        return Reconciliation(projection, "historical")
    sequence = (message.sequence if type(message) is NodeEventV2
                else message.covered_through_sequence)
    observed = (message.occurred_boottime_ms if type(message) is NodeEventV2
                else message.sampled_boottime_ms)
    facts = {fact_key(item.fact): item for item in projection.facts}
    updated = []
    for fact in message.facts:
        key = fact_key(fact)
        old = facts.get(key)
        if old is not None and sequence <= old.sequence:
            continue
        facts[key] = ProjectedFact(fact, sequence, observed, received_at)
        updated.append(key)
    if not updated:
        return Reconciliation(projection, "historical")
    return Reconciliation(
        EvidenceProjection(projection.producer, tuple(facts.values())),
        "applied", tuple(updated),
    )
