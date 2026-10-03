"""Admit base-observed process linkage to the existing Registry receipt/key.

This records operational evidence under LAN-serial assurance. It neither creates
physical device identity nor qualifies app health, output pixels, or a release.
"""
from __future__ import annotations

from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from central.fleet.acceptance_evidence import current_control_receipt_matches
from central.fleet.acceptance_query import load_current_app_control_in
from central.fleet.node_sessions import NodeControlError, NodeSessions, claim_intake_in
from central.transaction_locks import acquire_runtime_locks
from contracts.node_app_link import encode_node_app_link, node_app_link_message, parse_node_app_link
from contracts.player_control import ControlAppliedReceipt


class NodeAppLinks:
    def __init__(self, sessions: NodeSessions):
        self.sessions = sessions

    @staticmethod
    def _require_current_proof_in(conn, challenge, proof, receipt) -> None:
        control = load_current_app_control_in(conn, challenge.producer.device_id)
        if (control is None or control.player_id != challenge.player_id
                or control.authority_epoch != challenge.authority_epoch
                or control.public_key != proof.public_key
                or not current_control_receipt_matches(receipt, control)):
            raise NodeControlError("node_link_control_not_current", 409)
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(proof.public_key)).verify(
                bytes.fromhex(proof.signature), node_app_link_message(challenge))
        except (InvalidSignature, ValueError) as exc:
            raise NodeControlError("node_link_signature_invalid", 403) from exc

    def admit(self, session_id: UUID, credential: str, raw: bytes) -> dict:
        proof = parse_node_app_link(raw)
        canonical = encode_node_app_link(proof)
        challenge = proof.challenge
        receipt = ControlAppliedReceipt.model_validate_json(challenge.control_receipt)
        with self.sessions.db.transaction() as conn:
            acquire_runtime_locks(conn)
            principal = self.sessions.authenticate_in(conn, session_id, credential)
            if (principal.grant.scope != "app_effect" or challenge.producer != principal.grant.producer
                    or challenge.command_session_id != session_id):
                raise NodeControlError("node_link_scope_mismatch", 403)
            prior = conn.execute("SELECT payload,admitted_at,superseded_at FROM node_app_links "
                                 "WHERE nonce=%s", (challenge.nonce,)).fetchone()
            now = principal.admission.ensure_current(self.sessions.clock)
            if prior:
                if bytes(prior["payload"]) != canonical:
                    raise NodeControlError("node_link_identity_conflict")
                return {"stored": True, "duplicate": True, "admitted_at": prior["admitted_at"],
                        "current": prior["superseded_at"] is None, "physical_identity": "unverified"}
            self._require_current_proof_in(conn, challenge, proof, receipt)
            claim_intake_in(conn, challenge.producer.device_id, "evidence", now)
            conn.execute("UPDATE node_app_links SET superseded_at=%s WHERE device_id=%s "
                         "AND device_generation=%s AND superseded_at IS NULL",
                         (now, challenge.producer.device_id, challenge.producer.device_generation))
            conn.execute("INSERT INTO node_app_links(nonce,producer_id,device_id,device_generation,"
                         "player_id,authority_epoch,payload,admitted_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                         (challenge.nonce, principal.producer_id, challenge.producer.device_id,
                          challenge.producer.device_generation, challenge.player_id,
                          challenge.authority_epoch, canonical, now))
            principal.admission.ensure_current(self.sessions.clock)
            return {"stored": True, "duplicate": False, "admitted_at": now,
                    "current": True, "physical_identity": "unverified"}


def load_current_node_app_link_in(conn, principal):
    """Read accepted operational linkage under the caller's generation/session lock.

    Callers needing Runtime authority acquire Coordination→Runtime before session
    authentication. A still-current Registry epoch preserves the accepted process
    link even when a later control delivery supersedes its original proof receipt.
    """
    producer = principal.grant.producer
    current = load_current_node_app_link_for_device_in(conn, producer.device_id, producer.device_generation)
    if current is None or current[0].producer.kernel_boot_id != producer.kernel_boot_id:
        return None
    return current[0]


def load_current_node_app_link_for_device_in(conn, device_id: str, generation: int):
    """The current boot's accepted link and Central's admission time, or None.

    The device-keyed half of `load_current_node_app_link_in`, for the operator read
    (console DDD Part E G4): a link whose boot or Registry epoch is no longer current is
    not the app a qualification would observe.
    """
    row = conn.execute("SELECT l.payload,l.admitted_at FROM node_app_links l "
                       "JOIN node_producers p USING(producer_id) "
                       "JOIN node_boot_admissions b USING(admission_id) "
                       "JOIN node_sessions s ON s.producer_id=p.producer_id AND s.revoked_at IS NULL "
                       "WHERE l.device_id=%s AND l.device_generation=%s AND l.superseded_at IS NULL "
                       "AND b.superseded_at IS NULL", (device_id, generation)).fetchone()
    if row is None:
        return None
    link = parse_node_app_link(bytes(row["payload"])).challenge
    control = load_current_app_control_in(conn, device_id)
    if control is None or (control.player_id, control.authority_epoch) != (link.player_id, link.authority_epoch):
        return None
    return link, row["admitted_at"]


def load_current_node_app_link_for_player_in(conn, player_id: str, authority_epoch: int, now: float):
    """Runtime owner read under its locks; exact current Fleet/session lineage only."""
    row = conn.execute("SELECT l.payload,l.device_id FROM node_app_links l "
                       "JOIN node_producers p USING(producer_id) "
                       "JOIN node_boot_admissions b USING(admission_id) "
                       "JOIN node_sessions s ON s.producer_id=p.producer_id "
                       "JOIN fleet_device_lifecycle f ON f.device_id=l.device_id "
                       "JOIN devices d ON d.device_id=l.device_id "
                       "WHERE l.player_id=%s AND l.authority_epoch=%s AND l.superseded_at IS NULL "
                       "AND b.superseded_at IS NULL AND s.revoked_at IS NULL AND s.expires_at>%s "
                       "AND f.generation=l.device_generation AND f.revoked_at IS NULL AND d.retired_at IS NULL",
                       (player_id, authority_epoch, now)).fetchone()
    if row is None:
        return None
    link = parse_node_app_link(bytes(row["payload"])).challenge
    control = load_current_app_control_in(conn, row["device_id"])
    if control is None or (control.player_id, control.authority_epoch) != (player_id, authority_epoch):
        return None
    return link
