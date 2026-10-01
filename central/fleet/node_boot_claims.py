"""Explicit uncertainty fence for overlapping LAN boot claims.

No claim or expiry proves a physical Pi ended. Commands require a single selected
boot; observations continue while ambiguity awaits an operator CAS selection.
"""
from __future__ import annotations

from uuid import UUID

from central.fleet.node_sessions import NodeControlError
from contracts.node_protocol import counter, identifier, token


def note_boot_claim_in(conn, device_id: str, generation: int, boot_id: UUID, now: float) -> None:
    row = conn.execute("SELECT * FROM node_boot_claim_conflicts WHERE device_id=%s "
                       "AND device_generation=%s FOR UPDATE", (device_id, generation)).fetchone()
    if row is None:
        conn.execute("INSERT INTO node_boot_claim_conflicts(device_id,device_generation,revision,"
                     "selected_boot_id,conflict,changed_at) VALUES(%s,%s,1,%s,FALSE,%s)",
                     (device_id, generation, boot_id, now))
        return
    if row["operator_audit_ref"] is not None:
        known = conn.execute("SELECT created_at FROM node_boot_offers WHERE device_id=%s "
                             "AND device_generation=%s AND kernel_boot_id=%s",
                             (device_id, generation, boot_id)).fetchone()
        if known and known["created_at"] <= row["changed_at"]:
            return
    # A still-valid grant for another boot makes command targeting ambiguous.
    active = conn.execute("SELECT 1 FROM node_sessions s JOIN node_producers p USING(producer_id) "
                          "JOIN node_boot_admissions b USING(admission_id) WHERE s.device_id=%s "
                          "AND s.device_generation=%s AND b.kernel_boot_id<>%s "
                          "AND s.expires_at>%s LIMIT 1", (device_id, generation, boot_id, now)).fetchone()
    if active or row["conflict"]:
        if not row["conflict"]:
            conn.execute("UPDATE node_boot_claim_conflicts SET conflict=TRUE,revision=revision+1,changed_at=%s "
                         "WHERE device_id=%s AND device_generation=%s", (now, device_id, generation))
    elif row["selected_boot_id"] != boot_id:
        # Prior grants all expired: admit a new logical claim, without asserting
        # physical replacement. Operator provenance is not fabricated.
        conn.execute("UPDATE node_boot_claim_conflicts SET selected_boot_id=%s,revision=revision+1,"
                     "changed_at=%s,operator_audit_ref=NULL WHERE device_id=%s AND device_generation=%s",
                     (boot_id, now, device_id, generation))


def command_eligibility_in(conn, device_id: str, generation: int, boot_id: UUID,
                           offer_id: UUID) -> tuple[bool, str]:
    basis = conn.execute("SELECT basis FROM node_offer_contexts WHERE offer_id=%s", (offer_id,)).fetchone()
    if basis is None or basis["basis"] != "node_v2":
        return False, "legacy_observation_adoption"
    row = conn.execute("SELECT selected_boot_id,conflict FROM node_boot_claim_conflicts "
                       "WHERE device_id=%s AND device_generation=%s", (device_id, generation)).fetchone()
    if row is None or row["conflict"]:
        return False, "boot_claim_conflict"
    if row["selected_boot_id"] != boot_id:
        return False, "boot_not_selected"
    return True, "boot_admitted_rollout_required"


def require_command_boot_in(conn, producer, offer_id: UUID) -> None:
    eligible, reason = command_eligibility_in(conn, producer.device_id, producer.device_generation,
                                             producer.kernel_boot_id, offer_id)
    if not eligible:
        raise NodeControlError(reason, 409)


class NodeBootClaims:
    def __init__(self, sessions):
        self.sessions = sessions

    def select(self, device_id: str, *, generation: int, expected_revision: int,
               boot_id: UUID, operator_audit_ref: str) -> dict:
        self.sessions.require_enabled()
        counter(generation, 1)
        counter(expected_revision, 1)
        identifier(boot_id)
        token(operator_audit_ref, 256)
        with self.sessions.db.transaction() as conn:
            current_generation = self.sessions.lock_device_generation_in(conn, device_id)
            if current_generation != generation:
                raise NodeControlError("node_generation_conflict")
            row = conn.execute("SELECT revision FROM node_boot_claim_conflicts WHERE device_id=%s "
                               "AND device_generation=%s FOR UPDATE", (device_id, generation)).fetchone()
            if row is None or row["revision"] != expected_revision:
                raise NodeControlError("node_boot_selection_conflict")
            known = conn.execute("SELECT 1 FROM node_boot_offers WHERE device_id=%s AND device_generation=%s "
                                 "AND kernel_boot_id=%s AND offer_payload IS NOT NULL",
                                 (device_id, generation, boot_id)).fetchone()
            if known is None:
                raise NodeControlError("node_boot_claim_unknown", 404)
            revision = expected_revision + 1
            now = self.sessions.clock.utc()
            conn.execute("UPDATE node_boot_claim_conflicts SET selected_boot_id=%s,conflict=FALSE,"
                         "revision=%s,changed_at=%s,operator_audit_ref=%s WHERE device_id=%s AND device_generation=%s",
                         (boot_id, revision, now, operator_audit_ref, device_id, generation))
            conn.execute("INSERT INTO node_boot_selections(device_id,device_generation,revision,selected_boot_id,"
                         "operator_audit_ref,selected_at) VALUES(%s,%s,%s,%s,%s,%s)",
                         (device_id, generation, revision, boot_id, operator_audit_ref, now))
            return {"revision": revision, "selected_boot_id": str(boot_id), "physical_identity": "unverified"}
