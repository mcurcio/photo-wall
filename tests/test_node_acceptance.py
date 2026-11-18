"""Sustained qualification through Registry/Runtime/link/Display owners, no pixels claim."""
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from test_coordination import publish_fixture_catalog, report
from test_fleet_attempts import DEVICE_ID
from test_node_runtime_reconciliation import rig

from central.fleet.node_acceptance import NodeAcceptance
from central.fleet.node_app_links import NodeAppLinks
from central.fleet.node_display import NodeDisplay
from central.fleet.node_sessions import NodeControlError
from central.runtime import Contribution, Scene
from contracts.liveness import HEARD_WRITE_SECONDS
from contracts.node_app_link import NodeAppLinkV2, encode_node_app_link, node_app_link_message
from contracts.node_display import (
    DisplayExchange,
    DisplayReceipt,
    Surface,
    encode_display_exchange,
    parse_display_decision,
)
from contracts.node_frame import frame_witness_tag
from contracts.player_control import ControlAck


def fresh_proof(registry, service, claim, old_proof, key, *, sampled=35001, digest=None):
    registry.clock.advance(1)
    delivery = registry.issue_control_delivery_record(old_proof.challenge.player_id, 1,
                                                      digest or uuid4().hex*2)
    receipt = registry.control_ack_response(old_proof.challenge.player_id,
        ControlAck(authority_epoch=1, delivery_id=delivery['delivery_id'], result='applied')).receipt
    challenge = replace(old_proof.challenge, control_receipt=json.dumps(receipt.model_dump(mode='json', by_alias=True),
        sort_keys=True, separators=(',', ':')), nonce=uuid4().hex*2, sampled_boottime_ms=sampled)
    proof = NodeAppLinkV2(challenge, old_proof.public_key, key.sign(node_app_link_message(challenge)).hex())
    NodeAppLinks(service.sessions).admit(claim.session_id, claim.credential, encode_node_app_link(proof))
    return proof


