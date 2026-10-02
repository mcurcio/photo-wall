"""The causal receipt is read only from the current internal Registry row."""

from test_registry import enroll

from central.fleet.acceptance_query import load_current_app_control_in
from contracts.player_control import ControlAck, ControlHello


def test_current_control_snapshot_carries_accepted_nonce_without_public_leak(registry):
    player, _, request = enroll(registry)
    player_id, epoch = player["player_id"], player["authority_epoch"]
    registry.control_hello(player_id, ControlHello(
        authority_epoch=epoch, schemas=(1, 2), capabilities=()))
    delivery = registry.issue_control_delivery_record(player_id, epoch, "a" * 64)
    accepted = registry.control_ack_response(player_id, ControlAck(
        authority_epoch=epoch, delivery_id=delivery["delivery_id"], result="applied"))
    assert accepted.receipt is not None
    with registry.db.transaction() as conn:
        snapshot = load_current_app_control_in(conn, request.device_id)
    assert snapshot is not None
    assert snapshot.player_id == player_id
    assert snapshot.authority_epoch == epoch
    assert snapshot.public_key == request.public_key
    assert snapshot.applied_ack_nonce == accepted.receipt.ack_nonce
    assert snapshot.applied_delivery_id == accepted.receipt.delivery_id
    assert snapshot.issued_sequence == snapshot.applied_sequence
    assert snapshot.pending_id is None
    assert "applied_ack_nonce" not in registry.control_fact(player_id)

    registry.issue_control_delivery_record(player_id, epoch, "b" * 64)
    with registry.db.transaction() as conn:
        newer = load_current_app_control_in(conn, request.device_id)
    assert newer is not None and newer.applied_ack_nonce is None
    assert newer.pending_id is not None


def test_control_snapshot_refuses_missing_or_historical_player_row(registry):
    player, _, request = enroll(registry)
    with registry.db.transaction() as conn:
        assert load_current_app_control_in(conn, "device-" + "0" * 64) is None
        conn.execute("DELETE FROM player_control_sessions WHERE player_id=%s",
                     (player["player_id"],))
        assert load_current_app_control_in(conn, request.device_id) is None
