"""Current-proof admission observes operational lifecycle at one serialized DB cut."""
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from uuid import uuid4

import pytest
from test_fleet_attempts import DEVICE_ID
from test_node_lifecycle import event, setup
from test_registry import enroll

from central.fleet.node_app_links import NodeAppLinks
from central.fleet.node_sessions import NodeControlError
from central.transaction_locks import holds_runtime_locks_in
from contracts.node_app_link import NodeAppLinkV2, encode_node_app_link, node_app_link_message
from contracts.node_lifecycle import encode_app_effect_event, encode_stage_ready, parse_stop_permit
from contracts.node_protocol import NodeProcessIdentity
from contracts.player_control import ControlAck, ControlHello


def switched(registry, *, defect=None):
    service, claim, command, ready, _, player, key, old = setup(registry)
    permit = parse_stop_permit(service.ready(claim.session_id, claim.credential, encode_stage_ready(ready)))
    process = NodeProcessIdentity(456, 30, uuid4())
    phases = [('intent_stop', 1), ('stopped', 2)]
    if defect == 'missing_stop':
        phases = phases[:1]
    if defect == 'reversed_stop':
        phases = [('stopped', 1), ('intent_stop', 2)]
    for phase, sequence in phases:
        service.effect(claim.session_id, claim.credential,
                       encode_app_effect_event(event(command, permit, phase, sequence=sequence)))
    terminal = replace(event(command, permit, 'intent_stop', sequence=3), phase='running', process=process,
                       app_epoch=command.old_app_epoch + 1,
                       environment_sha256=command.target.environment_sha256)
    service.effect(claim.session_id, claim.credential, encode_app_effect_event(terminal))
    epoch = 1 if defect == 'old_authority' else 2
    if epoch == 2:
        player, _, _ = enroll(registry, key, device_id=DEVICE_ID)
        registry.control_hello(player['player_id'], ControlHello(authority_epoch=2, schemas=(2,), capabilities=()))
    if defect == 'bound':
        # A deliberately inconsistent post-permit fixture; no production bind bypass.
        with registry.db.transaction() as conn:
            conn.execute('INSERT INTO bindings VALUES(%s,%s,%s)', ('node-f0', player['player_id'], 'HDMI-A-1'))
    delivery = registry.issue_control_delivery_record(player['player_id'], epoch, 'c' * 64)
    receipt = registry.control_ack_response(player['player_id'], ControlAck(
        authority_epoch=epoch, delivery_id=delivery['delivery_id'], result='applied')).receipt
    challenge = replace(old.challenge, process=process, app_epoch=terminal.app_epoch,
        environment_sha256=terminal.environment_sha256, authority_epoch=epoch,
        control_receipt=json.dumps(receipt.model_dump(mode='json', by_alias=True), sort_keys=True, separators=(',', ':')),
        nonce=uuid4().hex * 2, sampled_boottime_ms=35001)
    if defect == 'wrong_process':
        challenge = replace(challenge, process=replace(process, start_ticks=31))
    proof = NodeAppLinkV2(challenge, old.public_key, key.sign(node_app_link_message(challenge)).hex())
    return service, claim, proof


def counts(registry):
    with registry.db.transaction() as conn:
        return (conn.execute('SELECT count(*) n FROM active_node_app_drains').fetchone()['n'],
                conn.execute('SELECT count(*) n FROM node_app_discharges').fetchone()['n'])


def supersede(registry, proof):
    challenge = proof.challenge
    delivery = registry.issue_control_delivery_record(challenge.player_id, challenge.authority_epoch, 'c' * 64)
    registry.control_ack_response(challenge.player_id, ControlAck(
        authority_epoch=challenge.authority_epoch, delivery_id=delivery['delivery_id'], result='applied'))


@pytest.mark.parametrize('atomic', [False, True])
def test_valid_ingress_discharge_survives_receipt_supersession_before_scheduler(registry, atomic):
    service, claim, proof = switched(registry)
    links = NodeAppLinks(service.sessions, observer=service if atomic else None)
    links.admit(claim.session_id, claim.credential, encode_node_app_link(proof))
    # Another ordinary same-content control poll+ACK before the next scheduler tick.
    supersede(registry, proof)
    assert service.reconcile() == 0
    assert counts(registry) == ((0, 1) if atomic else (1, 0))


@pytest.mark.parametrize('defect', ['wrong_process', 'old_authority', 'bound', 'missing_stop', 'reversed_stop'])
def test_atomic_link_observation_preserves_operational_refusals(registry, defect):
    service, claim, proof = switched(registry, defect=defect)
    assert NodeAppLinks(service.sessions, observer=service).admit(
        claim.session_id, claim.credential, encode_node_app_link(proof))['stored']
    assert counts(registry) == (1, 0)


@pytest.mark.parametrize('stale', [False, True])
def test_exact_link_replay_only_observes_when_receipt_still_current(registry, stale):
    service, claim, proof = switched(registry)
    raw = encode_node_app_link(proof)
    NodeAppLinks(service.sessions).admit(claim.session_id, claim.credential, raw)
    if stale:
        supersede(registry, proof)
    assert NodeAppLinks(service.sessions, observer=service).admit(claim.session_id, claim.credential, raw)['duplicate']
    assert counts(registry) == ((1, 0) if stale else (0, 1))


@pytest.mark.parametrize('defect', ['stale_receipt', 'bad_signature'])
def test_invalid_new_proof_cannot_trigger_observer(registry, defect):
    service, claim, proof = switched(registry)
    if defect == 'stale_receipt':
        supersede(registry, proof)
    else:
        proof = replace(proof, signature='0' * 128)
    with pytest.raises(NodeControlError, match='control_not_current|signature_invalid'):
        NodeAppLinks(service.sessions, observer=service).admit(
            claim.session_id, claim.credential, encode_node_app_link(proof))
    assert counts(registry) == (1, 0)


def test_runtime_locks_precede_authentication_and_control_cannot_overtake_observer(registry, monkeypatch):
    service, claim, proof = switched(registry)
    original = service.sessions.authenticate_in
    def authenticate(conn, *args, **kwargs):
        assert holds_runtime_locks_in(conn)
        return original(conn, *args, **kwargs)
    monkeypatch.setattr(service.sessions, 'authenticate_in', authenticate)
    entered, release, issuance_started = Event(), Event(), Event()
    class Observer:
        def observe_current_link_in(self, conn, principal):
            assert holds_runtime_locks_in(conn)
            entered.set()
            assert release.wait(5)
            service.observe_current_link_in(conn, principal)
    def issue():
        issuance_started.set()
        supersede(registry, proof)
    with ThreadPoolExecutor(2) as pool:
        admission = pool.submit(NodeAppLinks(service.sessions, observer=Observer()).admit,
                                claim.session_id, claim.credential, encode_node_app_link(proof))
        try:
            assert entered.wait(5)
            issuance = pool.submit(issue)
            assert issuance_started.wait(5)
            assert not issuance.done()
        finally:
            release.set()
        admission.result(timeout=5)
        issuance.result(timeout=5)
    assert counts(registry) == (0, 1)
