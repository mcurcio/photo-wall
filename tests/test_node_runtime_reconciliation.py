"""Accepted base process linkage drives only exact Output execution consequences."""
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from test_coordination import report
from test_fleet_attempts import BOOT_ID, DEVICE_ID, OFFER_ID, SERIAL, _seed
from test_registry import enroll, frame

from central.coordination import Coordinator
from central.fleet.node_app_links import NodeAppLinks
from central.fleet.node_ingest import NodeIngest
from central.fleet.node_sessions import NodeControlConfig, NodeControlError, NodeSessions
from central.node_runtime_reconciliation import NodeRuntimeReconciler
from central.runtime import Contribution, Scene
from contracts.models import Calibration
from contracts.node_app_link import (
    NodeAppLinkChallengeV2,
    NodeAppLinkV2,
    encode_node_app_link,
    node_app_link_message,
)
from contracts.node_commands import NodeSessionClaim
from contracts.node_protocol import (
    AppProcessFact,
    NodeEventV2,
    NodeProcessIdentity,
    OutputKey,
    RebootFact,
    SurfaceFact,
    encode_node_message,
)
from contracts.player_control import ControlAck, ControlHello


def rig(registry, *, node_v2=False):
    offer_id = OFFER_ID
    if node_v2:
        from test_node_boot import cold_setup

        from contracts.node_boot import NodeBootRequestV2
        boots, _, _ = cold_setup(registry)
        offer_id = boots.offer(NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64)).offer_id
    else:
        _seed(registry)
    player, key, _ = enroll(registry, device_id=DEVICE_ID)
    for index in range(2):
        frame(registry, f"node-f{index}")
        registry.bind(f"node-f{index}", player["player_id"], f"HDMI-A-{index+1}", expected_generation=0)
        registry.calibrate(f"node-f{index}", "commit", 1, Calibration(), expected_generation=1)
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.runtime.command("set_scene", Scene(scene_id="node-scene", loop=True, cycle_seconds=60,
        contributions=(Contribution(target="frame:node-f0", kind="black"),
                       Contribution(target="frame:node-f1", kind="black"),
                       Contribution(target="actuator:lamp", kind="actuator", ramp_to=1))))
    coordinator.runtime.command("activate", "node-scene", "node-run", 990)
    coordinator.advance()
    coordinator.readiness(player["player_id"], report(coordinator, player))
    registry.control_hello(player["player_id"], ControlHello(authority_epoch=1, schemas=(2,), capabilities=()))
    delivery = registry.issue_control_delivery_record(player["player_id"], 1, "c" * 64)
    receipt = registry.control_ack_response(player["player_id"], ControlAck(
        authority_epoch=1, delivery_id=delivery["delivery_id"], result="applied")).receipt
    sessions = NodeSessions(registry.db, registry.clock, NodeControlConfig("node-test"))
    claims, grants = {}, {}
    for owner in ("host_core", "app_effect_broker", "display_host"):
        claim = NodeSessionClaim(SERIAL, offer_id, BOOT_ID, owner, uuid4(), uuid4(),
                                 uuid4().hex + uuid4().hex, 1000)
        claims[owner], grants[owner] = claim, sessions.enroll(claim)
    process = NodeProcessIdentity(123, 10, uuid4())
    broker = grants["app_effect_broker"]
    challenge = NodeAppLinkChallengeV2(broker.producer, broker.session_id, process, 42, "d" * 64,
        player["player_id"], 1, json.dumps(receipt.model_dump(mode="json", by_alias=True),
                                         sort_keys=True, separators=(",", ":")), "e" * 64, 500)
    proof = NodeAppLinkV2(challenge, key.public_key().public_bytes_raw().hex(),
                          key.sign(node_app_link_message(challenge)).hex())
    links = NodeAppLinks(sessions)
    links.admit(broker.session_id, claims["app_effect_broker"].credential, encode_node_app_link(proof))
    ingest = NodeIngest(sessions)
    ingest.ingest(broker.session_id, claims["app_effect_broker"].credential, encode_node_message(
        NodeEventV2(broker.producer, uuid4(), 1, 1000,
                    (AppProcessFact(process, 42, "d" * 64, "running"),))))
    display = grants["display_host"]
    surfaces = []
    for index, binding in enumerate(coordinator.configuration(player["player_id"], 1).bindings):
        surface = SurfaceFact(OutputKey(BOOT_ID, display.producer.incarnation_id, binding.output_id, 1, 1),
                              process, 42, binding.generation, binding.configuration_revision,
                              "presented_to_compositor", f"buffer-{index}", frame_id=binding.frame_id)
        surfaces.append(surface)
        ingest.ingest(display.session_id, claims["display_host"].credential, encode_node_message(
            NodeEventV2(display.producer, uuid4(), index + 1, 1100, (surface,))))
    reconciler = NodeRuntimeReconciler(sessions, coordinator)
    reconciler.advance()
    return coordinator, player, key, sessions, claims, grants, surfaces, reconciler, proof


