"""Display control tests: real database/Runtime authority, observations remain inert."""

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from test_node_runtime_reconciliation import rig

from central.fleet.node_display import NodeDisplay
from central.fleet.node_ingest import NodeIngest
from central.fleet.node_sessions import NodeControlError
from contracts.node_display import (
    DisplayExchange,
    DisplayReceipt,
    Surface,
    encode_display_exchange,
    parse_display_decision,
    parse_display_exchange,
)
from contracts.node_protocol import NodeEventV2, encode_node_message


def test_display_decision_and_completion_renewal(registry):
    coordinator, _, _, sessions, claims, grants, facts, reconciler, _ = rig(registry)
    claim, grant, fact = claims["display_host"], grants["display_host"], facts[0]
    surface = Surface(
        fact.output, fact.process, fact.app_epoch, fact.binding_generation, fact.config_revision, fact.frame_id
    )
    NodeIngest(sessions).ingest(
        grant.session_id,
        claim.credential,
        encode_node_message(
            NodeEventV2(
                grant.producer,
                uuid4(),
                3,
                1200,
                (replace(fact, state="invalidated", buffer_id=None),),
            )
        ),
    )
    reconciler.advance()
    display = NodeDisplay(sessions, runtime=coordinator)

    def exchange(request, carrier=claim):
        return parse_display_decision(
            display.exchange(
                carrier.session_id, carrier.credential, encode_display_exchange(request)
            )
        )

    request = DisplayExchange(grant.producer, uuid4(), 1300, fact.output, True)
    candidate = exchange(request)
    assert candidate.operation == "candidate" and candidate.surface == surface
    assert exchange(request) == candidate
    with pytest.raises(NodeControlError, match="request_conflict"):
        exchange(replace(request, connected=False))
    receipt = DisplayReceipt(surface, uuid4(), "weston-100", "synthetic", 1400)
    pending = replace(
        request, request_id=uuid4(), sampled_boottime_ms=1400, candidate=surface, receipt=receipt
    )
    handoff = exchange(pending)
    assert handoff.operation == "handoff"
    with registry.db.transaction() as conn:
        assert (
            conn.execute("SELECT resolved_at FROM node_output_losses").fetchone()["resolved_at"]
            is None
        )
    # Renewal transports the already-observed effect, preserving original decision/time.
    renewed = replace(
        claim,
        session_id=uuid4(),
        credential=uuid4().hex + uuid4().hex,
        sampled_boottime_ms=1500,
    )
    sessions.enroll(renewed)
    completion = replace(
        pending,
        request_id=uuid4(),
        sampled_boottime_ms=1600,
        admitted=surface,
        receipt=replace(receipt, buffer_id="weston-101", sampled_boottime_ms=1500),
        completed_decision_id=handoff.decision_id,
        completed_boottime_ms=1500,
    )
    assert exchange(completion, renewed).operation == "retain"
    with registry.db.transaction() as conn:
        assert (
            conn.execute("SELECT resolved_at FROM node_output_losses").fetchone()["resolved_at"]
            is not None
        )
        assert conn.execute("SELECT count(*) n FROM node_display_handoffs").fetchone()["n"] == 1
    # An expired decision cannot be reinterpreted as a new effect after renewal.
    late = replace(
        completion,
        request_id=uuid4(),
        sampled_boottime_ms=5000,
        completed_boottime_ms=handoff.expires_boottime_ms,
        receipt=replace(receipt, buffer_id="weston-102", sampled_boottime_ms=4900),
    )
    with pytest.raises(NodeControlError, match="receipt_mismatch|sample_stale"):
        exchange(late, renewed)


