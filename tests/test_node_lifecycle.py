"""Actual PostgreSQL V2 switch and no-effect authority checks; local effects inert."""
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb
from test_fleet_attempts import DEVICE_ID
from test_fleet_rollout_gate import _gate
from test_node_boot import environment, seed_verified_publication
from test_node_runtime_reconciliation import rig

from central.fleet.node_acceptance import current_cohort_in
from central.fleet.node_app_links import NodeAppLinks
from central.fleet.node_boot import NodeBootService, parse_node_deployment
from central.fleet.node_display import NodeDisplay
from central.fleet.node_lifecycle import NodeLifecycle, OperatorAppStage
from central.fleet.node_sessions import NodeControlError
from central.transaction_locks import acquire_runtime_locks
from contracts.node_app_link import NodeAppLinkV2, encode_node_app_link, node_app_link_message
from contracts.node_display import DisplayExchange, encode_display_exchange
from contracts.node_lifecycle import (
    AppEffectEventV2,
    NoEffectProofV2,
    StageReadyV2,
    encode_app_effect_event,
    encode_no_effect_proof,
    encode_stage_ready,
    parse_revalidation_grant,
    parse_stage_command,
    parse_stop_permit,
    parse_stop_permit_receipt,
)
from contracts.player_control import ControlAck


def setup(registry, *, qualified=True, unbound=True):
    coordinator, player, key, sessions, claims, grants, surfaces, _, proof = rig(registry, node_v2=True)
    display = NodeDisplay(sessions, runtime=coordinator)
    for surface in surfaces:
        display.exchange(grants['display_host'].session_id, claims['display_host'].credential,
            encode_display_exchange(DisplayExchange(grants['display_host'].producer, uuid4(), 1000, surface.output, True)))
    with registry.db.transaction() as conn:
        deployment = parse_node_deployment(bytes(conn.execute('SELECT document FROM node_deployments').fetchone()['document']))
        cohort = current_cohort_in(conn, DEVICE_ID, 1, registry.clock.utc())
        if qualified:
            # Explicit pre-existing qualified record fixture, never an executable shortcut.
            qualification = uuid4()
            conn.execute('INSERT INTO node_app_qualifications VALUES(%s,%s,1,%s,%s,1000)',
                         (qualification, DEVICE_ID, proof.challenge.environment_sha256, 'test:qualified-fixture'))
            conn.execute('INSERT INTO node_environment_acceptances VALUES(%s,%s,%s,1,%s,%s,%s,%s,1000)',
                (uuid4(), qualification, DEVICE_ID, deployment.base.content_key,
                 proof.challenge.environment_sha256, Jsonb(cohort), Jsonb({'test_fixture': True})))
    target = environment('b')
    sources = {deployment.manager_primary.environment_sha256: deployment.environment_sources[deployment.manager_primary.environment_sha256],
               target.environment_sha256: 'https://example.invalid/target'}
    selected = replace(deployment, deployment_id=uuid4(), app_environment=target, environment_sources=sources)
    seed_verified_publication(registry, selected)
    NodeBootService(sessions).publish(selected)
    if unbound:
        registry.unbind('node-f0', expected_generation=1)
        registry.unbind('node-f1', expected_generation=1)
    gate, _ = _gate(registry)
    generation = gate.open(expected_revision=0).generation
    service = NodeLifecycle(sessions, gate)
    claim = claims['app_effect_broker']
    issued = service.stage(DEVICE_ID, OperatorAppStage(uuid4(), uuid4(), claim.session_id, 1,
                           selected.deployment_id, generation, 'operator:test'))
    command = parse_stage_command(json.dumps(issued['command']).encode())
    ready = StageReadyV2(command.producer, command.operation_id, command.command_id, command.command_sha256,
        command.command_session_id, uuid4(), 1, 1000, command.old_process, command.old_app_epoch,
        command.target.environment_sha256, command.fallback.environment_sha256 if command.fallback else None, True, True)
    return service, claim, command, ready, coordinator, player, key, proof