def commit_assignments(coordinator, player):
    return {assignment for commit in coordinator.delivery(player["player_id"], 1)["commits"]
            for assignment in commit.assignment_ids}


def test_initiation_never_interrupts_and_output_loss_preserves_other_frame_run_actuator(registry):
    coordinator, player, _, sessions, claims, grants, surfaces, reconciler, _ = rig(registry)
    before = coordinator.runtime.read().export_state()
    initial = commit_assignments(coordinator, player)
    assert len(initial) == 2
    host = grants["host_core"]
    NodeIngest(sessions).ingest(host.session_id, claims["host_core"].credential, encode_node_message(
        NodeEventV2(host.producer, uuid4(), 1, 1200, (RebootFact("unknown", "base_observer"),))))
    reconciler.advance()
    assert commit_assignments(coordinator, player) == initial
    display = grants["display_host"]
    event = NodeEventV2(display.producer, uuid4(), 3, 1300,
                        (replace(surfaces[0], state="invalidated", buffer_id=None),))
    NodeIngest(sessions).ingest(display.session_id, claims["display_host"].credential, encode_node_message(event))
    assert reconciler.advance() == 1
    remaining = commit_assignments(coordinator, player)
    assert len(remaining) == 1
    plan = coordinator.delivery(player["player_id"], 1)["plan"]
    assert {layer.output_id for layer in plan.layers if layer.assignment_id in remaining} == {surfaces[1].output.output_id}
    assert coordinator.runtime.read().export_state() == before
    assert reconciler.advance() == 0
    coordinator.readiness(player["player_id"], report(coordinator, player, sequence=2))
    assert commit_assignments(coordinator, player) == remaining
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM node_output_losses WHERE resolved_at IS NULL").fetchone()["n"] == 1


def test_process_exit_reconciles_both_linked_outputs_without_cancelling_run(registry):
    coordinator, player, _, sessions, claims, grants, _, reconciler, proof = rig(registry)
    before = coordinator.runtime.read().export_state()
    broker = grants["app_effect_broker"]
    exit_event = NodeEventV2(broker.producer, uuid4(), 2, 1300,
        (AppProcessFact(proof.challenge.process, 42, "d" * 64, "exited"),))
    NodeIngest(sessions).ingest(broker.session_id, claims["app_effect_broker"].credential,
                               encode_node_message(exit_event))
    reconciler.advance()
    assert commit_assignments(coordinator, player) == set()
    assert coordinator.runtime.read().export_state() == before


