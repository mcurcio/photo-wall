"""The current Registry control row and the one rule that joins an applied ACK receipt to it.

A node app link (node_app_links.py) and node acceptance (node_acceptance.py) both prove a
Player process against the receipt Registry issued for its latest applied control delivery;
this module is that join's one home.
"""

from __future__ import annotations

from dataclasses import dataclass

from contracts.player_control import ControlAppliedReceipt


@dataclass(frozen=True, slots=True)
class AppControl:
    """Current Registry row; an older applied ACK is retained as history."""

    player_id: str
    public_key: str
    authority_epoch: int
    schema_version: int
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


def current_control_receipt_matches(receipt: ControlAppliedReceipt,
                                    control: AppControl | None) -> bool:
    """One canonical current-Registry ACK correlation rule for every proof carrier."""
    if control is None:
        return False
    return (
        control.status == "negotiated" and control.schema_version == 2
        and control.applied_at is not None and control.last_result_at is not None
        and control.last_result == "applied"
        and control.issued_sequence == control.applied_sequence
        and control.pending_id is None and control.pending_digest is None
        and control.pending_expires is None
        and control.last_result_sequence == control.applied_sequence
        and control.last_delivery_id == control.applied_delivery_id
        and control.last_result_digest == control.applied_digest
        and receipt.authority_epoch == control.authority_epoch
        and receipt.delivery_id == control.applied_delivery_id
        and receipt.delivery_sequence == control.applied_sequence
        and receipt.state_digest == control.applied_digest
        and receipt.ack_nonce == control.applied_ack_nonce
    )