def event(command, permit, phase, *, sequence=1, occurred=34001):
    return AppEffectEventV2(command.producer, command.operation_id, command.command_id,
        command.command_sha256, command.command_session_id, uuid4(), sequence, occurred, phase,
        permit.permit_id, command.old_process if phase=='no_stop_quiescent' else None,
        command.old_app_epoch if phase=='no_stop_quiescent' else None,
        command.old_environment.environment_sha256 if phase=='no_stop_quiescent' else None,
        executor_sealed=phase=='no_stop_quiescent', journal_watermark=sequence if phase=='no_stop_quiescent' else None)


def fresh_proof(registry, service, claim, old_proof, key, *, sampled=35001):
    registry.clock.advance(1)
    delivery = registry.issue_control_delivery_record(old_proof.challenge.player_id, 1, uuid4().hex*2)
    receipt = registry.control_ack_response(old_proof.challenge.player_id,
        ControlAck(authority_epoch=1, delivery_id=delivery['delivery_id'], result='applied')).receipt
    challenge = replace(old_proof.challenge, control_receipt=json.dumps(receipt.model_dump(mode='json', by_alias=True),
        sort_keys=True, separators=(',', ':')), nonce=uuid4().hex*2, sampled_boottime_ms=sampled)
    proof = NodeAppLinkV2(challenge, old_proof.public_key, key.sign(node_app_link_message(challenge)).hex())
    NodeAppLinks(service.sessions).admit(claim.session_id, claim.credential, encode_node_app_link(proof))
    return proof


def test_missing_qualified_fallback_and_bound_policy_do_not_mint_permit(registry):
    service, claim, command, ready, *_ = setup(registry, qualified=False)
    assert command.fallback is None
    with pytest.raises(NodeControlError, match='qualified_fallback_required'):
        service.ready(claim.session_id, claim.credential, encode_stage_ready(ready))
    with registry.db.transaction() as conn:
        assert conn.execute('SELECT count(*) n FROM node_app_permits').fetchone()['n'] == 0


def test_bound_operation_stages_without_withdrawal(registry):
    service, claim, _, ready, *_ = setup(registry, unbound=False)
    with pytest.raises(NodeControlError, match='bound_drain_policy_unselected'):
        service.ready(claim.session_id, claim.credential, encode_stage_ready(ready))


def test_no_effect_requires_post_expiry_contiguous_seal_and_fresh_control(registry):
    service, claim, command, ready, coordinator, player, key, old_proof = setup(registry)
    raw_permit = service.ready(claim.session_id, claim.credential, encode_stage_ready(ready))
    permit = parse_stop_permit(raw_permit)
    assert service.ready(claim.session_id, claim.credential, encode_stage_ready(ready)) == raw_permit
    registry.clock.advance(34)
    fresh = event(command, permit, 'no_stop_quiescent', occurred=35000)
    service.effect(claim.session_id, claim.credential, encode_app_effect_event(fresh))
    revalidation_id = uuid4()
    raw_grant = service.revalidate(claim.session_id, claim.credential, command.operation_id, fresh.event_id, revalidation_id)
    grant = parse_revalidation_grant(raw_grant)
    registry.clock.advance(0.1)
    assert service.revalidate(claim.session_id, claim.credential, command.operation_id, fresh.event_id, revalidation_id) == raw_grant
    with pytest.raises(NodeControlError, match='not_renewable'):
        service.revalidate(claim.session_id, claim.credential, command.operation_id, fresh.event_id, uuid4())
    delivery = coordinator.delivery(player['player_id'], 1)
    assert delivery['plan'] is None and delivery['commits'] == ()
    with pytest.raises(NodeControlError, match='proof_not_fresh'):
        service.no_effect(claim.session_id, claim.credential, encode_no_effect_proof(
            NoEffectProofV2(command.operation_id, revalidation_id, fresh.event_id, old_proof)))
    proof = fresh_proof(registry, service, claim, old_proof, key, sampled=36001)
    result = service.no_effect(claim.session_id, claim.credential, encode_no_effect_proof(
        NoEffectProofV2(command.operation_id, grant.revalidation_id, fresh.event_id, proof)))
    assert result['released']
    with registry.db.transaction() as conn:
        assert conn.execute('SELECT count(*) n FROM active_node_app_drains').fetchone()['n'] == 0
        assert conn.execute('SELECT count(*) n FROM node_app_permits').fetchone()['n'] == 1


