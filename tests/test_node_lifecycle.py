"""Actual PostgreSQL V2 switch desired state and effect projection; local effects inert.

A bound Player's switch follows the operator-reboot rule (D16, console DDD Part E G6);
a finished stage reads ended_by_later_boot once a later boot is admitted (G2)."""
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb
from test_coordination import report
from test_fleet_attempts import DEVICE_ID, SERIAL
from test_fleet_rollout_gate import _certificate, _gate
from test_node_boot import claim_for, environment, publish_deployment
from test_node_runtime_reconciliation import rig
from test_registry import enroll

from central.coordination import CoordinationError
from central.displays.model import Readiness
from central.fleet.node_acceptance import current_cohort_in
from central.fleet.node_app_links import NodeAppLinks
from central.fleet.node_boot import NodeBootService, parse_node_deployment
from central.fleet.node_display import NodeDisplay
from central.fleet.node_ingest import NodeIngest
from central.fleet.node_lifecycle import NodeLifecycle, OperatorAppStage
from central.fleet.node_sessions import NodeControlError
from central.node_runtime_reconciliation import NodeRuntimeReconciler
from contracts.node_app_link import (
    NodeAppLinkChallengeV2,
    NodeAppLinkV2,
    encode_node_app_link,
    node_app_link_message,
)
from contracts.node_boot import NodeBootRequestV2
from contracts.node_display import DisplayExchange, encode_display_exchange
from contracts.node_lifecycle import AppEffectEventV2, encode_app_effect_event, parse_stage_command
from contracts.node_protocol import (
    AppProcessFact,
    NodeEventV2,
    NodeProcessIdentity,
    encode_node_message,
)
from contracts.player_control import ControlAck, ControlHello


class Rig:
    def __init__(self, registry, *, qualified=True, unbound=True, gate_seconds=60):
        coordinator, player, key, sessions, claims, grants, surfaces, _, proof = rig(registry)
        self.display = NodeDisplay(sessions, runtime=coordinator)
        self.display_claim, self.display_grant, self.surfaces = claims['display_host'], grants['display_host'], surfaces
        self.refresh_display(1000)
        self.registry, self.sessions, self.proof = registry, sessions, proof
        if qualified:
            # Explicit pre-existing qualified record fixture, never an executable shortcut.
            _accept(self, proof.challenge.environment_sha256, accepted_at=1000)
        with registry.db.transaction() as conn:
            deployment = parse_node_deployment(bytes(conn.execute('SELECT document FROM node_deployments').fetchone()['document']))
        self.deployments = []
        for digest in ('b', 'c'):
            target = environment(digest)
            sources = {deployment.manager_primary.environment_sha256:
                       deployment.environment_sources[deployment.manager_primary.environment_sha256],
                       target.environment_sha256: 'https://example.invalid/' + digest}
            selected = replace(deployment, deployment_id=uuid4(), app_environment=target, environment_sources=sources)
            publish_deployment(registry.db, selected, registry.clock)
            self.deployments.append(selected)
        if unbound:
            registry.unbind('node-f0', expected_generation=1)
            registry.unbind('node-f1', expected_generation=1)
        gate, _ = _gate(registry, _certificate(expires_in=gate_seconds))
        self.generation = gate.open(expected_revision=0).generation
        self.coordinator, self.key, self.grants = coordinator, key, grants
        self.service = NodeLifecycle(sessions, gate)
        self.claim = claims['app_effect_broker']

    def refresh_display(self, sampled_boottime_ms):
        """Current output evidence: a qualified fallback needs a fresh cohort at stage time."""
        for surface in self.surfaces:
            self.display.exchange(self.display_claim.session_id, self.display_claim.credential,
                encode_display_exchange(DisplayExchange(self.display_grant.producer, uuid4(),
                                                        sampled_boottime_ms, surface.output, True)))

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


def _first_deployment(conn):
    """The first published deployment: the cold boot's, whose base every Rig deployment shares."""
    return parse_node_deployment(bytes(conn.execute(
        'SELECT document FROM node_deployments ORDER BY published_at LIMIT 1').fetchone()['document']))


def _first_base_key(conn):
    return _first_deployment(conn).base.content_key


