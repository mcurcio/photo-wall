"""Durable node evidence inbox and per-fact projection transaction adapter.

Storage acknowledgement grants no effect authority. Projection and outbox commit
with the original immutable envelope. Runtime alone interprets interruption work;
this adapter neither alters Runs nor infers outcomes from command responses.
"""
from __future__ import annotations

from uuid import UUID

from psycopg.types.json import Jsonb

from central.fleet.node_evidence import EvidenceProjection, ProjectedFact, reconcile_evidence
from central.fleet.node_sessions import NodeControlError, NodeSessions, claim_intake_in
from contracts.node_commands import parse_reboot_request
from contracts.node_protocol import (
    NodeCommandResponseV2,
    NodeEventV2,
    NodeSnapshotV2,
    encode_node_message,
    fact_key,
    parse_node_message,
)

MAX_PROJECTED_FACTS = 2048


def _projection(producer, rows: list[dict]) -> EvidenceProjection:
    facts = []
    for row in rows:
        message = parse_node_message(row["payload"].encode())
        facts.append(ProjectedFact(message.facts[row["fact_index"]], row["sequence"],
                                   row["observed_boottime_ms"], row["received_at"]))
    return EvidenceProjection(producer, tuple(facts))


class NodeIngest:
    def __init__(self, sessions: NodeSessions):
        self.sessions = sessions

    def ingest(self, session_id: UUID, credential: str, raw: bytes) -> dict:
        message = parse_node_message(raw)
        canonical = encode_node_message(message)
        with self.sessions.db.transaction() as conn:
            principal = self.sessions.authenticate_in(conn, session_id, credential,
                                                       allow_historical=True)
            if message.producer != principal.grant.producer:
                raise NodeControlError("node_producer_mismatch", 403)
            if type(message) is NodeCommandResponseV2:
                return self._response_in(conn, principal, message, canonical)
            row = conn.execute("SELECT projection FROM node_producers WHERE producer_id=%s "
                               "FOR UPDATE", (principal.producer_id,)).fetchone()
            kind = "event" if type(message) is NodeEventV2 else "snapshot"
            evidence_id = message.event_id if kind == "event" else message.snapshot_id
            sequence = message.sequence if kind == "event" else message.covered_through_sequence
            prior = conn.execute("SELECT payload,received_at FROM node_evidence WHERE "
                                 "producer_id=%s AND kind=%s AND evidence_id=%s",
                                 (principal.producer_id, kind, evidence_id)).fetchone()
            projection = _projection(message.producer, row["projection"])
            now = principal.admission.ensure_current(self.sessions.clock)
            if prior:
                try:
                    reconcile_evidence(projection, message, received_at=now,
                                       previous=parse_node_message(bytes(prior["payload"])))
                except ValueError as exc:
                    raise NodeControlError("node_evidence_identity_conflict") from exc
                return {"stored": True, "disposition": "duplicate",
                        "received_at": prior["received_at"], "authority_granted": False}
            if kind == "event":
                occupied = conn.execute("SELECT 1 FROM node_evidence WHERE producer_id=%s "
                                        "AND kind='event' AND sequence=%s",
                                        (principal.producer_id, sequence)).fetchone()
                if occupied:
                    raise NodeControlError("node_event_sequence_conflict")
            result = reconcile_evidence(projection, message, received_at=now)
            disposition = result.disposition if principal.current else "historical"
            if principal.current and len(result.projection.facts) > MAX_PROJECTED_FACTS:
                raise NodeControlError("node_projection_capacity", 429)
            claim_intake_in(conn, message.producer.device_id, "evidence", now)
            conn.execute("INSERT INTO node_evidence(producer_id,evidence_id,kind,sequence,"
                         "payload,received_at,disposition) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                         (principal.producer_id, evidence_id, kind, sequence, canonical, now,
                          disposition))
            if principal.current and result.updated_keys:
                indexed = {fact_key(item.fact): entry for item, entry in
                           zip(projection.facts, row["projection"], strict=True)}
                observed = (message.occurred_boottime_ms if kind == "event"
                            else message.sampled_boottime_ms)
                for fact in message.facts:
                    if fact_key(fact) in result.updated_keys:
                        indexed[fact_key(fact)] = {
                            "payload": encode_node_message(NodeSnapshotV2(
                                message.producer, evidence_id, sequence, observed, (fact,)
                            )).decode(), "fact_index": 0,
                            "sequence": sequence, "observed_boottime_ms": observed,
                            "received_at": now}
                conn.execute("UPDATE node_producers SET projection=%s WHERE producer_id=%s",
                             (Jsonb(list(indexed.values())), principal.producer_id))
            conn.execute("INSERT INTO node_reconciliation_work(producer_id,evidence_id,kind,"
                         "received_at) VALUES(%s,%s,%s,%s)",
                         (principal.producer_id, evidence_id, kind, now))
            principal.admission.ensure_current(self.sessions.clock)
            return {"stored": True, "disposition": disposition, "received_at": now,
                    "authority_granted": False}

    def _response_in(self, conn, principal, message: NodeCommandResponseV2,
                     canonical: bytes) -> dict:
        if (message.command_session_id != principal.grant.session_id
                or message.scope != principal.grant.scope):
            raise NodeControlError("node_response_scope_mismatch", 403)
        row = conn.execute("SELECT payload FROM node_reboot_commands WHERE command_id=%s",
                           (message.command_id,)).fetchone()
        if row is None:
            raise NodeControlError("node_command_unknown", 404)
        command = parse_reboot_request(bytes(row["payload"]))
        if (message.command_sha256 != command.command_sha256
                or message.command_session_id != command.command_session_id
                or message.producer != command.producer):
            raise NodeControlError("node_response_command_mismatch")
        rows = conn.execute("SELECT decision,payload,received_at FROM node_command_responses "
                            "WHERE command_id=%s", (message.command_id,)).fetchall()
        now = principal.admission.ensure_current(self.sessions.clock)
        for prior in rows:
            if prior["decision"] == message.decision:
                if bytes(prior["payload"]) != canonical:
                    raise NodeControlError("node_response_identity_conflict")
                return {"stored": True, "disposition": "duplicate",
                        "received_at": prior["received_at"], "effect_established": False}
            if prior["decision"] in ("accepted", "rejected") and message.decision in (
                    "accepted", "rejected"):
                raise NodeControlError("node_response_terminal_conflict")
        conn.execute("INSERT INTO node_command_responses(command_id,decision,payload,received_at) "
                     "VALUES(%s,%s,%s,%s)",
                     (message.command_id, message.decision, canonical, now))
        principal.admission.ensure_current(self.sessions.clock)
        return {"stored": True, "disposition": "recorded", "received_at": now,
                "effect_established": False}