class Witnesses:
    def __init__(self, registry):
        self.registry = registry
        (self.coordinator,self.player,self.key,self.sessions,self.claims,self.grants,
         facts,_,self.proof) = rig(registry)
        self.display = NodeDisplay(self.sessions,runtime=self.coordinator)
        self.claim = self.claims['display_host']
        self.grant = self.grants['display_host']
        self.requests = {}
        self.sequence = 1
        for fact in facts:
            surface = Surface(fact.output,fact.process,fact.app_epoch,fact.binding_generation,fact.config_revision,fact.frame_id)
            request = DisplayExchange(self.grant.producer,uuid4(),1000,fact.output,True)
            assert self.send(request).operation == 'candidate'
            receipt = DisplayReceipt(surface,uuid4(),'first-buffer','synthetic',1001)
            request = replace(request,request_id=uuid4(),sampled_boottime_ms=1001,candidate=surface,receipt=receipt)
            handoff = self.send(request)
            assert handoff.operation == 'handoff'
            request = replace(request,request_id=uuid4(),sampled_boottime_ms=1002,admitted=surface,
                receipt=replace(receipt,buffer_id='admitted-buffer',sampled_boottime_ms=1002),
                completed_decision_id=handoff.decision_id,completed_boottime_ms=1002)
            self.send(request)
            self.requests[fact.output.output_id] = request
        for run in self.coordinator.runtime.read().project(registry.clock.utc()).runs:
            self.coordinator.runtime.command('cancel',run.run_id,registry.clock.utc(), by="console")
        # Two Frames, so two photos: one photo is never on two Frames at once (roadmap 5a).
        publish_fixture_catalog(registry,also=(('original-a2','c'*64),))
        self.coordinator.runtime.command('set_scene',Scene(scene_id='representative',loop=True,cycle_seconds=300,
            contributions=tuple(Contribution(target='frame:node-f'+str(i),kind='media',source_refs=('library:1',)) for i in range(2))), by="console")
        self.coordinator.runtime.command('activate','representative','representative-start',registry.clock.utc(), by="console")
        self.coordinator.advance()
        self.sequence = 2
        self.coordinator.readiness(self.player["player_id"],report(self.coordinator,self.player,sequence=self.sequence))
        self.registry.authenticate(self.player['token'])   # the Player's poll: Central heard it
        self.acceptance = NodeAcceptance(self.sessions)
        self.qualification = uuid4()
        self.acceptance.begin(DEVICE_ID,self.qualification,self.proof.challenge.environment_sha256,'operator:representative-media')

    def send(self, request):
        return parse_display_decision(self.display.exchange(self.claim.session_id,self.claim.credential,
                                                           encode_display_exchange(request)))

    def advance(self, *, interval=5, tag_override=None, digest=None, ready=True, heard=True):
        """`ready`: the Player posts readiness (it does only on a change); `heard`: it polls."""
        self.registry.clock.advance(interval-1)
        self.sequence += 1
        # A changed state issues a new applied receipt; an unchanged one (`digest`) replays it.
        self.proof = fresh_proof(self.registry,self,self.claims['app_effect_broker'],self.proof,self.key,
                                sampled=int((self.registry.clock.utc()-1000)*1000)+2000, digest=digest)
        self.coordinator.advance()
        if ready:
            self.coordinator.readiness(self.player['player_id'],report(self.coordinator,self.player,sequence=self.sequence))
        if heard:
            self.registry.authenticate(self.player['token'])   # the Player's poll: Central heard it
        plan = self.coordinator.delivery(self.player['player_id'],1)['plan']
        now_ms = int((self.registry.clock.utc()-1000)*1000)+1000
        for binding in plan.bindings:
            layers = [x for x in plan.layers if x.output_id==binding.output_id and x.start<=self.registry.clock.utc()<x.end]
            assert len(layers)==1 and layers[0].presentation=='media'
            tag = frame_witness_tag(output_id=binding.output_id, frame_id=binding.frame_id,binding_generation=binding.generation,
                config_revision=binding.configuration_revision,calibration=binding.calibration.model_dump(mode='json'),
                layers=({'assignment_id':layers[0].assignment_id,'variant_sha256':layers[0].variant.sha256},))
            old = self.requests[binding.output_id]
            request = replace(old,request_id=uuid4(),sampled_boottime_ms=now_ms,
                receipt=replace(old.receipt,buffer_id='normal-'+str(self.sequence),sampled_boottime_ms=now_ms,
                                frame_tag=tag_override or tag))
            self.send(request)
            self.requests[binding.output_id] = request
        return self.acceptance.sample(self.qualification)

    def change_mode(self):
        old = next(iter(self.requests.values()))
        output = replace(old.output,mode_generation=old.output.mode_generation+1)
        surface = replace(old.admitted,output=output)
        now_ms = int((self.registry.clock.utc()-1000)*1000)+1001
        request = DisplayExchange(self.grant.producer,uuid4(),now_ms,output,True)
        assert self.send(request).operation=='candidate'
        receipt = DisplayReceipt(surface,uuid4(),'mode-candidate','synthetic',now_ms+1)
        request = replace(request,request_id=uuid4(),sampled_boottime_ms=now_ms+1,candidate=surface,receipt=receipt)
        handoff = self.send(request)
        assert handoff.operation=='handoff'
        request = replace(request,request_id=uuid4(),sampled_boottime_ms=now_ms+2,admitted=surface,
            receipt=replace(receipt,buffer_id='mode-admitted',sampled_boottime_ms=now_ms+2),
            completed_decision_id=handoff.decision_id,completed_boottime_ms=now_ms+2)
        self.send(request)
        self.requests[output.output_id]=request

    def replace_media(self):
        for run in self.coordinator.runtime.read().project(self.registry.clock.utc()).runs:
            self.coordinator.runtime.command('cancel',run.run_id,self.registry.clock.utc(), by="console")
        publish_fixture_catalog(self.registry,digest='b'*64,asset='new-representative',
                                also=(('new-representative-2','d'*64),))
        self.coordinator.runtime.command('activate','representative','replacement-start',self.registry.clock.utc(), by="console")
        self.coordinator.advance()
        self.sequence += 1
        self.coordinator.readiness(self.player['player_id'],report(self.coordinator,self.player,sequence=self.sequence))
        self.registry.authenticate(self.player['token'])   # the Player's poll: Central heard it