def _accept(fixture, environment_sha256, *, accepted_at, generation=None, base_key=None,
            audit_ref='test:qualified-fixture'):
    """A stored acceptance (its qualification, then the acceptance) in the current output cohort:
    this boot's base and the current device generation unless `base_key` or `generation` says otherwise."""
    with fixture.registry.db.transaction() as conn:
        current = fixture.sessions.lock_device_generation_in(conn, DEVICE_ID)
        cohort = current_cohort_in(conn, DEVICE_ID, current, fixture.registry.clock.utc())
        generation = current if generation is None else generation
        qualification = uuid4()
        conn.execute('INSERT INTO node_app_qualifications(qualification_id,device_id,device_generation,'
                     'environment_sha256,operator_audit_ref,opened_at) VALUES(%s,%s,%s,%s,%s,%s)',
                     (qualification, DEVICE_ID, generation, environment_sha256, audit_ref, accepted_at))
        conn.execute('INSERT INTO node_environment_acceptances(acceptance_id,qualification_id,device_id,'
                     'device_generation,base_content_key,environment_sha256,cohort,evidence,accepted_at) '
                     'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                     (uuid4(), qualification, DEVICE_ID, generation, base_key or _first_base_key(conn),
                      environment_sha256, Jsonb(cohort), Jsonb({'test_fixture': True}), accepted_at))


def _other_key(key):
    """Another content key of the same shape."""
    return ('0' if key[0] != '0' else '1') + key[1:]


def test_stage_refuses_unqualified_fallback(registry):
    unqualified = Rig(registry, qualified=False)
    with pytest.raises(NodeControlError, match='qualified_fallback_required'):
        unqualified.stage()
    with registry.db.transaction() as conn:
        assert conn.execute('SELECT count(*) n FROM node_app_operations').fetchone()['n'] == 0


def test_stage_admits_a_frame_bound_player(registry):
    """D16 (G6): a bound Player is staged like an unbound one; the switch's consequences are
    test_a_bound_players_switch_follows_the_operator_reboot_rule."""
    bound = Rig(registry, unbound=False)
    command = bound.stage()
    assert bound.desired() == [command] and bound.states() == ['staged']
    with registry.db.transaction() as conn:
        assert conn.execute('SELECT count(*) n FROM bindings').fetchone()['n'] == 2


def test_the_app_attempts_read_serves_the_linked_app_and_this_generations_acceptances(registry):
    """G4: what a qualification would observe (the current boot's linked app) and what earlier
    qualifications recorded, with the base named for the base this boot runs; no verdict."""
    fixture = Rig(registry)
    with registry.db.transaction() as conn:
        deployment = _first_deployment(conn)
    block = fixture.service.status(DEVICE_ID)['qualification']
    linked = fixture.proof.challenge.environment_sha256
    assert block['linked_app']['environment_sha256'] == linked
    assert isinstance(block['linked_app']['admitted_at'], float)
    assert block['acceptances'] == [{'environment_sha256': linked, 'base_content_key': deployment.base.content_key,
                                     'base_tag': deployment.base.tag, 'accepted_at': 1000.0}]
    assert 'usable' not in block['acceptances'][0]
    # A later boot has linked no app yet; the generation's acceptance is still listed.
    _later_boot(fixture)
    block = fixture.service.status(DEVICE_ID)['qualification']
    assert block['linked_app'] is None and len(block['acceptances']) == 1


def test_an_acceptance_on_another_base_is_listed_without_this_boots_base_tag(registry):
    """G4: a base tag names only an acceptance on the base this boot runs; one recorded on any
    other base content key is listed with no tag, so it never reads as this base's."""
    fixture = Rig(registry)
    with registry.db.transaction() as conn:
        base_key = _first_base_key(conn)
    other_key = _other_key(base_key)
    _accept(fixture, fixture.proof.challenge.environment_sha256, accepted_at=1001, base_key=other_key,
            audit_ref='test:other-base')
    acceptances = fixture.service.status(DEVICE_ID)['qualification']['acceptances']
    assert [(row['base_content_key'], row['base_tag'] is None) for row in acceptances] == [
        (other_key, True), (base_key, False)]


