"""Explicit boot/producer admission and separate, boot-scoped LAN credentials.

Lock order: optional rollout gate, Fleet, device, lifecycle, boot, session,
producer, then evidence/commands. Retirement shares the Fleet/device boundary.
A session is a serial claim; it does not authenticate a physical device.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from hmac import compare_digest
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from central.content_catalog.catalog import device_id_for_serial, sanitize_serial
from central.db import Database
from central.fleet.locks import lock_fleet_assets_in
from central.fleet.principal import SessionAdmission, _clock_sample
from contracts.node_boot import parse_node_boot_offer
from contracts.node_commands import (
    NodeSessionClaim,
    NodeSessionGrant,
    encode_session_claim,
    encode_session_grant,
    parse_session_grant,
    producer_document,
    scope_for_owner,
)
from contracts.node_protocol import NodeProducerV2, digest, token
from contracts.time import Clock


class NodeControlError(ValueError):
    def __init__(self, code: str, status: int = 409, *, details: dict | None = None):
        super().__init__(code)
        self.code, self.status, self.details = code, status, details or {}


@dataclass(frozen=True, slots=True)
class NodeControlConfig:
    installation_audience: str
    session_seconds: int = 3600

    def __post_init__(self) -> None:
        token(self.installation_audience, 256)
        if self.installation_audience == "photo-wall-central-t0":
            raise ValueError("node_command_audience_required")
        if type(self.session_seconds) is not int or not 60 <= self.session_seconds <= 86400:
            raise ValueError("node_session_duration_invalid")


@dataclass(frozen=True, slots=True)
class NodePrincipal:
    grant: NodeSessionGrant
    producer_id: UUID
    admission: SessionAdmission
    current: bool


def claim_intake_in(conn, device_id: str, kind: str, now: float) -> None:
    """Device/day caps are deliberately generous but finite across replicas."""
    limits = {"session": 1024, "evidence": 100000, "observation": 20000, "command": 1024, "display": 500000, "preparation": 20000}
    day = int(now) // 86400
    row = conn.execute("INSERT INTO node_intake_quotas(device_id,day,kind,used) "
                       "VALUES(%s,%s,%s,1) ON CONFLICT(device_id,day,kind) DO UPDATE "
                       "SET used=node_intake_quotas.used+1 WHERE node_intake_quotas.used<%s "
                       "RETURNING used", (device_id, day, kind, limits[kind])).fetchone()
    if row is None:
        raise NodeControlError("node_intake_capacity", 429)
    conn.execute("DELETE FROM node_intake_quotas WHERE device_id=%s AND day<%s",
                 (device_id, day - 1))


class NodeSessions:
    def __init__(self, db: Database, clock: Clock, config: NodeControlConfig | None):
        self.db, self.clock, self.config = db, clock, config

    def require_enabled(self) -> NodeControlConfig:
        if self.config is None:
            raise NodeControlError("node_control_disabled", 503)
        return self.config

    def lock_device_generation_in(self, conn, device_id: str) -> int:
        lock_fleet_assets_in(conn)
        device = conn.execute("SELECT retired_at FROM devices WHERE device_id=%s FOR UPDATE",
                              (device_id,)).fetchone()
        lifecycle = conn.execute("SELECT generation,revoked_at FROM fleet_device_lifecycle "
                                 "WHERE device_id=%s FOR UPDATE", (device_id,)).fetchone()
        if (device is None or lifecycle is None or device["retired_at"] is not None
                or lifecycle["revoked_at"] is not None):
            raise NodeControlError("node_device_unavailable", 403)
        return lifecycle["generation"]

    def _offer_in(self, conn, claim: NodeSessionClaim, device_id: str, generation: int, now: float):
        node = conn.execute("SELECT offer_payload,device_generation FROM node_boot_offers WHERE offer_id=%s",
                            (claim.offer_id,)).fetchone()
        if node:
            if node["offer_payload"] is None or node["device_generation"] != generation:
                raise NodeControlError("node_offer_mismatch", 403)
            offer = parse_node_boot_offer(bytes(node["offer_payload"]))
            if (offer.device_id, offer.serial, offer.kernel_boot_id, offer.installation_audience) != (
                    device_id, claim.serial, claim.kernel_boot_id, self.config.installation_audience):
                raise NodeControlError("node_offer_mismatch", 403)
            return {"expires_at": offer.expires_at_utc_ms / 1000}
        legacy = conn.execute("SELECT * FROM fleet_boot_offers WHERE offer_id=%s", (claim.offer_id,)).fetchone()
        if (legacy is None or legacy["device_id"] != device_id or legacy["serial"] != claim.serial
                or legacy["kernel_boot_id"] != claim.kernel_boot_id):
            raise NodeControlError("node_offer_mismatch", 403)
        conn.execute("INSERT INTO node_offer_contexts(offer_id,basis,legacy_offer_id) "
                     "VALUES(%s,'legacy_adoption',%s) ON CONFLICT DO NOTHING", (claim.offer_id, claim.offer_id))
        return legacy

    def _grant_eligibility_in(self, conn, grant: NodeSessionGrant) -> NodeSessionGrant:
        from central.fleet.node_boot_claims import command_eligibility_in
        live = conn.execute("SELECT 1 FROM node_sessions s JOIN node_producers p USING(producer_id) "
                            "JOIN node_boot_admissions b USING(admission_id) WHERE s.session_id=%s "
                            "AND s.revoked_at IS NULL AND s.expires_at>%s AND b.superseded_at IS NULL",
                            (grant.session_id, self.clock.utc())).fetchone()
        if live is None:
            return replace(grant, command_eligible=False, command_reason="node_session_unavailable")
        eligible, reason = command_eligibility_in(conn, grant.producer.device_id,
            grant.producer.device_generation, grant.producer.kernel_boot_id, grant.offer_id)
        return replace(grant, command_eligible=eligible, command_reason=reason)

    def enroll(self, claim: NodeSessionClaim) -> NodeSessionGrant:
        config = self.require_enabled()
        serial = sanitize_serial(claim.serial)
        device_id = device_id_for_serial(serial)
        if serial != claim.serial or device_id is None:
            raise NodeControlError("node_serial_invalid", 422)
        started, monotonic = _clock_sample(self.clock)
        with self.db.transaction() as conn:
            generation = self.lock_device_generation_in(conn, device_id)
            now, later = _clock_sample(self.clock)
            if later < monotonic:
                raise NodeControlError("node_clock_invalid", 503)
            now = max(now, started + later - monotonic)
            prior = conn.execute("SELECT * FROM node_sessions WHERE session_id=%s FOR UPDATE",
                                 (claim.session_id,)).fetchone()
            claim_hash = sha256(encode_session_claim(claim)).hexdigest()
            if prior:
                if prior["claim_sha256"] != claim_hash:
                    raise NodeControlError("node_session_identity_conflict")
                if (prior["revoked_at"] is not None or prior["expires_at"] <= now
                        or prior["device_generation"] != generation):
                    raise NodeControlError("node_session_unavailable", 403)
                grant = parse_session_grant(bytes(prior["grant_payload"]))
                if grant.producer.installation_audience != config.installation_audience:
                    raise NodeControlError("node_audience_mismatch", 403)
                SessionAdmission(now, later, prior["expires_at"]).ensure_current(self.clock)
                return self._grant_eligibility_in(conn, grant)
            credential_hash = sha256(claim.credential.encode()).hexdigest()
            if conn.execute("SELECT 1 FROM node_sessions WHERE credential_sha256=%s",
                            (credential_hash,)).fetchone():
                raise NodeControlError("node_credential_reuse", 409)
            claim_intake_in(conn, device_id, "session", now)
            offer = self._offer_in(conn, claim, device_id, generation, now)
            # Context records distinguish genuine V2 offers from weak legacy
            # observation adoption. Neither rewrites the original offer.
            boot = conn.execute("SELECT * FROM node_boot_admissions WHERE device_id=%s "
                                "AND device_generation=%s AND superseded_at IS NULL FOR UPDATE",
                                (device_id, generation)).fetchone()
            boot_id = boot["kernel_boot_id"] if boot else None
            if boot_id != claim.kernel_boot_id:
                if claim.expected_boot_id != boot_id:
                    raise NodeControlError("node_boot_conflict", details={
                        "current_boot_id": str(boot_id) if boot_id else None})
                used = conn.execute("SELECT admission_id,offer_id FROM node_boot_admissions WHERE device_id=%s "
                                    "AND device_generation=%s AND kernel_boot_id=%s",
                                    (device_id, generation, claim.kernel_boot_id)).fetchone()
                selected = conn.execute("SELECT selected_boot_id,conflict,operator_audit_ref "
                                        "FROM node_boot_claim_conflicts WHERE device_id=%s AND device_generation=%s",
                                        (device_id, generation)).fetchone()
                reselected = bool(used and selected and not selected["conflict"]
                                  and selected["selected_boot_id"] == claim.kernel_boot_id
                                  and selected["operator_audit_ref"] is not None
                                  and used["offer_id"] == claim.offer_id)
                if (used and not reselected) or (not used and offer["expires_at"] <= now):
                    raise NodeControlError("node_boot_not_admissible", 403)
                if boot:
                    conn.execute("UPDATE node_boot_admissions SET superseded_at=%s "
                                 "WHERE admission_id=%s", (now, boot["admission_id"]))
                    conn.execute("UPDATE node_sessions SET revoked_at=%s WHERE device_id=%s "
                                 "AND device_generation=%s AND revoked_at IS NULL",
                                 (now, device_id, generation))
                if reselected:
                    admission_id = used["admission_id"]
                    conn.execute("UPDATE node_boot_admissions SET superseded_at=NULL WHERE admission_id=%s",
                                 (admission_id,))
                else:
                    admission_id = uuid4()
                    conn.execute("INSERT INTO node_boot_admissions(admission_id,device_id,"
                                 "device_generation,kernel_boot_id,offer_id,installation_audience,"
                                 "trust_mode,admitted_at) VALUES(%s,%s,%s,%s,%s,%s,'lan_serial',%s)",
                                 (admission_id, device_id, generation, claim.kernel_boot_id,
                                  claim.offer_id, config.installation_audience, now))
            else:
                if (boot["offer_id"] != claim.offer_id
                        or boot["installation_audience"] != config.installation_audience):
                    raise NodeControlError("node_boot_adoption_mismatch", 403)
                admission_id = boot["admission_id"]
            current = conn.execute("SELECT session_id FROM node_sessions WHERE device_id=%s "
                                   "AND device_generation=%s AND owner=%s AND revoked_at IS NULL "
                                   "FOR UPDATE", (device_id, generation, claim.owner)).fetchone()
            current_id = current["session_id"] if current else None
            if claim.expected_session_id != current_id:
                raise NodeControlError("node_session_conflict", details={
                    "current_boot_id": str(claim.kernel_boot_id),
                    "current_session_id": str(current_id) if current_id else None})
            if current:
                conn.execute("UPDATE node_sessions SET revoked_at=%s WHERE session_id=%s",
                             (now, current_id))
            producer = NodeProducerV2(config.installation_audience, device_id, generation,
                                      claim.kernel_boot_id, claim.owner, claim.incarnation_id)
            producer_row = conn.execute("SELECT producer_id FROM node_producers WHERE "
                                        "admission_id=%s AND owner=%s AND incarnation_id=%s",
                                        (admission_id, claim.owner, claim.incarnation_id)).fetchone()
            producer_id = producer_row["producer_id"] if producer_row else uuid4()
            if not producer_row:
                conn.execute("INSERT INTO node_producers(producer_id,admission_id,owner,"
                             "incarnation_id,producer,admitted_at) VALUES(%s,%s,%s,%s,%s,%s)",
                             (producer_id, admission_id, claim.owner, claim.incarnation_id,
                              Jsonb(producer_document(producer)), now))
            from central.fleet.node_boot_claims import note_boot_claim_in
            note_boot_claim_in(conn, device_id, generation, claim.kernel_boot_id, now)
            grant = NodeSessionGrant(producer, claim.session_id, claim.offer_id,
                                     claim.sampled_boottime_ms + config.session_seconds * 1000,
                                     scope_for_owner(claim.owner))
            conn.execute("INSERT INTO node_sessions(session_id,producer_id,device_id,"
                         "device_generation,owner,scope,credential_sha256,claim_sha256,grant_payload,"
                         "issued_at,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         (claim.session_id, producer_id, device_id, generation, claim.owner,
                          grant.scope, credential_hash, claim_hash,
                          encode_session_grant(grant), now, now + config.session_seconds))
            SessionAdmission(now, later, now + config.session_seconds).ensure_current(self.clock)
            return self._grant_eligibility_in(conn, grant)

    def authenticate_in(self, conn, session_id: UUID, credential: str, *,
                        allow_historical: bool = False) -> NodePrincipal:
        self.require_enabled()
        try:
            digest(credential)
        except ValueError as exc:
            raise NodeControlError("node_credential_required", 401) from exc
        principal, row = self._load_session_in(conn, session_id, allow_historical=allow_historical)
        if not compare_digest(row["credential_sha256"], sha256(credential.encode()).hexdigest()):
            raise NodeControlError("node_session_unavailable", 401)
        return principal

    def load_operator_target_in(self, conn, session_id: UUID) -> NodePrincipal:
        """Internal owner port for an already-authenticated operator command request.

        This is not a node authentication mechanism and must never back a public
        node route. Device credentials use authenticate_in instead.
        """
        return self._load_session_in(conn, session_id, allow_historical=False)[0]

    def _load_session_in(self, conn, session_id: UUID, *, allow_historical: bool):
        config = self.require_enabled()
        started, monotonic = _clock_sample(self.clock)
        # Read only to locate owning device, then re-read under canonical locks.
        located = conn.execute("SELECT device_id FROM node_sessions WHERE session_id=%s",
                               (session_id,)).fetchone()
        if not located:
            raise NodeControlError("node_session_unavailable", 401)
        generation = self.lock_device_generation_in(conn, located["device_id"])
        row = conn.execute("SELECT s.*,b.superseded_at FROM node_sessions s "
                           "JOIN node_producers p USING(producer_id) "
                           "JOIN node_boot_admissions b USING(admission_id) "
                           "WHERE s.session_id=%s FOR UPDATE OF s", (session_id,)).fetchone()
        now, later = _clock_sample(self.clock)
        if later < monotonic:
            raise NodeControlError("node_clock_invalid", 503)
        now = max(now, started + later - monotonic)
        if (row is None or row["device_generation"] != generation
                or now < row["issued_at"] or now >= row["expires_at"]):
            raise NodeControlError("node_session_unavailable", 401)
        current = row["revoked_at"] is None and row["superseded_at"] is None
        if not current and not allow_historical:
            raise NodeControlError("node_session_superseded", 403)
        grant = parse_session_grant(bytes(row["grant_payload"]))
        if grant.producer.installation_audience != config.installation_audience:
            raise NodeControlError("node_audience_mismatch", 403)
        return NodePrincipal(grant, row["producer_id"],
                             SessionAdmission(now, later, row["expires_at"]), current), row


NODE_SAMPLE_FUTURE_TOLERANCE_MS = 1000


def grant_boottime_at(grant: NodeSessionGrant, expires_at: float, now: float) -> int:
    """Enrolled boot-clock estimate; never substitute receipt time for sample age."""
    return grant.expires_boottime_ms - int((expires_at-now)*1000)


def session_boottime_at(principal: NodePrincipal, now: float) -> int:
    return grant_boottime_at(principal.grant, principal.admission.expires_at, now)


def node_sample_fresh(local_now_ms: int, sampled_ms: int, *, max_age_ms: int = 5000) -> bool:
    """Bounded enrollment/transport skew is tolerated, never an age refresh."""
    return -NODE_SAMPLE_FUTURE_TOLERANCE_MS <= local_now_ms-sampled_ms < max_age_ms
