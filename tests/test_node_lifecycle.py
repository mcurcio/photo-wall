"""Actual PostgreSQL V2 switch desired state and effect projection; local effects inert."""
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb
from test_fleet_attempts import DEVICE_ID, SERIAL
from test_fleet_rollout_gate import _gate
from test_node_boot import claim_for, environment, seed_verified_publication
from test_node_runtime_reconciliation import rig

from central.fleet.node_acceptance import current_cohort_in
from central.fleet.node_boot import NodeBootService, parse_node_deployment
from central.fleet.node_display import NodeDisplay
from central.fleet.node_lifecycle import NodeLifecycle, OperatorAppStage
from central.fleet.node_sessions import NodeControlError
from contracts.node_boot import NodeBootRequestV2
from contracts.node_display import DisplayExchange, encode_display_exchange
from contracts.node_lifecycle import AppEffectEventV2, encode_app_effect_event, parse_stage_command
from contracts.node_protocol import NodeProcessIdentity


class Rig:
    def __init__(self, registry, *, qualified=True, unbound=True):
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
        self.deployments = []
        for digest in ('b', 'c'):
            target = environment(digest)
            sources = {deployment.manager_primary.environment_sha256:
                       deployment.environment_sources[deployment.manager_primary.environment_sha256],
                       target.environment_sha256: 'https://example.invalid/' + digest}
            selected = replace(deployment, deployment_id=uuid4(), app_environment=target, environment_sources=sources)
            seed_verified_publication(registry, selected)
            NodeBootService(sessions).publish(selected)
            self.deployments.append(selected)
        if unbound:
            registry.unbind('node-f0', expected_generation=1)
            registry.unbind('node-f1', expected_generation=1)
        gate, _ = _gate(registry)
        self.generation = gate.open(expected_revision=0).generation
        self.registry, self.sessions, self.proof = registry, sessions, proof
        self.service = NodeLifecycle(sessions, gate)
        self.claim = claims['app_effect_broker']

    def stage(self, index=0, *, session_id=None):
        issued = self.service.stage(DEVICE_ID, OperatorAppStage(uuid4(), uuid4(), session_id or self.claim.session_id,
            1, self.deployments[index].deployment_id, self.generation, 'operator:test'))
        return parse_stage_command(json.dumps(issued['command']).encode())

    def desired(self, claim=None):
        claim = claim or self.claim
        return [parse_stage_command(json.dumps(item).encode())
                for item in self.service.desired(claim.session_id, claim.credential, effects=True)['commands']]

    def report(self, command, phase, sequence, *, claim=None, environment_sha256=None):
        running = phase in ('starting_new', 'running', 'fallback_starting', 'fallback_running')
        event = AppEffectEventV2(command.producer, command.operation_id, command.command_id,
            command.command_sha256, command.command_session_id, uuid4(), sequence, 2000 + sequence, phase,
            NodeProcessIdentity(456, 30, uuid4()) if running else None,
            command.old_app_epoch + 1 if running else None,
            (environment_sha256 or command.target.environment_sha256) if running else None)
        claim = claim or self.claim
        return self.service.effect(claim.session_id, claim.credential, encode_app_effect_event(event))

    def states(self):
        return [item['state'] for item in self.service.status(DEVICE_ID)['operations']]


def test_stage_refuses_unqualified_fallback(registry):
    unqualified = Rig(registry, qualified=False)
    with pytest.raises(NodeControlError, match='qualified_fallback_required'):
        unqualified.stage()
    with registry.db.transaction() as conn:
        assert conn.execute('SELECT count(*) n FROM node_app_operations').fetchone()['n'] == 0


def test_stage_refuses_frame_bound_player(registry):
    bound = Rig(registry, unbound=False)
    with pytest.raises(NodeControlError, match='bound_switch_policy_unselected'):
        bound.stage()


def test_latest_stage_is_desired_state_without_command_expiry(registry):
    fixture = Rig(registry)
    first = fixture.stage(0)
    assert first.fallback.environment_sha256 == fixture.proof.challenge.environment_sha256
    assert fixture.desired() == [first]
    second = fixture.stage(1)
    registry.clock.advance(1800)  # Former stage lifetime: desired state does not expire.
    assert fixture.desired() == [second]
    assert fixture.states() == ['staged', 'superseded']


def test_reported_effects_project_operation_state_and_hold_nothing(registry):
    fixture = Rig(registry)
    command = fixture.stage()
    fixture.report(command, 'intent_stop', 1)
    assert fixture.states() == ['switching']
    fixture.report(command, 'stopped', 2)
    with pytest.raises(NodeControlError, match='environment_mismatch'):
        fixture.report(command, 'starting_new', 3, environment_sha256=command.fallback.environment_sha256)
    fixture.report(command, 'starting_new', 3)
    result = fixture.report(command, 'running', 4)
    assert result['stored'] and 'authority_granted' not in result
    status = fixture.service.status(DEVICE_ID)['operations'][0]
    assert status['state'] == 'target_running' and status['latest_effect']['phase'] == 'running'
    with pytest.raises(NodeControlError, match='sequence_conflict'):
        fixture.report(command, 'running', 4)
    # Nothing is held: the next stage is admitted immediately.
    fixture.stage(1)
    assert fixture.states() == ['staged', 'target_running']


def test_renewed_broker_session_keeps_the_stage_current_and_reportable(registry):
    fixture = Rig(registry)
    command = fixture.stage()
    renewed = replace(fixture.claim, session_id=uuid4(), credential=uuid4().hex + uuid4().hex)
    fixture.sessions.enroll(renewed)
    assert fixture.desired(renewed) == [command]
    fixture.report(command, 'intent_stop', 1, claim=renewed)
    assert fixture.states() == ['switching']


def test_operation_of_a_rebooted_boot_is_interrupted_and_not_desired(registry):
    fixture = Rig(registry)
    command = fixture.stage()
    fixture.report(command, 'intent_stop', 1)
    offer = NodeBootService(fixture.sessions).offer(NodeBootRequestV2(SERIAL, uuid4(), 'b' * 64))
    broker = claim_for(offer, owner='app_effect_broker')
    fixture.sessions.enroll(broker)
    assert fixture.states() == ['interrupted_by_reboot']
    assert fixture.service.desired(broker.session_id, broker.credential, effects=True)['commands'] == []
    # The old boot's late report is retained history and does not change the projection.
    assert fixture.report(command, 'stopped', 2)['disposition'] == 'historical'
    assert fixture.states() == ['interrupted_by_reboot']