def test_a_reenrolled_app_on_the_same_boot_serves_no_linked_app_until_it_links(registry):
    """G4: after the Player app re-enrolls (a new authority epoch, as on every bound switch) and
    before it links, the old link is not the app a qualification would observe."""
    fixture = Rig(registry)
    assert fixture.service.status(DEVICE_ID)['qualification']['linked_app'] is not None
    player, _, _ = enroll(registry, fixture.key, device_id=DEVICE_ID)
    assert player['authority_epoch'] == 2
    assert fixture.service.status(DEVICE_ID)['qualification']['linked_app'] is None


def test_an_unqualified_player_serves_no_acceptances(registry):
    block = Rig(registry, qualified=False).service.status(DEVICE_ID)['qualification']
    assert block['acceptances'] == [] and block['linked_app'] is not None


def test_an_acceptance_of_another_device_generation_is_not_listed(registry):
    """G4: acceptances are this device generation's only; one recorded under any other
    generation (an earlier or later registration of the box) is never served as current."""
    fixture = Rig(registry, qualified=False)
    with registry.db.transaction() as conn:
        generation = fixture.sessions.lock_device_generation_in(conn, DEVICE_ID)
    _accept(fixture, fixture.proof.challenge.environment_sha256, accepted_at=1001, generation=generation + 1,
            audit_ref='test:other-generation')
    with registry.db.transaction() as conn:
        stored = conn.execute('SELECT count(*) n FROM node_environment_acceptances WHERE device_id=%s',
                              (DEVICE_ID,)).fetchone()['n']
    assert stored == 1  # positive control: the row exists, under the other generation
    assert fixture.service.status(DEVICE_ID)['qualification']['acceptances'] == []


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
    assert fixture.states() == ['staged', 'superseded']


def test_a_replaced_operation_is_superseded_and_keeps_its_last_effect(registry):
    fixture = Rig(registry)
    first = fixture.stage(0)
    fixture.report(first, 'intent_stop', 1)
    fixture.stage(1)
    replaced = fixture.service.status(DEVICE_ID)['operations'][1]
    assert replaced['state'] == 'superseded' and replaced['latest_effect']['phase'] == 'intent_stop'
    fixture.report(first, 'stopped', 2)  # A late effect of the replaced stage stays detail.
    assert fixture.states() == ['staged', 'superseded']


def test_renewed_broker_session_keeps_the_stage_current_and_reportable(registry):
    fixture = Rig(registry)
    command = fixture.stage()
    renewed = replace(fixture.claim, session_id=uuid4(), credential=uuid4().hex + uuid4().hex)
    fixture.sessions.enroll(renewed)
    assert fixture.desired(renewed) == [command]
    fixture.report(command, 'intent_stop', 1, claim=renewed)
    assert fixture.states() == ['switching']


@pytest.mark.parametrize('phases, before', [((), 'staged'), (('intent_stop', 'effect_unknown'), 'effect_unknown')])
def test_a_staged_or_effect_unknown_operation_of_a_rebooted_boot_is_interrupted(registry, phases, before):
    """G2's other arms (Part E §27): Staged and EffectUnknown become interrupted_by_reboot, never
    ended_by_later_boot, once a later boot is admitted; each is asserted first, so neither passes vacuously."""
    fixture = Rig(registry)
    command = fixture.stage()
    for sequence, phase in enumerate(phases, start=1):
        fixture.report(command, phase, sequence)
    assert fixture.states() == [before]
    _later_boot(fixture)
    assert fixture.states() == ['interrupted_by_reboot']


def _wrong_acceptance(fixture, kind, environment_sha256, accepted_at):
    """An acceptance in the current cohort that differs from this boot only by its base content
    key (`kind='base'`) or its device generation (`kind='generation'`)."""
    with fixture.registry.db.transaction() as conn:
        generation = fixture.sessions.lock_device_generation_in(conn, DEVICE_ID)
        base_key = _first_base_key(conn)
    if kind == 'base':
        _accept(fixture, environment_sha256, accepted_at=accepted_at, base_key=_other_key(base_key),
                audit_ref='test:other-base')
    else:
        _accept(fixture, environment_sha256, accepted_at=accepted_at, generation=generation + 1,
                audit_ref='test:other-generation')


