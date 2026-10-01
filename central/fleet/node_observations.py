"""Immutable host telemetry intake and operator read projection."""
from __future__ import annotations

import json
from uuid import UUID

from central.fleet.node_sessions import NodeControlError, NodeSessions, claim_intake_in
from contracts.node_commands import parse_session_grant, producer_document
from contracts.node_observation import encode_host_observation, parse_host_observation
from contracts.node_preparation import encode_manager_preparation, parse_manager_preparation
from contracts.node_protocol import RebootFact, parse_node_message


class NodeObservations:
    def __init__(self, sessions: NodeSessions):
        self.sessions = sessions

    def record(self, session_id: UUID, credential: str, raw: bytes) -> dict:
        observation = parse_host_observation(raw)
        return self._record(session_id, credential, observation, encode_host_observation(observation), "node_host_observations")

    def record_preparation(self, session_id: UUID, credential: str, raw: bytes) -> dict:
        observation = parse_manager_preparation(raw)
        return self._record(session_id, credential, observation, encode_manager_preparation(observation), "node_manager_observations")

    def _record(self, session_id, credential, observation, canonical, table):
        # Table names are internal constants selected by typed owner entry points.
        with self.sessions.db.transaction() as conn:
            principal = self.sessions.authenticate_in(conn, session_id, credential,
                                                       allow_historical=True)
            if observation.producer != principal.grant.producer:
                raise NodeControlError("node_producer_mismatch", 403)
            prior = conn.execute(f"SELECT payload,received_at FROM {table} "
                                 "WHERE producer_id=%s AND sequence=%s",
                                 (principal.producer_id, observation.sequence)).fetchone()
            now = principal.admission.ensure_current(self.sessions.clock)
            if prior:
                if bytes(prior["payload"]) != canonical:
                    raise NodeControlError("node_observation_identity_conflict")
                return {"stored": True, "disposition": "duplicate",
                        "received_at": prior["received_at"], "authority_granted": False}
            claim_intake_in(conn, observation.producer.device_id,
                            "preparation" if table == "node_manager_observations" else "observation", now)
            conn.execute(f"INSERT INTO {table}(producer_id,sequence,payload,"
                         "received_at) VALUES(%s,%s,%s,%s)",
                         (principal.producer_id, observation.sequence, canonical, now))
            principal.admission.ensure_current(self.sessions.clock)
            return {"stored": True, "disposition": "recorded" if principal.current else "historical",
                    "received_at": now, "authority_granted": False}

    def status(self, device_id: str) -> dict:
        self.sessions.require_enabled()
        with self.sessions.db.transaction() as conn:
            generation = self.sessions.lock_device_generation_in(conn, device_id)
            rows = conn.execute("SELECT s.session_id,s.grant_payload,s.expires_at,s.revoked_at,"
                                "p.producer_id,p.projection,b.superseded_at FROM node_sessions s "
                                "JOIN node_producers p USING(producer_id) "
                                "JOIN node_boot_admissions b USING(admission_id) "
                                "WHERE s.device_id=%s AND s.device_generation=%s "
                                "ORDER BY s.issued_at DESC LIMIT 64", (device_id, generation)).fetchall()
            now = self.sessions.clock.utc()
            sessions = []
            for row in rows:
                grant = self.sessions._grant_eligibility_in(conn,
                    parse_session_grant(bytes(row["grant_payload"])))
                sample = conn.execute("SELECT payload,received_at FROM node_host_observations "
                                      "WHERE producer_id=%s ORDER BY sequence DESC LIMIT 1",
                                      (row["producer_id"],)).fetchone()
                preparation = conn.execute("SELECT payload,received_at FROM node_manager_observations "
                    "WHERE producer_id=%s ORDER BY sequence DESC LIMIT 1", (row["producer_id"],)).fetchone()
                sessions.append({"session_id": str(grant.session_id), "producer": producer_document(
                    grant.producer), "scope": grant.scope, "trust_mode": "lan_serial",
                    "physical_identity": "unverified", "command_eligible": grant.command_eligible,
                    "command_reason": grant.command_reason, "current": row["revoked_at"] is None
                    and row["superseded_at"] is None and row["expires_at"] > now,
                    "expires_at": row["expires_at"], "expires_boottime_ms": grant.expires_boottime_ms,
                    "manager_preparation": None if preparation is None else {
                        "sample": json.loads(bytes(preparation["payload"])), "received_at": preparation["received_at"],
                        "receipt_age_seconds": max(0, now-preparation["received_at"]), "authority_granted": False},
                    "projection": row["projection"], "host_observation": None if sample is None else {
                        "sample": json.loads(bytes(sample["payload"])),
                        "received_at": sample["received_at"], "receipt_age_seconds": max(
                            0, now - sample["received_at"])}})
            commands = conn.execute("SELECT c.command_id,c.operator_audit_ref,c.issued_at,c.expires_at,"
                                    "c.payload FROM node_reboot_commands c JOIN node_sessions s "
                                    "ON s.session_id=c.session_id WHERE s.device_id=%s "
                                    "ORDER BY c.issued_at DESC LIMIT 64", (device_id,)).fetchall()
            audit = []
            for command in commands:
                responses = conn.execute("SELECT payload,received_at FROM node_command_responses "
                                         "WHERE command_id=%s ORDER BY received_at",
                                         (command["command_id"],)).fetchall()
                effects = conn.execute("SELECT e.payload,e.received_at,e.disposition FROM node_evidence e "
                    "JOIN node_sessions s ON s.producer_id=e.producer_id "
                    "JOIN node_reboot_commands c ON c.session_id=s.session_id "
                    "WHERE c.command_id=%s AND e.kind='event' "
                    "AND convert_from(e.payload,'UTF8')::jsonb #>> '{message,causative_command_id,uuid}'=%s "
                    "ORDER BY e.sequence LIMIT 64", (command["command_id"], str(command["command_id"]))).fetchall()
                initiations = []
                for effect in effects:
                    message = parse_node_message(bytes(effect["payload"]))
                    if any(type(fact) is RebootFact for fact in message.facts):
                        initiations.append({"event_id": str(message.event_id), "sequence": message.sequence,
                            "occurred_boottime_ms": message.occurred_boottime_ms,
                            "received_at": effect["received_at"], "disposition": effect["disposition"],
                            "state": "reboot_initiated", "physical_completion": "unknown"})
                audit.append({"effects": initiations,**{key: value for key, value in command.items() if key != "payload"},
                              "command_id": str(command["command_id"]),
                              "command": json.loads(bytes(command["payload"])),
                              "responses": [{"message": json.loads(bytes(item["payload"])),
                                             "received_at": item["received_at"]} for item in responses]})
            conflict = conn.execute("SELECT revision,selected_boot_id,conflict,changed_at,operator_audit_ref "
                                    "FROM node_boot_claim_conflicts WHERE device_id=%s AND device_generation=%s",
                                    (device_id, generation)).fetchone()
            if conflict and conflict["selected_boot_id"]:
                conflict["selected_boot_id"] = str(conflict["selected_boot_id"])
            claims = conn.execute("SELECT kernel_boot_id,offer_id,created_at,refusal FROM node_boot_offers "
                "WHERE device_id=%s AND device_generation=%s ORDER BY created_at DESC,offer_id LIMIT 64",
                (device_id, generation)).fetchall()
            boot_claims = [{"kernel_boot_id": str(item["kernel_boot_id"]), "offer_id": str(item["offer_id"]),
                "first_received_at": item["created_at"], "offer_refusal": item["refusal"],
                "selectable": item["refusal"] is None, "physical_identity": "unverified"} for item in claims]
            return {"boot_claims": boot_claims, "boot_claim_admission": conflict, "device_id": device_id, "device_generation": generation, "read_at": now,
                    "sessions": sessions, "reboot_commands": audit, "physical_output": "unknown",
                    "runtime_reconciliation": "asynchronous_output_evidence"}