def test_same_process_config_change_requests_revision(registry):
    coordinator, _, _, sessions, claims, grants, facts, _, _ = rig(registry)
    fact, grant, claim = facts[0], grants["display_host"], claims["display_host"]
    old = Surface(
        fact.output, fact.process, fact.app_epoch, fact.binding_generation, fact.config_revision, fact.frame_id
    )
    from central.transaction_locks import acquire_runtime_locks
    from contracts.models import Calibration

    with registry.db.transaction() as conn:
        acquire_runtime_locks(conn)
        registry.commit_calibration_trial_in(conn, frame_id="node-f0", player_id=conn.execute("SELECT player_id FROM bindings WHERE frame_id='node-f0'").fetchone()["player_id"],
            authority_epoch=1, output_id=old.output.output_id, binding_generation=old.binding_generation,
            config_revision=old.config_revision, calibration_revision=2, calibration=Calibration(gain=0.8))
    request = DisplayExchange(grant.producer, uuid4(), 1500, fact.output, True, admitted=old)
    decision = parse_display_decision(
        NodeDisplay(sessions, runtime=coordinator).exchange(
            claim.session_id, claim.credential, encode_display_exchange(request)
        )
    )
    assert decision.operation == "revision"
    assert decision.surface.process == old.process
    assert decision.surface.config_revision > old.config_revision


def test_display_wire_unknown_fields_and_completion_time(registry):
    _, _, _, _, _, grants, facts, _, _ = rig(registry)
    request = DisplayExchange(grants["display_host"].producer, uuid4(), 1000, facts[0].output, True)
    assert parse_display_exchange(encode_display_exchange(request)) == request
    raw = json.loads(encode_display_exchange(request))
    raw["unexpected"] = True
    with pytest.raises(ValueError):
        parse_display_exchange(json.dumps(raw).encode())
    with pytest.raises(ValueError):
        replace(request, completed_decision_id=uuid4(), completed_boottime_ms=1001)


def test_delayed_display_pair_cannot_refresh_presentation_age(registry):
    coordinator, _, _, sessions, claims, grants, facts, _, _ = rig(registry)
    request = DisplayExchange(grants["display_host"].producer, uuid4(), 1000, facts[0].output, True)
    registry.clock.advance(6)
    with pytest.raises(NodeControlError, match="display_sample_stale"):
        NodeDisplay(sessions, runtime=coordinator).exchange(claims["display_host"].session_id,
            claims["display_host"].credential, encode_display_exchange(request))


def test_equal_counter_frame_rebind_requires_exact_withdrawal_and_role_removal(registry):
    from test_registry import frame

    from central.transaction_locks import acquire_runtime_locks
    from contracts.models import Calibration

    coordinator, _, _, sessions, claims, grants, facts, _, _ = rig(registry)
    claim, grant, fact = claims["display_host"], grants["display_host"], facts[0]
    old = Surface(fact.output, fact.process, fact.app_epoch, fact.binding_generation,
                  fact.config_revision, fact.frame_id)
    display = NodeDisplay(sessions, runtime=coordinator)
    def exchange(request, carrier=claim):
        return parse_display_decision(display.exchange(carrier.session_id, carrier.credential,
            encode_display_exchange(request)))
    request = DisplayExchange(grant.producer, uuid4(), 1300, fact.output, True, admitted=old)
    assert exchange(request).operation == "retain"
    with registry.db.transaction() as conn:
        player_id = conn.execute("SELECT player_id FROM bindings WHERE frame_id='node-f0'").fetchone()["player_id"]
    registry.unbind("node-f0", expected_generation=1)
    frame(registry, "replacement-frame")
    registry.bind("replacement-frame", player_id, fact.output.output_id, expected_generation=0)
    with registry.db.transaction() as conn:
        acquire_runtime_locks(conn)
        registry.commit_calibration_trial_in(conn, frame_id="replacement-frame", player_id=player_id,
            authority_epoch=1, output_id=fact.output.output_id, binding_generation=1,
            config_revision=2, calibration_revision=1, calibration=Calibration())
    request = replace(request, request_id=uuid4(), sampled_boottime_ms=1400)
    withdraw = exchange(request)
    assert withdraw.operation == "withdraw" and withdraw.surface == old
    renewed = replace(claim, session_id=uuid4(), credential=uuid4().hex+uuid4().hex,
                      sampled_boottime_ms=1500)
    sessions.enroll(renewed)
    removed = replace(request, request_id=uuid4(), sampled_boottime_ms=1700, admitted=None,
                      completed_decision_id=withdraw.decision_id, completed_boottime_ms=1500)
    candidate = exchange(removed, renewed)
    assert candidate.operation == "candidate"
    assert candidate.surface.frame_id == "replacement-frame"
    assert (candidate.surface.binding_generation, candidate.surface.config_revision) == (
        old.binding_generation, old.config_revision)
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) n FROM node_display_withdrawals").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) n FROM node_display_handoffs").fetchone()["n"] == 0
    # A historical removal retry records only the old outcome; it does not
    # reapply withdrawal or disturb an already-admitted replacement.
    assert exchange(replace(removed, request_id=uuid4(), sampled_boottime_ms=1800,
                            admitted=candidate.surface), renewed).operation == "retain"
    with pytest.raises(NodeControlError, match="withdrawal_completion_mismatch"):
        exchange(replace(removed, request_id=uuid4(), sampled_boottime_ms=1900,
                         admitted=old), renewed)