@pytest.mark.parametrize('kind', ['base', 'generation'])
def test_the_qualified_fallback_is_this_boots_base_and_this_device_generations_only(registry, kind):
    """G4's one home of fallback admission (`_qualified_fallback_in`): an acceptance on another
    base, or under another device generation, never becomes the fallback, alone or when newest.
    With G6 this is the only guard that a failed target on a bound wall restores a runnable app."""
    fixture = Rig(registry, qualified=False)
    other = fixture.deployments[1].app_environment.environment_sha256  # known to Central, not the target
    _wrong_acceptance(fixture, kind, other, 1001)
    with pytest.raises(NodeControlError, match='qualified_fallback_required'):
        fixture.stage(0)
    # The right acceptance, older than the wrong one: the fallback is still the right one.
    linked = fixture.proof.challenge.environment_sha256
    _accept(fixture, linked, accepted_at=1000)
    assert fixture.stage(0).fallback.environment_sha256 == linked


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


def _later_boot(fixture):
    """A later kernel boot of the same device is offered and its broker admitted; its claim."""
    offer = NodeBootService(fixture.sessions).offer(NodeBootRequestV2(SERIAL, uuid4(), 'b' * 64))
    claim = claim_for(offer, owner='app_effect_broker')
    fixture.sessions.enroll(claim)
    return claim


def test_a_superseded_boot_that_claims_again_is_current_and_its_operation_reads_its_own_state(registry):
    """Errata item 9, by design (console DDD §42, G8 withdrawn): a claim for its matching offer
    always admits its boot (`player-node-domain-model.md` Host report and reboot). A superseded
    kernel boot that is still running and claims again is admitted again, the later boot's
    sessions are revoked, and the earlier boot's operation reads its own state once more: the
    visible flap, because that boot is current again."""
    fixture = Rig(registry)
    command = fixture.stage()
    for sequence, phase in enumerate(('intent_stop', 'stopped', 'starting_new', 'running'), start=1):
        fixture.report(command, phase, sequence)
    later = _later_boot(fixture)
    assert fixture.states() == ['ended_by_later_boot']
    # The earlier boot's broker re-enrolls with a new session and the same kernel boot id.
    again = replace(fixture.claim, session_id=uuid4(), credential=uuid4().hex + uuid4().hex)
    fixture.sessions.enroll(again)
    with registry.db.transaction() as conn:
        revoked = conn.execute('SELECT revoked_at FROM node_sessions WHERE session_id=%s',
                               (later.session_id,)).fetchone()['revoked_at']
        current = conn.execute('SELECT kernel_boot_id FROM node_boot_admissions WHERE device_id=%s '
                               'AND superseded_at IS NULL', (DEVICE_ID,)).fetchall()
    assert revoked is not None
    assert [row['kernel_boot_id'] for row in current] == [fixture.claim.kernel_boot_id]
    assert fixture.states() == ['target_running']
    assert fixture.desired(again) == [command]


@pytest.mark.parametrize('phases, reached', [
    (('intent_stop', 'stopped', 'starting_new', 'running'), 'target_running'),
    (('intent_stop', 'stopped', 'target_failed', 'fallback_starting', 'fallback_running'), 'fallback_running')])
def test_a_finished_stage_reads_ended_by_a_later_boot_and_unchanged_without_one(registry, phases, reached):
    fixture = Rig(registry)
    command = fixture.stage()
    for sequence, phase in enumerate(phases, start=1):
        fixture.report(command, phase, sequence, environment_sha256=command.fallback.environment_sha256
                       if phase.startswith('fallback') else None)
    registry.clock.advance(600)
    assert fixture.states() == [reached]  # no later boot: the finished state stands
    _later_boot(fixture)
    [ended] = fixture.service.status(DEVICE_ID)['operations']
    assert ended['state'] == 'ended_by_later_boot' and ended['latest_effect']['phase'] == reached.replace(
        'target_', '')


