"""The current Registry control row and the one rule that joins an applied ACK receipt to it.

A node app link (node_app_links.py) and node acceptance (node_acceptance.py) both prove a
Player process against the receipt Registry issued for its latest applied control delivery;
this module is that join's one home.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass

from contracts.player_control import ControlAppliedReceipt


@dataclass(frozen=True, slots=True)
class AppControl:
    """Current Registry row; an older applied ACK is retained as history."""

    player_id: str
    public_key: str
    authority_epoch: int
    status: str
    applied_sequence: int
    applied_delivery_id: str | None
    applied_digest: str | None
    applied_at: float | None
    last_result: str | None
    last_result_sequence: int | None
    last_delivery_id: str | None
    last_result_digest: str | None
    last_result_at: float | None
    # These fields are internal Registry evidence, not part of the public
    # control_fact projection. Missing fields fail the causal join closed.
    issued_sequence: int | None = None
    pending_id: str | None = None
    pending_digest: str | None = None
    pending_expires: float | None = None
    applied_ack_nonce: str | None = None


def applied_is_latest(row: Mapping[str, object]) -> bool:
    """The applied delivery is still the latest: nothing issued, pending or rejected since.

    One home for the rule Registry uses to re-send (and replay the receipt of) an applied
    delivery and that app-link admission and qualification use to call a receipt current;
    `row` is a `player_control_sessions` row or an `AppControl`'s fields.
    """
    return (row["status"] == "negotiated"
            and row["pending_id"] is None and row["pending_digest"] is None
            and row["pending_expires"] is None
            and row["issued_sequence"] == row["applied_sequence"]
            and row["last_result_sequence"] == row["applied_sequence"]
            and row["last_result"] == "applied"
            and row["applied_at"] is not None and row["last_result_at"] is not None
            and row["last_delivery_id"] == row["applied_delivery_id"]
            and row["last_result_digest"] == row["applied_digest"]
            and row["applied_ack_nonce"] is not None)


def current_control_receipt_matches(receipt: ControlAppliedReceipt,
                                    control: AppControl | None) -> bool:
    """One canonical current-Registry ACK correlation rule for every proof carrier."""
    if control is None:
        return False
    return (
        applied_is_latest(asdict(control))
        and receipt.authority_epoch == control.authority_epoch
        and receipt.delivery_id == control.applied_delivery_id
        and receipt.delivery_sequence == control.applied_sequence
        and receipt.state_digest == control.applied_digest
        and receipt.ack_nonce == control.applied_ack_nonce
    )
