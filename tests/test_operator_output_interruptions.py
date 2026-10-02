"""The operator snapshot's interruption read (console DDD §16, bead C1).

Central serves only unresolved Output losses that fence a Frame's CURRENT Binding: the
Player's current authority epoch and the same Frame, Player, Output and binding generation
that Runtime fences on. `cause_layer` is the owner of the producer that reported the loss.
"""

from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_fleet_attempts import DEVICE_ID
from test_node_runtime_reconciliation import rig
from test_operator_frames import ADMIN, AUTH
from test_registry import enroll, frame

from central.app import create_app
from central.coordination import Coordinator
from central.fleet.node_ingest import NodeIngest
from contracts.node_protocol import AppProcessFact, NodeEventV2, encode_node_message


def _lose_first_output(sessions, claims, grants, surfaces, reconciler):
    """Display Host reports Output 0's surface invalidated; Central records the loss."""
    display = grants["display_host"]
    event = NodeEventV2(display.producer, uuid4(), 3, 1300,
                        (replace(surfaces[0], state="invalidated", buffer_id=None),))
    NodeIngest(sessions).ingest(display.session_id, claims["display_host"].credential,
                                encode_node_message(event))
    assert reconciler.advance() == 1


def _served(registry):
    with registry.db.transaction() as conn:
        return [row.model_dump(mode="json") for row in Coordinator.output_interruptions_in(conn)]


def _unresolved(registry):
    with registry.db.transaction() as conn:
        return conn.execute("SELECT count(*) AS n FROM node_output_losses "
                            "WHERE resolved_at IS NULL").fetchone()["n"]


def test_a_loss_on_the_current_binding_is_served_on_the_snapshot_with_its_cause_layer(registry):
    _, player, _, sessions, claims, grants, surfaces, reconciler, _ = rig(registry)
    _lose_first_output(sessions, claims, grants, surfaces, reconciler)
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    with TestClient(app) as client:
        response = client.get("/v1/operator/snapshot", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    rows = body["output_interruptions"]
    assert len(rows) == 1
    row = rows[0]
    assert {key: row[key] for key in ("frame_id", "player_id", "output_id", "binding_generation",
                                      "cause_layer")} == {
        "frame_id": surfaces[0].frame_id, "player_id": player["player_id"],
        "output_id": surfaces[0].output.output_id,
        "binding_generation": surfaces[0].binding_generation, "cause_layer": "display_host"}
    # Central's record time, on Central's clock, no later than the snapshot's read time.
    assert row["interrupted_at"] <= body["read_at"]
    # It agrees with the Binding beside it in the same snapshot.
    bound = next(f for f in body["inventory"]["frames"] if f["id"] == row["frame_id"])
    assert (bound["player_id"], bound["output_id"], bound["generation"]) == (
        row["player_id"], row["output_id"], row["binding_generation"])


def test_the_cause_layer_is_the_reporting_producers_owner(registry):
    _, _, _, sessions, claims, grants, _, reconciler, proof = rig(registry)
    broker = grants["app_effect_broker"]
    NodeIngest(sessions).ingest(broker.session_id, claims["app_effect_broker"].credential,
        encode_node_message(NodeEventV2(broker.producer, uuid4(), 2, 1300,
            (AppProcessFact(proof.challenge.process, 42, "d" * 64, "exited"),))))
    reconciler.advance()
    served = _served(registry)
    assert [(row["frame_id"], row["cause_layer"]) for row in served] == [
        ("node-f0", "app_effect_broker"), ("node-f1", "app_effect_broker")]


def test_a_loss_from_an_earlier_binding_generation_of_the_same_output_is_not_served(registry):
    _, player, _, sessions, claims, grants, surfaces, reconciler, _ = rig(registry)
    _lose_first_output(sessions, claims, grants, surfaces, reconciler)
    old = surfaces[0]
    registry.unbind(old.frame_id, expected_generation=old.binding_generation)
    registry.bind(old.frame_id, player["player_id"], old.output.output_id,
                  expected_generation=old.binding_generation + 1)
    assert _unresolved(registry) == 1  # the historical fence is kept; it is not served
    assert _served(registry) == []


def test_a_frame_newly_bound_to_that_output_inherits_nothing(registry):
    _, player, _, sessions, claims, grants, surfaces, reconciler, _ = rig(registry)
    _lose_first_output(sessions, claims, grants, surfaces, reconciler)
    old = surfaces[0]
    registry.unbind(old.frame_id, expected_generation=old.binding_generation)
    frame(registry, "replacement-frame")
    generation = registry.bind("replacement-frame", player["player_id"], old.output.output_id,
                               expected_generation=0)["generation"]
    # Same Player, Output and binding generation number; only the Frame differs.
    assert generation == old.binding_generation
    assert _unresolved(registry) == 1
    assert _served(registry) == []


def test_a_loss_from_an_earlier_authority_epoch_is_not_served(registry):
    _, _, key, sessions, claims, grants, surfaces, reconciler, _ = rig(registry)
    _lose_first_output(sessions, claims, grants, surfaces, reconciler)
    enroll(registry, key, device_id=DEVICE_ID)
    assert _unresolved(registry) == 1
    assert _served(registry) == []


@pytest.mark.parametrize("resolved", [False, True])
def test_only_an_unresolved_loss_is_served(registry, resolved):
    _, _, _, sessions, claims, grants, surfaces, reconciler, _ = rig(registry)
    _lose_first_output(sessions, claims, grants, surfaces, reconciler)
    if resolved:
        with registry.db.transaction() as conn:
            conn.execute("UPDATE node_output_losses SET resolved_at=interrupted_at+1")
    assert len(_served(registry)) == (0 if resolved else 1)


def test_no_loss_serves_an_empty_list(registry):
    rig(registry)
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    with TestClient(app) as client:
        assert client.get("/v1/operator/snapshot", headers=AUTH).json()["output_interruptions"] == []
