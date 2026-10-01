"""Operator reboot audit and exact dispatch; no Run withdrawal policy here.

Issue and dispatch each hold the rollout gate before Fleet/generation locks.
The command record commits before HTTP polling can see it. Responses and effects
remain separate. Expiry or a changed session never retargets an old command.
Expiry is Central's own: poll filters by expires_at and HostCore dedupes by
command id. Central never states a node-clock deadline.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from hashlib import sha256
from uuid import UUID

from central.fleet.node_sessions import (
    NodeControlError,
    NodeSessions,
    claim_intake_in,
    command_eligibility_in,
)
from central.fleet.principal import SessionAdmission, _clock_sample
from central.fleet.rollout_gate import RolloutEffectGate
from contracts.node_commands import (
    RebootRequest,
    encode_reboot_request,
    parse_session_grant,
    reboot_digest,
)
from contracts.node_protocol import counter, identifier, token


@dataclass(frozen=True, slots=True)
class OperatorReboot:
    command_id: UUID
    session_id: UUID
    device_generation: int
    operator_audit_ref: str
    rollout_generation: int
    valid_for_seconds: int = 30

    def __post_init__(self) -> None:
        identifier(self.command_id)
        identifier(self.session_id)
        counter(self.device_generation, 1)
        counter(self.rollout_generation, 1)
        token(self.operator_audit_ref, 256)
        if type(self.valid_for_seconds) is not int or not 1 <= self.valid_for_seconds <= 60:
            raise ValueError("node_reboot_duration_invalid")


class NodeCommands:
    def __init__(self, sessions: NodeSessions, gate: RolloutEffectGate):
        self.sessions, self.gate = sessions, gate

    def request_reboot(self, device_id: str, request: OperatorReboot) -> dict:
        config = self.sessions.require_enabled()
        # Slots dataclass identity is explicitly frozen, independent of request transport.
        request_hash = sha256(json.dumps({"device_id": device_id,
            "command_id": str(request.command_id), "session_id": str(request.session_id),
            "device_generation": request.device_generation,
            "operator_audit_ref": request.operator_audit_ref,
            "rollout_generation": request.rollout_generation,
            "valid_for_seconds": request.valid_for_seconds}, sort_keys=True).encode()).hexdigest()
        started, monotonic = _clock_sample(self.sessions.clock)
        with self.sessions.db.transaction() as conn:
            gate = self.gate.require_open_in(conn, expected_generation=request.rollout_generation)
            generation = self.sessions.lock_device_generation_in(conn, device_id)
            row = conn.execute("SELECT s.*,b.superseded_at FROM node_sessions s "
                               "JOIN node_producers p USING(producer_id) "
                               "JOIN node_boot_admissions b USING(admission_id) "
                               "WHERE session_id=%s FOR UPDATE OF s", (request.session_id,)).fetchone()
            if row is None:
                raise NodeControlError("node_reboot_session_unavailable", 403)
            admission = SessionAdmission(started, monotonic, row["expires_at"])
            now = admission.ensure_current(self.sessions.clock)
            if (generation != request.device_generation
                    or row["device_generation"] != generation or row["device_id"] != device_id
                    or row["scope"] != "operator_reboot" or row["revoked_at"] is not None
                    or row["superseded_at"] is not None or row["issued_at"] > now
                    or row["expires_at"] <= now):
                raise NodeControlError("node_reboot_session_unavailable", 403)
            grant = parse_session_grant(bytes(row["grant_payload"]))
            if grant.producer.installation_audience != config.installation_audience:
                raise NodeControlError("node_audience_mismatch", 403)
            eligible, reason = command_eligibility_in(conn, grant.offer_id)
            if not eligible:
                raise NodeControlError(reason, 409)
            prior = conn.execute("SELECT request_sha256,payload,expires_at FROM node_reboot_commands "
                                 "WHERE command_id=%s", (request.command_id,)).fetchone()
            if prior:
                if prior["request_sha256"] != request_hash:
                    raise NodeControlError("node_reboot_identity_conflict")
                if prior["expires_at"] <= now:
                    raise NodeControlError("node_reboot_expired", 410)
                gate.ensure_current_in(conn)
                if admission.ensure_current(self.sessions.clock) >= prior["expires_at"]:
                    raise NodeControlError("node_reboot_expired", 410)
                return {"command": json.loads(bytes(prior["payload"])), "duplicate": True,
                        "effect_established": False}
            command = RebootRequest(request.command_id, "0" * 64, grant.session_id,
                                     grant.offer_id, grant.producer)
            command = replace(command, command_sha256=reboot_digest(command))
            payload = encode_reboot_request(command)
            expires_at = min(now + request.valid_for_seconds, row["expires_at"])
            claim_intake_in(conn, device_id, "command", now)
            conn.execute("INSERT INTO node_reboot_commands(command_id,session_id,request_sha256,"
                         "operator_audit_ref,payload,issued_at,expires_at,gate_generation,"
                         "gate_scope_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         (request.command_id, request.session_id, request_hash,
                          request.operator_audit_ref, payload, now, expires_at,
                          gate.generation, gate.scope_sha256))
            gate.ensure_current_in(conn)
            final_now = admission.ensure_current(self.sessions.clock)
            if final_now >= expires_at:
                raise NodeControlError("node_reboot_expired", 410)
            return {"command": json.loads(payload), "duplicate": False, "effect_established": False}

    def poll(self, session_id: UUID, credential: str) -> dict:
        self.sessions.require_enabled()
        # Locate the relevant gate without taking lower-level locks. Recheck every
        # frozen command after both gate and session locks have been acquired.
        with self.sessions.db.transaction() as conn:
            located = conn.execute("SELECT gate_generation FROM node_reboot_commands "
                                   "WHERE session_id=%s ORDER BY issued_at DESC LIMIT 1",
                                   (session_id,)).fetchone()
            if located is None:
                self.sessions.authenticate_in(conn, session_id, credential)
                return {"commands": []}
            gate = self.gate.require_open_in(conn, expected_generation=located["gate_generation"])
            principal = self.sessions.authenticate_in(conn, session_id, credential)
            if principal.grant.scope != "operator_reboot":
                raise NodeControlError("node_command_scope_denied", 403)
            now = principal.admission.ensure_current(self.sessions.clock)
            rows = conn.execute("SELECT payload FROM node_reboot_commands WHERE session_id=%s "
                                "AND expires_at>%s AND gate_generation=%s AND gate_scope_sha256=%s "
                                "ORDER BY issued_at LIMIT 32",
                                (session_id, now, gate.generation, gate.scope_sha256)).fetchall()
            gate.ensure_current_in(conn)
            final_now = principal.admission.ensure_current(self.sessions.clock)
            # No lease expires in a downstream lock wait and then escapes dispatch.
            if final_now > now:
                rows = conn.execute("SELECT payload FROM node_reboot_commands WHERE session_id=%s "
                                    "AND expires_at>%s AND gate_generation=%s AND gate_scope_sha256=%s "
                                    "ORDER BY issued_at LIMIT 32",
                                    (session_id, final_now, gate.generation, gate.scope_sha256)).fetchall()
                gate.ensure_current_in(conn)
            return {"commands": [json.loads(bytes(row["payload"])) for row in rows]}