@pytest.mark.parametrize("renew", [False, True])
def test_promoted_revision_unbind_before_upload_uses_exact_issued_role(registry, renew):
    from central.transaction_locks import acquire_runtime_locks
    from contracts.models import Calibration

    coordinator, _, _, sessions, claims, grants, facts, _, _ = rig(registry)
    claim, grant, fact = claims["display_host"], grants["display_host"], facts[0]
    old = Surface(fact.output, fact.process, fact.app_epoch, fact.binding_generation,
                  fact.config_revision, fact.frame_id)
    display = NodeDisplay(sessions, runtime=coordinator)

    def exchange(request):
        return parse_display_decision(display.exchange(claim.session_id, claim.credential,
            encode_display_exchange(request)))

    with registry.db.transaction() as conn:
        acquire_runtime_locks(conn)
        player_id = conn.execute("SELECT player_id FROM bindings WHERE frame_id='node-f0'").fetchone()["player_id"]
        registry.commit_calibration_trial_in(conn, frame_id="node-f0", player_id=player_id,
            authority_epoch=1, output_id=old.output.output_id, binding_generation=old.binding_generation,
            config_revision=old.config_revision, calibration_revision=2, calibration=Calibration(gain=0.8))
    request = DisplayExchange(grant.producer, uuid4(), 1300, fact.output, True, admitted=old)
    revision = exchange(request)
    assert revision.operation == "revision"
    # Real native promotion occurred but was not uploaded before Registry unbound.
    registry.unbind("node-f0", expected_generation=1)
    if renew:
        claim = replace(claim, session_id=uuid4(), credential=uuid4().hex+uuid4().hex,
                        sampled_boottime_ms=1400)
        sessions.enroll(claim)
    promoted = revision.surface
    receipt = DisplayReceipt(promoted, uuid4(), "weston-promoted", "baseline", 1400)
    request = replace(request, request_id=uuid4(), sampled_boottime_ms=1500,
                      admitted=promoted, receipt=receipt)
    # Snapshots and invented roles cannot establish authorization for removal.
    for unknown in (replace(promoted, frame_id="different-frame"),
                    replace(promoted, config_revision=promoted.config_revision+1),
                    replace(promoted, process=replace(promoted.process, start_ticks=promoted.process.start_ticks+1))):
        with pytest.raises(NodeControlError, match="withdrawal_previous_unknown"):
            exchange(replace(request, admitted=unknown, receipt=replace(receipt, surface=unknown)))
    with pytest.raises(NodeControlError, match="withdrawal_previous_unknown"):
        exchange(replace(request, receipt=None))
    with pytest.raises(NodeControlError, match="withdrawal_previous_unknown"):
        exchange(replace(request, receipt=replace(receipt, sampled_boottime_ms=1200)))
    decision = exchange(request)
    assert decision.operation == "withdraw" and decision.surface == promoted
    assert decision.runtime_fence is not None
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) n FROM node_display_handoffs").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) n FROM node_display_withdrawals").fetchone()["n"] == 0
