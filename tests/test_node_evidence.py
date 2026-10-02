from dataclasses import replace
from uuid import UUID

import pytest

from central.fleet.node_evidence import EvidenceProjection, reconcile_evidence
from contracts.node_protocol import (
    AppProcessFact,
    NodeCommandResponseV2,
    NodeEventV2,
    NodeProcessIdentity,
    NodeProducerV2,
    NodeSnapshotV2,
)


def fixture_values():
    p = NodeProducerV2("test", "device-" + "a" * 64, 1, UUID(int=1),
                       "app_effect_broker", UUID(int=2))
    a = AppProcessFact(NodeProcessIdentity(100, 10, UUID(int=3)), 1, "a" * 64, "running")
    b = AppProcessFact(NodeProcessIdentity(101, 11, UUID(int=4)), 2, "b" * 64, "running")
    return p, a, b


def test_partial_snapshot_fences_only_covered_fact():
    p, a, b = fixture_values()
    snap = NodeSnapshotV2(p, UUID(int=5), 10, 100, (replace(a, state="exited"),))
    state = reconcile_evidence(EvidenceProjection(p), snap, received_at=1).projection
    delayed = NodeEventV2(p, UUID(int=6), 9, 90, (a, b))
    result = reconcile_evidence(state, delayed, received_at=2)
    assert result.disposition == "applied"
    assert [(x.fact.state, x.sequence) for x in result.projection.facts] == [("exited", 10), ("running", 9)]
    older = NodeSnapshotV2(p, UUID(int=7), 8, 80, (a, replace(b, state="exited")))
    assert reconcile_evidence(result.projection, older, received_at=3).projection == result.projection


def test_duplicate_preserves_evidence_age_and_conflicting_identity_rejected():
    p, a, _ = fixture_values()
    event = NodeEventV2(p, UUID(int=6), 1, 10, (a,))
    state = reconcile_evidence(EvidenceProjection(p), event, received_at=1).projection
    result = reconcile_evidence(state, event, received_at=999, previous=event)
    assert result.disposition == "duplicate"
    assert result.projection.facts[0].received_at == 1
    with pytest.raises(ValueError, match="conflicting_evidence"):
        reconcile_evidence(state, replace(event, facts=(replace(a, state="exited"),)),
                           received_at=999, previous=event)


@pytest.mark.parametrize("change", [{"kernel_boot_id": UUID(int=9)},
                                     {"incarnation_id": UUID(int=9)}, {"device_generation": 2}])
def test_unadmitted_context_cannot_elect_itself(change):
    p, a, _ = fixture_values()
    event = NodeEventV2(replace(p, **change), UUID(int=6), 999, 10, (a,))
    result = reconcile_evidence(EvidenceProjection(p), event, received_at=1)
    assert result.disposition == "historical"
    assert result.projection.facts == ()


def test_command_response_cannot_become_effect_evidence():
    p, _, _ = fixture_values()
    response = NodeCommandResponseV2(p, UUID(int=5), "f" * 64, UUID(int=6),
                                     "app_effect", "accepted", "accepted")
    with pytest.raises(ValueError, match="invalid_evidence_message"):
        reconcile_evidence(EvidenceProjection(p), response, received_at=1)


def test_projection_constructor_rejects_cross_owner_fact():
    from central.fleet.node_evidence import ProjectedFact
    p, a, _ = fixture_values()
    with pytest.raises(ValueError, match="unowned"):
        EvidenceProjection(replace(p, owner="host_core"), (ProjectedFact(a, 1, 1, 1),))
