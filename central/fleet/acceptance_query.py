"""Internal, read-only Registry snapshot for managed-attempt assessment.

The caller owns one transaction and must first establish the current T1/T2
attempt, session and lifecycle under the fleet lock order. This adapter then
locks Player before its control row, so retirement, re-enrollment or a newer
delivery cannot change the receipt while the caller assesses that snapshot.
It does not confer command authority or write acceptance.
"""

from __future__ import annotations

from central.fleet.acceptance_evidence import AppControl


def load_current_app_control_in(conn, device_id: str) -> AppControl | None:
    """Return the internal current control row for one active device Player."""
    player = conn.execute(
        "SELECT id,public_key,authority_epoch FROM players "
        "WHERE device_id=%s AND retired_at IS NULL FOR SHARE",
        (device_id,),
    ).fetchone()
    if player is None:
        return None
    row = conn.execute(
        "SELECT authority_epoch,schema_version,status,issued_sequence,pending_id,"
        "pending_digest,pending_expires,applied_sequence,applied_at,"
        "applied_delivery_id,applied_digest,applied_ack_nonce,last_result,"
        "last_result_sequence,last_delivery_id,last_result_digest,last_result_at "
        "FROM player_control_sessions WHERE player_id=%s FOR SHARE",
        (player["id"],),
    ).fetchone()
    if row is None or row["authority_epoch"] != player["authority_epoch"]:
        return None
    return AppControl(
        player_id=player["id"], public_key=player["public_key"],
        authority_epoch=row["authority_epoch"], schema_version=row["schema_version"],
        status=row["status"], applied_sequence=row["applied_sequence"],
        applied_delivery_id=row["applied_delivery_id"],
        applied_digest=row["applied_digest"], applied_at=row["applied_at"],
        last_result=row["last_result"],
        last_result_sequence=row["last_result_sequence"],
        last_delivery_id=row["last_delivery_id"],
        last_result_digest=row["last_result_digest"],
        last_result_at=row["last_result_at"],
        issued_sequence=row["issued_sequence"], pending_id=row["pending_id"],
        pending_digest=row["pending_digest"],
        pending_expires=row["pending_expires"],
        applied_ack_nonce=row["applied_ack_nonce"],
    )