def _bound_state(registry):
    with registry.db.transaction() as conn:
        bindings = conn.execute('SELECT frame_id,player_id,output_id FROM bindings ORDER BY frame_id').fetchall()
        frames = conn.execute('SELECT id,generation,configuration_revision,calibration,position_generation,position_display_id '
                              'FROM frames ORDER BY id').fetchall()
        losses = conn.execute('SELECT frame_id,authority_epoch FROM node_output_losses '
                              'WHERE resolved_at IS NULL ORDER BY frame_id').fetchall()
    return bindings, frames, losses


def _committed(coordinator, player_id, epoch):
    delivery = coordinator.delivery(player_id, epoch)
    committed = {a for commit in delivery['commits'] for a in commit.assignment_ids}
    return {(layer.frame_id, layer.output_id) for layer in delivery['plan'].layers if layer.assignment_id in committed}


def test_a_bound_players_switch_follows_the_operator_reboot_rule(registry):
    """D16 (G6): the old app's observed exit interrupts each bound Output; bindings and
    calibration are untouched; the new app's enrollment rejoins the Run at its current point."""
    fixture = Rig(registry, unbound=False)
    coordinator = fixture.coordinator
    player_id = fixture.proof.challenge.player_id
    bindings, frames, losses = _bound_state(registry)
    assert len(bindings) == 2 and losses == []
    assert {frame.readiness for frame in registry.inventory().frames} == {Readiness.READY}
    both = {('node-f0', 'HDMI-A-1'), ('node-f1', 'HDMI-A-2')}
    assert _committed(coordinator, player_id, 1) == both
    runtime = coordinator.runtime.read().export_state()
    command = fixture.stage()  # admitted while bound
    fixture.report(command, 'intent_stop', 1)
    # The broker stops the old process: its observed exit, as on an operator reboot.
    exited = AppProcessFact(fixture.proof.challenge.process, 42, 'd' * 64, 'exited')
    NodeIngest(fixture.sessions).ingest(fixture.claim.session_id, fixture.claim.credential, encode_node_message(
        NodeEventV2(fixture.grants['app_effect_broker'].producer, uuid4(), 2, 1300, (exited,))))
    assert NodeRuntimeReconciler(fixture.sessions, coordinator).advance() == 1
    fixture.report(command, 'stopped', 2)
    assert _committed(coordinator, player_id, 1) == set()  # each bound Output interrupted
    after_bindings, after_frames, losses = _bound_state(registry)
    assert after_bindings == bindings and after_frames == frames  # calibration and bindings kept
    assert [(row['frame_id'], row['authority_epoch']) for row in losses] == [('node-f0', 1), ('node-f1', 1)]
    assert coordinator.runtime.read().export_state() == runtime  # the Run continues
    registry.clock.advance(20)  # the switch takes time; the Run advances meanwhile
    # The new app enrolls (a new authority epoch, as on a reboot) and reports readiness.
    player, _, _ = enroll(registry, fixture.key, device_id=DEVICE_ID)
    assert player['player_id'] == player_id and player['authority_epoch'] == 2
    coordinator.advance()
    coordinator.readiness(player_id, report(coordinator, player))
    assert _committed(coordinator, player_id, 2) == both  # rejoined
    # At the Run's current point: Runtime only advanced its clock; no Run restarted or ended.
    rejoined = coordinator.runtime.read().export_state()
    assert rejoined.pop('now') > runtime['now'] and rejoined == {k: v for k, v in runtime.items() if k != 'now'}
    after_bindings, after_frames, _ = _bound_state(registry)
    assert after_bindings == bindings and after_frames == frames
    fixture.report(command, 'starting_new', 3)
    fixture.report(command, 'running', 4)
    assert fixture.states() == ['target_running']