@pytest.mark.parametrize("replacement", ["epoch", "binding"])
def test_old_loss_does_not_target_replacement_authority_or_binding(registry, replacement):
    coordinator, player, key, sessions, claims, grants, surfaces, reconciler, _ = rig(registry)
    display = grants["display_host"]
    event = NodeEventV2(display.producer, uuid4(), 3, 1300,
                        (replace(surfaces[0], state="invalidated", buffer_id=None),))
    NodeIngest(sessions).ingest(display.session_id, claims["display_host"].credential, encode_node_message(event))
    if replacement == "epoch":
        enroll(registry, key, device_id=DEVICE_ID)
    else:
        registry.unbind("node-f0", expected_generation=1)
    reconciler.advance()
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM node_output_losses").fetchone()["n"] == 0


def test_link_replay_exact_but_modified_proof_and_new_control_rejected(registry):
    _, _, _, sessions, claims, grants, _, _, proof = rig(registry)
    broker = grants["app_effect_broker"]
    links = NodeAppLinks(sessions)
    assert links.admit(broker.session_id, claims["app_effect_broker"].credential,
                       encode_node_app_link(proof))["duplicate"]
    with pytest.raises(NodeControlError, match="identity_conflict"):
        links.admit(broker.session_id, claims["app_effect_broker"].credential,
                    encode_node_app_link(replace(proof, signature="0" * 128)))
    registry.issue_control_delivery_record(proof.challenge.player_id, 1, "f" * 64)
    changed = replace(proof, challenge=replace(proof.challenge, nonce="f" * 64))
    with pytest.raises(NodeControlError, match="control_not_current"):
        links.admit(broker.session_id, claims["app_effect_broker"].credential, encode_node_app_link(changed))


def test_display_admission_is_exact_and_never_releases_runtime_or_active_drain(registry):
    from central.transaction_locks import acquire_runtime_locks
    coordinator, player, _, _, _, _, surfaces, _, proof = rig(registry)
    surface = surfaces[0]
    arguments = dict(player_id=player["player_id"], authority_epoch=1,
        output_id=surface.output.output_id, frame_id=surface.frame_id, binding_generation=surface.binding_generation,
        config_revision=surface.config_revision, process=surface.process,
        app_epoch=surface.app_epoch, environment_sha256=proof.challenge.environment_sha256,
        phase="candidate")
    before = coordinator.runtime.read().export_state()
    with registry.db.transaction() as conn:
        with pytest.raises(ValueError, match="runtime_locks_required"):
            coordinator.display_admission_in(conn, **arguments)
        acquire_runtime_locks(conn)
        admitted = coordinator.display_admission_in(conn, **arguments)
        assert admitted.allowed and admitted.fence
        assert not coordinator.display_admission_in(conn, **{**arguments,
            "process": replace(surface.process, start_ticks=surface.process.start_ticks+1)}).allowed
        assert not coordinator.display_admission_in(conn, **{**arguments,
            "config_revision": surface.config_revision+1}).allowed
        conn.execute("INSERT INTO equipment_drains(player_id,attempt_id,boot_id,authority_epoch,phase,"
                     "prepared_at,authorization_expires_at,snapshot) VALUES(%s,'test','test',1,'prepared',1000,1100,jsonb_build_object('outputs',jsonb_build_array()))",
                     (player["player_id"],))
        denied = coordinator.display_admission_in(conn, **arguments)
        assert not denied.allowed and denied.reason == "equipment_drain_requires_v2_target_reservation"
    assert coordinator.runtime.read().export_state() == before


def test_first_compositor_frame_cannot_release_output_loss(registry):
    coordinator, player, _, sessions, claims, grants, surfaces, reconciler, _ = rig(registry)
    display = grants["display_host"]
    ingest = NodeIngest(sessions)
    for sequence, state in ((3, "invalidated"), (4, "presented_to_compositor")):
        fact = replace(surfaces[0], state=state, buffer_id="recovery-candidate" if sequence==4 else None)
        ingest.ingest(display.session_id, claims["display_host"].credential, encode_node_message(
            NodeEventV2(display.producer, uuid4(), sequence, 1300 + sequence, (fact,))))
        reconciler.advance()
    coordinator.readiness(player["player_id"], report(coordinator, player, sequence=2))
    assert len(commit_assignments(coordinator, player)) == 1