def test_intent_race_cannot_leave_drain_discharged(registry):
    service, claim, command, ready, _, _, key, old_proof = setup(registry)
    permit = parse_stop_permit(service.ready(claim.session_id, claim.credential, encode_stage_ready(ready)))
    registry.clock.advance(34)
    sealed = event(command, permit, 'no_stop_quiescent', occurred=35000)
    service.effect(claim.session_id, claim.credential, encode_app_effect_event(sealed))
    grant = parse_revalidation_grant(service.revalidate(claim.session_id, claim.credential,
        command.operation_id, sealed.event_id, uuid4()))
    proof = fresh_proof(registry, service, claim, old_proof, key, sampled=36001)
    no_effect = encode_no_effect_proof(NoEffectProofV2(command.operation_id, grant.revalidation_id, sealed.event_id, proof))
    intent = encode_app_effect_event(event(command, permit, 'intent_stop', sequence=2, occurred=1500))
    def discharge():
        try:
            return service.no_effect(claim.session_id, claim.credential, no_effect)
        except NodeControlError:
            return None
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(discharge)
        second = pool.submit(service.effect, claim.session_id, claim.credential, intent)
        first.result()
        second.result()
    with registry.db.transaction() as conn:
        assert conn.execute('SELECT count(*) n FROM active_node_app_drains').fetchone()['n'] == 1
        acquire_runtime_locks(conn)


@pytest.mark.parametrize('sequence,occurred,reason', [
    (1, 2000, 'quiescence_before_expiry'),
    (2, 35000, 'journal_not_sealed_contiguous'),
])
def test_delayed_pre_expiry_or_incomplete_seal_cannot_revalidate(registry, sequence, occurred, reason):
    service, claim, command, ready, *_ = setup(registry)
    permit = parse_stop_permit(service.ready(claim.session_id, claim.credential, encode_stage_ready(ready)))
    registry.clock.advance(34)
    sealed = event(command, permit, 'no_stop_quiescent', sequence=sequence, occurred=occurred)
    service.effect(claim.session_id, claim.credential, encode_app_effect_event(sealed))
    with pytest.raises(NodeControlError, match=reason):
        service.revalidate(claim.session_id, claim.credential, command.operation_id, sealed.event_id, uuid4())


def test_lost_permit_response_replays_exact_then_recovers_without_authority(registry):
    service, claim, command, ready, *_ = setup(registry)
    raw = encode_stage_ready(ready)
    original = service.ready(claim.session_id, claim.credential, raw)
    registry.clock.advance(6)
    assert service.ready(claim.session_id, claim.credential, raw) == original
    with pytest.raises(NodeControlError, match='identity_conflict'):
        service.ready(claim.session_id, claim.credential,
                      encode_stage_ready(replace(ready, sampled_boottime_ms=7000)))
    # Receipt is never executable even while the enclosed permit is live.
    recovery = service.permit_receipt(claim.session_id, claim.credential, command.operation_id)
    assert parse_stop_permit_receipt(recovery).permit == parse_stop_permit(original)
    with pytest.raises(ValueError):
        parse_stop_permit(recovery)
    registry.clock.advance(30)
    with pytest.raises(NodeControlError, match='not_renewable'):
        service.ready(claim.session_id, claim.credential, raw)
    assert service.permit_receipt(claim.session_id, claim.credential, command.operation_id) == recovery
    with registry.db.transaction() as conn:
        assert conn.execute('SELECT count(*) n FROM node_app_permits').fetchone()['n'] == 1
        assert conn.execute('SELECT count(*) n FROM active_node_app_drains').fetchone()['n'] == 1


