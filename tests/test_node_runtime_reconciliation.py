"""Accepted base process linkage drives only exact Output execution consequences."""
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from support.position import PORTRAIT_ON_WIDE, save_position
from test_coordination import report
from test_fleet_attempts import BOOT_ID, DEVICE_ID, SERIAL
from test_registry import enroll, frame

from central.coordination import Coordinator
from central.fleet.node_app_links import NodeAppLinks
from central.fleet.node_ingest import NodeIngest
from central.fleet.node_sessions import NodeControlConfig, NodeControlError, NodeSessions
from central.node_runtime_reconciliation import NodeRuntimeReconciler
from central.runtime import Contribution, Scene
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


def rig(registry):
    from test_node_boot import cold_setup

    from contracts.node_boot import NodeBootRequestV2
    boots, _, _ = cold_setup(registry)
    offer_id = boots.offer(NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64)).offer_id
    player, key, _ = enroll(registry, device_id=DEVICE_ID)
    for index in range(2):
        frame(registry, f"node-f{index}")
        registry.bind(f"node-f{index}", player["player_id"], f"HDMI-A-{index+1}", expected_generation=0, by="console")
        save_position(registry, f"node-f{index}", PORTRAIT_ON_WIDE)
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.runtime.command("set_scene", Scene(scene_id="node-scene", loop=True, cycle_seconds=60,
        contributions=(Contribution(target="frame:node-f0", kind="black"),
                       Contribution(target="frame:node-f1", kind="black"),
                       Contribution(target="actuator:lamp", kind="actuator", ramp_to=1))), by="console")
    coordinator.runtime.command("activate", "node-scene", "node-run", 990, by="console")
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
                                 uuid4().hex + uuid4().hex)
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
    reconciler = NodeRuntimeReconciler(sessions)
    reconciler.advance()
    return coordinator, player, key, sessions, claims, grants, surfaces, reconciler, proof


def commit_assignments(coordinator, player):
    return {assignment for commit in coordinator.delivery(player["player_id"], 1)["commits"]
            for assignment in commit.assignment_ids}


def test_old_evidence_finishes_as_display_line_and_never_fences(registry):
    """Decision 0034, S4b part 2: an Output's loss is judged from the display line alone
    (tests/test_output_losses.py). A reboot initiation, a surface invalidated and the Player's exit
    on the old evidence path each finish as `display_line`: no loss, every commit kept, the Run
    unchanged."""
    coordinator, player, _, sessions, claims, grants, surfaces, reconciler, proof = rig(registry)
    before = coordinator.runtime.read().export_state()
    initial = commit_assignments(coordinator, player)
    assert len(initial) == 2
    host, display, broker = grants["host_core"], grants["display_host"], grants["app_effect_broker"]
    ingest = NodeIngest(sessions)
    ingest.ingest(host.session_id, claims["host_core"].credential, encode_node_message(
        NodeEventV2(host.producer, uuid4(), 1, 1200, (RebootFact("unknown", "base_observer"),))))
    ingest.ingest(display.session_id, claims["display_host"].credential, encode_node_message(
        NodeEventV2(display.producer, uuid4(), 3, 1300, (replace(surfaces[0], state="invalidated", buffer_id=None),))))
    ingest.ingest(broker.session_id, claims["app_effect_broker"].credential, encode_node_message(
        NodeEventV2(broker.producer, uuid4(), 2, 1300, (AppProcessFact(proof.challenge.process, 42, "d" * 64, "exited"),))))
    assert reconciler.advance() == 3
    assert reconciler.advance() == 0
    assert commit_assignments(coordinator, player) == initial
    assert coordinator.runtime.read().export_state() == before
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM output_losses").fetchone()["n"] == 0
        results = {row["result"] for row in conn.execute(
            "SELECT result FROM node_reconciliation_work WHERE completed_at IS NOT NULL").fetchall()}
    assert results == {"display_line"}


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


def test_display_admission_is_exact_and_never_releases_runtime(registry):
    from central.transaction_locks import acquire_node_lock
    coordinator, player, _, _, _, _, surfaces, _, proof = rig(registry)
    surface = surfaces[0]
    arguments = dict(device_id=DEVICE_ID, player_id=player["player_id"], authority_epoch=1,
        output_id=surface.output.output_id, frame_id=surface.frame_id, binding_generation=surface.binding_generation,
        config_revision=surface.config_revision, process=surface.process,
        app_epoch=surface.app_epoch, environment_sha256=proof.challenge.environment_sha256,
        phase="candidate")
    before = coordinator.runtime.read().export_state()
    with registry.db.transaction() as conn:
        with pytest.raises(ValueError, match="node_lock_required"):
            coordinator.display_admission_in(conn, **arguments)
        acquire_node_lock(conn, DEVICE_ID)
        admitted = coordinator.display_admission_in(conn, **arguments)
        assert admitted.allowed and admitted.fence
        assert not coordinator.display_admission_in(conn, **{**arguments,
            "process": replace(surface.process, start_ticks=surface.process.start_ticks+1)}).allowed
        assert not coordinator.display_admission_in(conn, **{**arguments,
            "config_revision": surface.config_revision+1}).allowed
    assert coordinator.runtime.read().export_state() == before
