"""Admit base-observed process linkage to the existing Registry receipt/key.

This records operational evidence under LAN-serial assurance. It neither creates
physical device identity nor qualifies app health, output pixels, or a release.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Literal
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from central.actor import Actor
from central.fleet.acceptance_evidence import current_control_receipt_matches
from central.fleet.acceptance_query import load_current_app_control_in
from central.fleet.node_sessions import NodeControlError, NodeSessions, claim_intake_in
from central.transaction_locks import acquire_runtime_locks
from contracts.node_app_link import (
    NodeAppLinkChallengeV2,
    encode_node_app_link,
    node_app_link_message,
    parse_node_app_link,
)
from contracts.player_control import ControlAppliedReceipt

LinkStanding = Literal["new", "restates", "older"]


def link_standing(current: NodeAppLinkChallengeV2 | None,
                  new: NodeAppLinkChallengeV2) -> LinkStanding:
    """How a proved link stands against its device generation's current one: the one judge,
    pure (decision 0033). Central keeps the current link only, one row per device generation.

    - `"restates"`: every field equals `current`'s except `nonce` and `sampled_boottime_ms`,
      and the sample is not earlier. The same process says again that it is linked (588 of
      the last 658 relinks measured on 2026-11-15); an exact retry, nonce included, restates
      too.
    - `"older"`: the same kernel boot (`producer.kernel_boot_id`) and a lower
      `(app_epoch, sampled_boottime_ms)` than `current`'s. A late retry of a link the app has
      since replaced; it must never replace the newer one. Both values are the Node's own
      counters within one boot, so a clock is compared only with itself.
    - `"new"`: anything else, `current is None` and another kernel boot included.

    Never reads a clock of Central's, the database, or the nonce to tell a restating link from
    a new one.
    """
    if current is None:
        return "new"
    if (replace(new, nonce=current.nonce, sampled_boottime_ms=current.sampled_boottime_ms) == current
            and new.sampled_boottime_ms >= current.sampled_boottime_ms):
        return "restates"
    if (new.producer.kernel_boot_id == current.producer.kernel_boot_id
            and (new.app_epoch, new.sampled_boottime_ms)
            < (current.app_epoch, current.sampled_boottime_ms)):
        return "older"
    return "new"


class NodeAppLinks:
    def __init__(self, sessions: NodeSessions):
        self.sessions = sessions

    @staticmethod
    def _require_current_proof_in(conn, challenge, proof, receipt) -> None:
        control = load_current_app_control_in(conn, challenge.producer.device_id, lock=True)
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
        """Admit a proved link (decision 0033):

        After the session and scope checks, the stored link sent again (the current row's
        payload, byte for byte) is answered as `"restates"` below without a new proof check, as
        a retry is today: its receipt may have been superseded since it was proved. Any other
        link passes the proof check, then stands by `link_standing` against the current row:
        - `"new"`: the row of `(device_id, device_generation)` is replaced (inserted for a
          first link), its `changed_at` and `changed_by = 'node'` stamped, `created_*` kept;
          one intake count. Answer `stored: True, duplicate: False, current: True`.
        - `"restates"`: nothing is written, not a row version and not an intake count. Answer
          `stored: True, duplicate: True, current: True` with the current `admitted_at`.
        - `"older"`: nothing is written. Answer `stored: False, duplicate: False,
          current: False, admitted_at: None`.
        All three answer 200, so the Pi's outbox slot clears (appliance/node/app_link.py).

        Errors, unchanged: `node_link_scope_mismatch` (403), `node_link_control_not_current`
        (409), `node_link_signature_invalid` (403); the current nonce with another payload is
        `node_link_identity_conflict`.
        """
        proof = parse_node_app_link(raw)
        canonical = encode_node_app_link(proof)
        challenge = proof.challenge
        receipt = ControlAppliedReceipt.model_validate_json(challenge.control_receipt)
        device_id, generation = challenge.producer.device_id, challenge.producer.device_generation
        with self.sessions.db.transaction() as conn:
            acquire_runtime_locks(conn)
            principal = self.sessions.authenticate_in(conn, session_id, credential)
            if (principal.grant.scope != "app_effect" or challenge.producer != principal.grant.producer
                    or challenge.command_session_id != session_id):
                raise NodeControlError("node_link_scope_mismatch", 403)
            # The Runtime locks serialize every admission, so this plain read is the row the
            # write below replaces: no row lock, so a link that writes nothing dirties no row.
            stored = conn.execute("SELECT nonce,payload,admitted_at FROM node_app_links "
                                  "WHERE device_id=%s AND device_generation=%s",
                                  (device_id, generation)).fetchone()
            now = principal.admission.ensure_current(self.sessions.clock)
            if stored and stored["nonce"] == challenge.nonce:
                if bytes(stored["payload"]) != canonical:
                    raise NodeControlError("node_link_identity_conflict")
                return _answer("restates", stored["admitted_at"])
            self._require_current_proof_in(conn, challenge, proof, receipt)
            current = parse_node_app_link(bytes(stored["payload"])).challenge if stored else None
            standing = link_standing(current, challenge)
            if standing == "restates":
                return _answer(standing, stored["admitted_at"])
            if standing == "older":
                return _answer(standing, None)
            claim_intake_in(conn, device_id, "evidence", now)
            conn.execute("INSERT INTO node_app_links(device_id,device_generation,nonce,producer_id,"
                         "player_id,authority_epoch,payload,admitted_at,created_at,created_by,"
                         "changed_at,changed_by) VALUES(%(device)s,%(generation)s,%(nonce)s,"
                         "%(producer)s,%(player)s,%(epoch)s,%(payload)s,%(now)s,%(now)s,%(by)s,"
                         "%(now)s,%(by)s) ON CONFLICT (device_id,device_generation) DO UPDATE SET "
                         "nonce=EXCLUDED.nonce,producer_id=EXCLUDED.producer_id,"
                         "player_id=EXCLUDED.player_id,authority_epoch=EXCLUDED.authority_epoch,"
                         "payload=EXCLUDED.payload,admitted_at=EXCLUDED.admitted_at,"
                         "changed_at=EXCLUDED.changed_at,changed_by=EXCLUDED.changed_by",
                         {"device": device_id, "generation": generation, "nonce": challenge.nonce,
                          "producer": principal.producer_id, "player": challenge.player_id,
                          "epoch": challenge.authority_epoch, "payload": canonical, "now": now,
                          "by": _ACTOR})
            principal.admission.ensure_current(self.sessions.clock)
            return _answer(standing, now)


_ACTOR: Actor = "node"


def _answer(standing: LinkStanding, admitted_at: float | None) -> dict:
    """`admit`'s 200 answer for a link's standing (its docstring's three cases)."""
    return {"stored": standing != "older", "duplicate": standing == "restates",
            "admitted_at": admitted_at, "current": standing != "older",
            "physical_identity": "unverified"}


def load_current_node_app_link_in(conn, principal):
    """Read accepted operational linkage under the caller's generation/session lock.

    Callers needing Runtime authority acquire Coordination→Runtime before session
    authentication. A still-current Registry epoch preserves the accepted process
    link even when a later control delivery supersedes its original proof receipt.
    """
    producer = principal.grant.producer
    current = load_current_node_app_link_for_device_in(conn, producer.device_id, producer.device_generation,
                                                       lock=True)
    if current is None or current[0].producer.kernel_boot_id != producer.kernel_boot_id:
        return None
    return current[0]


def load_current_node_app_link_for_device_in(conn, device_id: str, generation: int, *, lock: bool):
    """The current boot's accepted link and Central's admission time, or None.

    The device-keyed half of `load_current_node_app_link_in`, for the operator read
    (console DDD Part E G4): a link whose boot or Registry epoch is no longer current is
    not the app a qualification would observe. `lock` as `load_current_app_control_in`'s.
    """
    row = conn.execute("SELECT l.payload,l.admitted_at FROM node_app_links l "
                       "JOIN node_producers p USING(producer_id) "
                       "JOIN node_boot_admissions b USING(admission_id) "
                       "JOIN node_sessions s ON s.producer_id=p.producer_id AND s.revoked_at IS NULL "
                       "WHERE l.device_id=%s AND l.device_generation=%s "
                       "AND b.superseded_at IS NULL", (device_id, generation)).fetchone()
    if row is None:
        return None
    link = parse_node_app_link(bytes(row["payload"])).challenge
    control = load_current_app_control_in(conn, device_id, lock=lock)
    if control is None or (control.player_id, control.authority_epoch) != (link.player_id, link.authority_epoch):
        return None
    return link, row["admitted_at"]


def load_current_node_app_link_for_player_in(conn, player_id: str, authority_epoch: int, now: float):
    """Runtime owner read under its locks; exact current device/session lineage only."""
    row = conn.execute("SELECT l.payload,l.device_id FROM node_app_links l "
                       "JOIN node_producers p USING(producer_id) "
                       "JOIN node_boot_admissions b USING(admission_id) "
                       "JOIN node_sessions s ON s.producer_id=p.producer_id "
                       "JOIN fleet_device_lifecycle f ON f.device_id=l.device_id "
                       "JOIN devices d ON d.device_id=l.device_id "
                       "WHERE l.player_id=%s AND l.authority_epoch=%s "
                       "AND b.superseded_at IS NULL AND s.revoked_at IS NULL AND s.expires_at>%s "
                       "AND f.generation=l.device_generation AND f.revoked_at IS NULL AND d.retired_at IS NULL",
                       (player_id, authority_epoch, now)).fetchone()
    if row is None:
        return None
    link = parse_node_app_link(bytes(row["payload"])).challenge
    control = load_current_app_control_in(conn, row["device_id"], lock=True)
    if control is None or (control.player_id, control.authority_epoch) != (player_id, authority_epoch):
        return None
    return link