def test_expired_revalidation_is_visible_recovery_required_with_retained_fence(registry):
    service, claim, command, ready, *_ = setup(registry)
    permit = parse_stop_permit(service.ready(claim.session_id, claim.credential, encode_stage_ready(ready)))
    registry.clock.advance(34)
    sealed = event(command, permit, 'no_stop_quiescent', occurred=35000)
    service.effect(claim.session_id, claim.credential, encode_app_effect_event(sealed))
    service.revalidate(claim.session_id, claim.credential, command.operation_id, sealed.event_id, uuid4())
    registry.clock.advance(61)
    status = service.status(DEVICE_ID)['operations'][0]
    assert status['state'] == 'recovery_required' and status['runtime_fenced']
    assert status['artifact_roots_retained'] and status['command_response'] is None
    assert status['latest_effect']['phase'] == 'no_stop_quiescent'


def cancellation(command, *, sequence=1):
    return AppEffectEventV2(command.producer, command.operation_id, command.command_id,
        command.command_sha256, command.command_session_id, uuid4(), sequence, 1000,
        'cancelled_before_stop', None, command.old_process, command.old_app_epoch,
        command.old_environment.environment_sha256, executor_sealed=True, journal_watermark=sequence)


def test_sealed_stage_cancellation_is_atomic_and_retry_keeps_permit_closed(registry):
    service, claim, command, ready, *_ = setup(registry)
    sealed = cancellation(command)
    raw = encode_app_effect_event(sealed)
    result = service.effect(claim.session_id, claim.credential, raw)
    assert result['stage_closed'] and not result['authority_granted']
    assert service.effect(claim.session_id, claim.credential, raw)['stage_closed']
    with pytest.raises(NodeControlError, match='stage_cancelled'):
        service.ready(claim.session_id, claim.credential, encode_stage_ready(ready))
    assert service.status(DEVICE_ID)['operations'][0]['state'] == 'cancelled_before_stop'
    with pytest.raises(NodeControlError, match='permit_unknown'):
        service.permit_receipt(claim.session_id, claim.credential, command.operation_id)


def test_sealed_cancellation_after_lost_permit_never_claims_closed(registry):
    service, claim, command, ready, *_ = setup(registry)
    permit = parse_stop_permit(service.ready(claim.session_id, claim.credential, encode_stage_ready(ready)))
    raw = encode_app_effect_event(cancellation(command))
    assert not service.effect(claim.session_id, claim.credential, raw)['stage_closed']
    assert not service.effect(claim.session_id, claim.credential, raw)['stage_closed']
    retained = parse_stop_permit_receipt(service.permit_receipt(claim.session_id, claim.credential, command.operation_id))
    assert retained.permit == permit
    registry.clock.advance(34)
    sealed = event(command, permit, 'no_stop_quiescent', sequence=2, occurred=35000)
    service.effect(claim.session_id, claim.credential, encode_app_effect_event(sealed))
    assert parse_revalidation_grant(service.revalidate(claim.session_id, claim.credential,
        command.operation_id, sealed.event_id, uuid4())).operation_id == command.operation_id


def test_stage_cancellation_requires_contiguous_sealed_old_context(registry):
    service, claim, command, ready, *_ = setup(registry)
    sealed = cancellation(command, sequence=2)
    with pytest.raises(NodeControlError, match='journal_not_sealed_contiguous'):
        service.effect(claim.session_id, claim.credential, encode_app_effect_event(sealed))
    with pytest.raises(NodeControlError, match='old_process_changed'):
        service.effect(claim.session_id, claim.credential,
                       encode_app_effect_event(replace(sealed, process=replace(command.old_process, pid=456))))
    assert parse_stop_permit(service.ready(claim.session_id, claim.credential, encode_stage_ready(ready)))
