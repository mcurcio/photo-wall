"""The operator snapshot's interruption read (console DDD §16, bead C1).

Central serves only unresolved Output losses that fence a Frame's CURRENT Binding: the
Player's current authority epoch and the same Frame, Player, Output and binding generation
that Runtime fences on. A loss is judged from the Node's display line (decision 0034, S4b part 2),
so its `cause_layer` is 'display_host'.
"""

import pytest
from fastapi.testclient import TestClient
from support.node_rig import rig
from support.output_report import report_modes
from test_fleet_attempts import DEVICE_ID
from test_operator_frames import ADMIN, AUTH
from test_registry import enroll, frame

from central.app import create_app
from central.coordination import Coordinator


def _lose_first_output(registry, surfaces):
    """The Node's display line reports Output 0 disconnected; Central records the loss."""
    report_modes(registry, DEVICE_ID, (surfaces[0].output.output_id,), None)


def _served(registry):
    with registry.db.transaction() as conn:
        return [row.model_dump(mode="json") for row in Coordinator.output_interruptions_in(conn)]


def _unresolved(registry):
    with registry.db.transaction() as conn:
        return conn.execute("SELECT count(*) AS n FROM output_losses "
                            "WHERE resolved_at IS NULL").fetchone()["n"]


def test_a_loss_on_the_current_binding_is_served_on_the_snapshot_with_its_cause_layer(registry):
    _, player, _, surfaces = rig(registry)
    _lose_first_output(registry, surfaces)
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
    # The display line's record time: the judge stamps it on the database's clock (as every write
    # of that line), never compared with the snapshot's read time on Central's.
    assert row["interrupted_at"] > 0
    # It agrees with the Binding beside it in the same snapshot.
    bound = next(f for f in body["inventory"]["frames"] if f["id"] == row["frame_id"])
    assert (bound["player_id"], bound["output_id"], bound["generation"]) == (
        row["player_id"], row["output_id"], row["binding_generation"])


def test_a_loss_from_an_earlier_binding_generation_of_the_same_output_is_not_served(registry):
    _, player, _, surfaces = rig(registry)
    _lose_first_output(registry, surfaces)
    old = surfaces[0]
    registry.unbind(old.frame_id, expected_generation=old.binding_generation, by="console")
    registry.bind(old.frame_id, player["player_id"], old.output.output_id,
                  expected_generation=old.binding_generation + 1, by="console")
    assert _unresolved(registry) == 1  # the historical fence is kept; it is not served
    assert _served(registry) == []


def test_a_frame_newly_bound_to_that_output_inherits_nothing(registry):
    _, player, _, surfaces = rig(registry)
    _lose_first_output(registry, surfaces)
    old = surfaces[0]
    registry.unbind(old.frame_id, expected_generation=old.binding_generation, by="console")
    frame(registry, "replacement-frame")
    generation = registry.bind("replacement-frame", player["player_id"], old.output.output_id,
                               expected_generation=0, by="console")["generation"]
    # Same Player, Output and binding generation number; only the Frame differs.
    assert generation == old.binding_generation
    assert _unresolved(registry) == 1
    assert _served(registry) == []


def test_a_loss_from_an_earlier_authority_epoch_is_not_served(registry):
    _, _, key, surfaces = rig(registry)
    _lose_first_output(registry, surfaces)
    enroll(registry, key, device_id=DEVICE_ID, report=False)   # the display is still away
    assert _unresolved(registry) == 1
    assert _served(registry) == []


@pytest.mark.parametrize("resolved", [False, True])
def test_only_an_unresolved_loss_is_served(registry, resolved):
    _, _, _, surfaces = rig(registry)
    _lose_first_output(registry, surfaces)
    if resolved:
        with registry.db.transaction() as conn:
            conn.execute("UPDATE output_losses SET resolved_at=interrupted_at+1")
    assert len(_served(registry)) == (0 if resolved else 1)


def test_no_loss_serves_an_empty_list(registry):
    rig(registry)
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    with TestClient(app) as client:
        assert client.get("/v1/operator/snapshot", headers=AUTH).json()["output_interruptions"] == []