def test_equal_counter_frame_rebind_preserves_historical_loss_without_fencing_replacement(registry):
    from central.transaction_locks import acquire_runtime_locks
    from contracts.node_display import Surface

    coordinator, player, _, sessions, claims, grants, surfaces, reconciler, _ = rig(registry)
    old = surfaces[0]
    display = grants["display_host"]
    event = NodeEventV2(display.producer, uuid4(), 3, 1300,
        (replace(old, state="invalidated", buffer_id=None),))
    NodeIngest(sessions).ingest(display.session_id, claims["display_host"].credential,
                                encode_node_message(event))
    reconciler.advance()
    assert len(commit_assignments(coordinator, player)) == 1
    registry.unbind(old.frame_id, expected_generation=old.binding_generation)
    frame(registry, "replacement-frame")
    registry.bind("replacement-frame", player["player_id"], old.output.output_id, expected_generation=0)
    binding = next(b for b in coordinator.configuration(player["player_id"], 1).bindings
                   if b.frame_id == "replacement-frame")
    # The Registry owner port fixtures commissioning; no synthetic Trial witness.
    with registry.db.transaction() as conn:
        acquire_runtime_locks(conn)
        registry.commit_calibration_trial_in(conn, frame_id=binding.frame_id,
            player_id=player["player_id"], authority_epoch=1, output_id=binding.output_id,
            binding_generation=binding.generation, config_revision=binding.configuration_revision,
            calibration_revision=binding.calibration.revision, calibration=Calibration())
    binding = next(b for b in coordinator.configuration(player["player_id"], 1).bindings
                   if b.frame_id == "replacement-frame")
    assert (binding.generation, binding.configuration_revision) == (old.binding_generation, old.config_revision)
    previous = Surface(old.output, old.process, old.app_epoch, old.binding_generation, old.config_revision, old.frame_id)
    expected = replace(previous, frame_id=binding.frame_id)
    with registry.db.transaction() as conn:
        acquire_runtime_locks(conn)
        assert coordinator.display_withdrawal_in(conn, player_id=player["player_id"], authority_epoch=1,
            previous_surface=previous, expected_surface=expected).allowed
        assert not coordinator.display_withdrawal_in(conn, player_id=player["player_id"], authority_epoch=1,
            previous_surface=expected, expected_surface=expected).allowed
        args = dict(player_id=player["player_id"], authority_epoch=1, output_id=old.output.output_id,
            binding_generation=old.binding_generation, configuration_revision=old.config_revision,
            producer_id=display.producer.incarnation_id, evidence_id=event.event_id,
            evidence_kind="event", detail={})
        # Neither old Frame evidence nor missing historical identity may clear an
        # unresolved loss. No wrong-frame insertion occurs either.
        for frame_id in (old.frame_id, None):
            for recovering in (True, False):
                assert not coordinator.reconcile_node_output_in(conn, **args,
                    frame_id=frame_id, recovering=recovering)
        assert not coordinator.reconcile_node_output_in(conn, **args,
            frame_id=binding.frame_id, recovering=True)
        rows = conn.execute("SELECT frame_id,resolved_at FROM node_output_losses").fetchall()
        assert rows == [{"frame_id": old.frame_id, "resolved_at": None}]
    coordinator.runtime.command("set_scene", Scene(scene_id="replacement-scene", loop=True, cycle_seconds=60,
        contributions=(Contribution(target="frame:replacement-frame", kind="black"),)))
    coordinator.runtime.command("activate", "replacement-scene", "replacement-run", 1000)
    coordinator.advance()
    coordinator.readiness(player["player_id"], report(coordinator, player, sequence=2))
    delivery = coordinator.delivery(player["player_id"], 1)
    committed = commit_assignments(coordinator, player)
    assert any(layer.frame_id == binding.frame_id and layer.assignment_id in committed
               for layer in delivery["plan"].layers)