def assert_qualified_evidence(registry, witness):
    with registry.db.transaction() as conn:
        row = conn.execute('SELECT evidence FROM node_environment_acceptances').fetchone()
        assert row['evidence']['physical_pixels']=='unknown'
        assert len(row['evidence']['sample_ids'])==7
    # G4: the operator read lists the qualified environment (the gate plays no part in a read).
    from central.fleet.node_lifecycle import NodeLifecycle
    [listed] = NodeLifecycle(witness.sessions).status(DEVICE_ID)['qualification']['acceptances']
    assert listed['environment_sha256']==witness.proof.challenge.environment_sha256 and listed['base_tag'] is not None


def test_advancing_real_owner_witnesses_qualify_without_cached_repeats(registry):
    witness = Witnesses(registry)
    result = witness.advance()
    assert result['status']=='observing'
    assert witness.acceptance.sample(witness.qualification)['status']=='awaiting_new_witnesses'
    for _ in range(6):
        result = witness.advance()
    assert result['status']=='accepted' and result['fleet_frontier_changed'] is False
    assert_qualified_evidence(registry, witness)


def test_an_unchanged_control_state_still_qualifies(registry):
    """Central re-sends the applied delivery while the state is unchanged, so the applied
    receipt keeps its sequence and age; Central hearing the Player carries liveness instead.
    Mutation probe: restore the applied-age check or the advancing control sequence."""
    witness = Witnesses(registry)
    for _ in range(7):
        result = witness.advance(digest='f'*64)
    assert result['status']=='accepted' and result['fleet_frontier_changed'] is False
    assert_qualified_evidence(registry, witness)


def test_readiness_sent_long_ago_qualifies_while_central_hears_the_player(registry):
    """Readiness is sent once per change, so an unchanged Player's readiness keeps its number and
    is as old as its last change: liveness is when Central last heard from it (its polls), never
    the readiness receipt's age nor an advancing readiness number. Mutations: gate on
    `player_show_state.readiness_received_at` -> readiness_stale; ask each sample for a newer readiness
    number -> awaiting_new_witnesses forever."""
    witness = Witnesses(registry)
    for _ in range(7):
        result = witness.advance(ready=False)
    assert result['status']=='accepted' and result['fleet_frontier_changed'] is False
    assert_qualified_evidence(registry, witness)


def test_a_player_central_has_not_heard_within_the_gap_is_readiness_stale(registry):
    """Fresh readiness does not make a Player live: unheard past MAX_GAP_SECONDS plus the
    heard write's HEARD_WRITE_SECONDS, the sample is refused. Mutation: drop the heard check,
    or widen it past the gap -> the sample is taken."""
    witness = Witnesses(registry)
    witness.advance()
    gap = NodeAcceptance.MAX_GAP_SECONDS + HEARD_WRITE_SECONDS
    with pytest.raises(NodeControlError, match='node_qualification_readiness_stale'):
        witness.advance(interval=int(gap) + 1, heard=False)


@pytest.mark.parametrize('tag',['synthetic','trial-not-normal-media'])
def test_ready_media_does_not_qualify_synthetic_or_trial_buffer(registry,tag):
    witness = Witnesses(registry)
    with pytest.raises(NodeControlError,match='frame_witness_mismatch'):
        witness.advance(tag_override=tag)
    with registry.db.transaction() as conn:
        assert conn.execute('SELECT count(*) n FROM node_environment_acceptances').fetchone()['n']==0


def test_gap_restarts_sustained_window(registry):
    witness = Witnesses(registry)
    witness.advance()
    witness.advance()
    result = witness.advance(interval=6)
    assert result['status']=='observing' and result['sustained_seconds']==0