def test_a_bound_switch_whose_new_app_enrolls_before_the_exit_is_reconciled_rejoins_with_nothing_stale(registry):
    """D16 (G6), the other ordering: the new app enrolls (epoch 2) before the reconciler reads
    the old app's exit. Stated costs, pinned here: no interruption fact is ever recorded in this
    order (`node_output_losses` stays empty); the exit's work item stays `awaiting_output_link`,
    re-queued, until the new app links, then finishes as `before_process_link`, so the loss is
    dropped for good. Nothing of epoch 1 stays deliverable, bindings and calibration are kept, and
    epoch 2 rejoins the Run at its current point."""
    fixture = Rig(registry, unbound=False)
    coordinator = fixture.coordinator
    player_id = fixture.proof.challenge.player_id
    bindings, frames, _ = _bound_state(registry)
    both = {('node-f0', 'HDMI-A-1'), ('node-f1', 'HDMI-A-2')}
    assert _committed(coordinator, player_id, 1) == both
    runtime = coordinator.runtime.read().export_state()
    command = fixture.stage()
    fixture.report(command, 'intent_stop', 1)
    exited = AppProcessFact(fixture.proof.challenge.process, 42, 'd' * 64, 'exited')
    exit_evidence = uuid4()
    NodeIngest(fixture.sessions).ingest(fixture.claim.session_id, fixture.claim.credential, encode_node_message(
        NodeEventV2(fixture.grants['app_effect_broker'].producer, exit_evidence, 2, 1300, (exited,))))
    player, _, _ = enroll(registry, fixture.key, device_id=DEVICE_ID)  # before the reconciler runs
    assert player['authority_epoch'] == 2
    reconciler = NodeRuntimeReconciler(fixture.sessions, coordinator)
    for _ in range(3):  # each retry after its 5 s back-off
        reconciler.advance()
        registry.clock.advance(6)
    with registry.db.transaction() as conn:
        work = conn.execute('SELECT completed_at,result FROM node_reconciliation_work WHERE evidence_id=%s',
                            (exit_evidence,)).fetchone()
    assert dict(work) == {'completed_at': None, 'result': 'awaiting_output_link'}
    assert _bound_state(registry)[2] == []  # no interruption fact in this order
    with pytest.raises(CoordinationError, match='stale_authority'):  # epoch 1 is no longer deliverable at all
        coordinator.delivery(player_id, 1)
    after_bindings, after_frames, _ = _bound_state(registry)
    assert after_bindings == bindings and after_frames == frames
    registry.clock.advance(20)
    coordinator.advance()
    coordinator.readiness(player_id, report(coordinator, player))
    assert _committed(coordinator, player_id, 2) == both  # rejoined
    rejoined = coordinator.runtime.read().export_state()
    assert rejoined.pop('now') > runtime['now'] and rejoined == {k: v for k, v in runtime.items() if k != 'now'}
    # The new app links (its sample after the exit's), and the reconciler finishes the exit's item.
    _link_new_app(fixture, player, command, sampled_boottime_ms=1400)
    reconciler.advance()
    with registry.db.transaction() as conn:
        work = conn.execute('SELECT completed_at,result FROM node_reconciliation_work WHERE evidence_id=%s',
                            (exit_evidence,)).fetchone()
    assert work['completed_at'] is not None and work['result'] == 'before_process_link'
    assert _bound_state(registry)[2] == []  # the loss is never recorded


def _link_new_app(fixture, player, command, *, sampled_boottime_ms):
    """The re-enrolled Player app applies its epoch's control delivery and the broker links it."""
    registry, player_id, epoch = fixture.registry, player['player_id'], player['authority_epoch']
    registry.control_hello(player_id, ControlHello(authority_epoch=epoch, schemas=(2,), capabilities=()))
    delivery = registry.issue_control_delivery_record(player_id, epoch, 'c' * 64)
    receipt = registry.control_ack_response(player_id, ControlAck(
        authority_epoch=epoch, delivery_id=delivery['delivery_id'], result='applied')).receipt
    broker = fixture.grants['app_effect_broker']
    challenge = NodeAppLinkChallengeV2(broker.producer, broker.session_id, NodeProcessIdentity(456, 30, uuid4()),
        command.old_app_epoch + 1, command.target.environment_sha256, player_id, epoch,
        json.dumps(receipt.model_dump(mode='json', by_alias=True), sort_keys=True, separators=(',', ':')),
        'f' * 64, sampled_boottime_ms)
    proof = NodeAppLinkV2(challenge, fixture.key.public_key().public_bytes_raw().hex(),
                          fixture.key.sign(node_app_link_message(challenge)).hex())
    NodeAppLinks(fixture.sessions).admit(fixture.claim.session_id, fixture.claim.credential,
                                         encode_node_app_link(proof))