@pytest.mark.parametrize('change',['mode','assignment'])
def test_changed_output_cohort_or_media_assignment_restarts_window(registry,change):
    witness = Witnesses(registry)
    for _ in range(3):
        witness.advance()
    if change=='mode':
        witness.change_mode()
    else:
        witness.replace_media()
    result = witness.advance()
    assert result['status']=='observing' and result['sustained_seconds']==0
    with registry.db.transaction() as conn:
        assert conn.execute('SELECT count(*) n FROM node_environment_acceptances').fetchone()['n']==0


def test_binding_change_cannot_keep_old_qualification_witness(registry):
    witness = Witnesses(registry)
    witness.advance()
    registry.unbind('node-f0',expected_generation=1, by="console")
    with pytest.raises(NodeControlError,match='plan_changed'):
        witness.acceptance.sample(witness.qualification)


def test_actual_qualification_selects_fallback_but_changed_mode_blocks_new_stage(registry):
    import json

    from test_node_boot import environment, publish_deployment, released

    from central.fleet.node_boot import parse_node_deployment
    from central.fleet.node_lifecycle import NodeLifecycle, OperatorAppStage
    from contracts.node_lifecycle import parse_stage_command
    witness=Witnesses(registry)
    for _ in range(7):
        result=witness.advance()
    assert result['status']=='accepted'
    with registry.db.transaction() as conn:
        deployment=parse_node_deployment(bytes(conn.execute('SELECT deployment_document FROM node_releases').fetchone()['deployment_document']))
    target=environment('b')
    selected=released(replace(deployment,app_environment=target,environment_sources={
        deployment.manager_primary.environment_sha256:deployment.environment_sources[deployment.manager_primary.environment_sha256],
        target.environment_sha256:'https://example.invalid/target'}))
    publish_deployment(registry.db,selected,registry.clock)
    registry.unbind('node-f0',expected_generation=1, by="console")
    registry.unbind('node-f1',expected_generation=1, by="console")
    lifecycle=NodeLifecycle(witness.sessions)
    claim=witness.claims['app_effect_broker']
    issued=lifecycle.stage(DEVICE_ID,OperatorAppStage(uuid4(),uuid4(),claim.session_id,1,selected.deployment_id,'operator:qualified'))
    command=parse_stage_command(json.dumps(issued['command']).encode())
    assert command.fallback==deployment.app_environment
    old=next(iter(witness.requests.values()))
    local_now=int((registry.clock.utc()-1000)*1000)+1000
    changed=DisplayExchange(witness.grant.producer,uuid4(),local_now+1,
                           replace(old.output,mode_generation=2),True)
    witness.send(changed)
    # The fallback is frozen at stage time; a changed output cohort refuses a new stage.
    with pytest.raises(NodeControlError,match='qualified_fallback_required'):
        lifecycle.stage(DEVICE_ID,OperatorAppStage(uuid4(),uuid4(),claim.session_id,1,selected.deployment_id,
                                                   'operator:after-mode-change'))


def test_an_output_whose_layer_awaits_no_grant_qualifies_without_a_commit(registry):
    """Decision 0032 (B12): a layer that awaits no grant (a later turn of its Run) is in force once
    prepared; qualification asks a Commit only of a layer that awaits one. Mutation probe:
    restore the Commit requirement."""
    witness = Witnesses(registry)
    witness.advance(interval=300)            # the Scene's second turn: no grant awaited
    for _ in range(6):
        result = witness.advance()
    plan = witness.coordinator.delivery(witness.player['player_id'], 1)['plan']
    now = registry.clock.utc()
    active = [layer for layer in plan.layers if layer.start <= now < layer.end]
    assert active and not any(layer.awaits_grant for layer in active)
    with registry.db.transaction() as conn:
        committed = {row['assignment_id'] for row in conn.execute(
            'SELECT assignment_id FROM execution_commits WHERE valid').fetchall()}
    assert not committed & {layer.assignment_id for layer in active}
    assert result['status'] == 'accepted'
