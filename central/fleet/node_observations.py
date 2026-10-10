"""Immutable host telemetry intake and operator read projection."""
from __future__ import annotations

import json
from uuid import UUID

from central.fleet.host_thresholds import thresholds_document
from central.fleet.node_bus_presence import bus_links_in
from central.fleet.node_commands import OUTSTANDING_REBOOT_SQL
from central.fleet.node_display import display_outputs_in
from central.fleet.node_sessions import (
    OBSERVATION_DAILY_CAP,
    PREPARATION_DAILY_CAP,
    NodeControlError,
    NodeSessions,
    claim_intake_in,
)
from contracts.node_boot import parse_node_boot_offer
from contracts.node_commands import parse_session_grant, producer_document
from contracts.node_host_facts import (
    encode_host_facts,
    fact_values_document,
    parse_host_facts,
    stored_fact_values,
)
from contracts.node_observation import (
    HOST_OBSERVATION_INTERVAL_SECONDS,
    encode_host_observation,
    parse_host_observation,
)
from contracts.node_preparation import encode_manager_preparation, parse_manager_preparation
from contracts.node_protocol import RebootFact, parse_node_message

# A producer's newest host sample, by sequence through the primary key (never ORDER BY
# received_at over the table): coalescing (its payload's boot clock) and G12 both read it.
_NEWEST_HOST_SQL = ("SELECT payload,received_at FROM node_host_observations WHERE producer_id={p} "
                    "ORDER BY sequence DESC LIMIT 1")

# A producer's newest App Manager samples, by sequence through the primary key. Coalescing
# looks for the same (state, operation, fault) among them; a window holds at most one stored
# sample per such key, so these rows cover this many distinct keys (more only costs storage,
# bounded by the cap).
_PREPARATION_KEYS = 8
_RECENT_PREPARATION_SQL = ("SELECT payload,received_at FROM node_manager_observations "
                           f"WHERE producer_id=%s ORDER BY sequence DESC LIMIT {_PREPARATION_KEYS}")


def _preparation_key(sample) -> tuple:
    return sample.state, sample.operation_id, sample.fault


# G12 (console DDD §63), one statement per read. Per active device in its current generation:
# the current admission (at most one, node_current_boot) and the most recently superseded one,
# each one's newest-admitted host_core producer, that producer's newest sample, the current
# admission's newest-admitted app_manager producer and its newest preparation sample, and
# today's observation and preparation quota rows; the current admission's node boot offer (its base tag) and
# its host_core producer's facts row. Every sample read is producer-scoped on the primary key.
_FLEET_HOSTS_SQL = (
    "SELECT d.device_id,d.serial,ch.payload,ch.received_at,ph.received_at AS previous_received_at,"
    "cm.payload AS preparation_payload,cm.received_at AS preparation_received_at,"
    "q.used AS intake_used,pq.used AS preparation_intake_used,cb.admission_id AS current_admission,co.offer_payload,"
    "cf.payload AS facts_payload,cf.first_received_at AS facts_first_received_at FROM devices d "
    "JOIN fleet_device_lifecycle l ON l.device_id=d.device_id "
    "LEFT JOIN node_boot_admissions cb ON cb.device_id=d.device_id "
    "AND cb.device_generation=l.generation AND cb.superseded_at IS NULL "
    "LEFT JOIN LATERAL (SELECT producer_id FROM node_producers WHERE admission_id=cb.admission_id "
    "AND owner='host_core' ORDER BY admitted_at DESC,producer_id DESC LIMIT 1) cp ON true "
    "LEFT JOIN LATERAL (" + _NEWEST_HOST_SQL.format(p="cp.producer_id") + ") ch ON true "
    "LEFT JOIN node_boot_offers co ON co.offer_id=cb.offer_id "
    "LEFT JOIN node_host_facts cf ON cf.producer_id=cp.producer_id "
    "LEFT JOIN LATERAL (SELECT producer_id FROM node_producers WHERE admission_id=cb.admission_id "
    "AND owner='app_manager' ORDER BY admitted_at DESC,producer_id DESC LIMIT 1) mp ON true "
    "LEFT JOIN LATERAL (SELECT payload,received_at FROM node_manager_observations "
    "WHERE producer_id=mp.producer_id ORDER BY sequence DESC LIMIT 1) cm ON true "
    "LEFT JOIN LATERAL (SELECT admission_id FROM node_boot_admissions WHERE device_id=d.device_id "
    "AND device_generation=l.generation AND superseded_at IS NOT NULL "
    "ORDER BY superseded_at DESC,admission_id DESC LIMIT 1) pb ON true "
    "LEFT JOIN LATERAL (SELECT producer_id FROM node_producers WHERE admission_id=pb.admission_id "
    "AND owner='host_core' ORDER BY admitted_at DESC,producer_id DESC LIMIT 1) pp ON true "
    "LEFT JOIN LATERAL (" + _NEWEST_HOST_SQL.format(p="pp.producer_id") + ") ph ON true "
    "LEFT JOIN node_intake_quotas q ON q.device_id=d.device_id AND q.day=%(day)s "
    "AND q.kind='observation' "
    "LEFT JOIN node_intake_quotas pq ON pq.device_id=d.device_id AND pq.day=%(day)s "
    "AND pq.kind='preparation' "
    "WHERE d.retired_at IS NULL AND l.revoked_at IS NULL ORDER BY d.device_id")


class NodeObservations:
    def __init__(self, sessions: NodeSessions):
        self.sessions = sessions

    def record(self, session_id: UUID, credential: str, raw: bytes) -> dict:
        observation = parse_host_observation(raw)
        return self._record(session_id, credential, observation, encode_host_observation(observation), "node_host_observations")

    def record_preparation(self, session_id: UUID, credential: str, raw: bytes) -> dict:
        observation = parse_manager_preparation(raw)
        return self._record(session_id, credential, observation, encode_manager_preparation(observation), "node_manager_observations")

    def record_facts(self, session_id: UUID, credential: str, raw: bytes) -> dict:
        """Host facts ingest (console DDD §64): one row per producer, replaced only by a higher
        sequence. `first_received_at` is the receipt of the row's current values: kept when a
        higher sequence repeats them, renewed when any value changes."""
        facts = parse_host_facts(raw)
        canonical = encode_host_facts(facts)
        with self.sessions.db.transaction() as conn:
            principal = self.sessions.authenticate_in(conn, session_id, credential,
                                                       allow_historical=True)
            if facts.producer != principal.grant.producer:
                raise NodeControlError("node_producer_mismatch", 403)
            now = principal.admission.ensure_current(self.sessions.clock)
            # The session row lock taken by authenticate_in serializes one producer's posts.
            prior = conn.execute("SELECT sequence,payload,received_at FROM node_host_facts "
                                 "WHERE producer_id=%s FOR UPDATE", (principal.producer_id,)).fetchone()
            if prior is not None and facts.sequence < prior["sequence"]:
                return {"stored": False, "disposition": "stale", "received_at": now,
                        "authority_granted": False}
            if prior is not None and facts.sequence == prior["sequence"]:
                if bytes(prior["payload"]) != canonical:
                    raise NodeControlError("node_observation_identity_conflict")
                return {"stored": True, "disposition": "duplicate",
                        "received_at": prior["received_at"], "authority_granted": False}
            # Only a new value claims intake: a same-values resend at a higher sequence (every
            # Host Management process start, so a crash loop) rewrites the row without using
            # the day's quota, so restarts cannot outrun the derived cap. Both sides are plain
            # documents (a nested `boot` included); a row stored before `boot` existed reads it
            # as None, so it equals a post that did not read one.
            same = (prior is not None
                    and stored_fact_values(bytes(prior["payload"])) == fact_values_document(facts))
            if not same:
                claim_intake_in(conn, facts.producer.device_id, "host_facts", now)
            if prior is None:
                conn.execute("INSERT INTO node_host_facts(producer_id,sequence,payload,"
                             "first_received_at,received_at) VALUES(%s,%s,%s,%s,%s)",
                             (principal.producer_id, facts.sequence, canonical, now, now))
            else:
                # The payload is rewritten either way, so a resend at the new sequence is a
                # duplicate; first_received_at moves only when a value changed.
                conn.execute("UPDATE node_host_facts SET sequence=%s,payload=%s,received_at=%s,"
                             "first_received_at=CASE WHEN %s THEN first_received_at ELSE %s END "
                             "WHERE producer_id=%s",
                             (facts.sequence, canonical, now, same, now, principal.producer_id))
            principal.admission.ensure_current(self.sessions.clock)
            return {"stored": True, "disposition": "recorded" if principal.current else "historical",
                    "received_at": now, "authority_granted": False}

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
            if self._coalesced_in(conn, table, principal.producer_id, observation):
                return {"stored": False, "disposition": "coalesced", "received_at": now,
                        "authority_granted": False}
            claim_intake_in(conn, observation.producer.device_id,
                            "preparation" if table == "node_manager_observations" else "observation", now)
            conn.execute(f"INSERT INTO {table}(producer_id,sequence,payload,"
                         "received_at) VALUES(%s,%s,%s,%s)",
                         (principal.producer_id, observation.sequence, canonical, now))
            principal.admission.ensure_current(self.sessions.clock)
            return {"stored": True, "disposition": "recorded" if principal.current else "historical",
                    "received_at": now, "authority_granted": False}

    @staticmethod
    def _coalesced_in(conn, table, producer_id, observation) -> bool:
        """Whether this post stores nothing (§60, §64), judged on the producer's OWN boot clock:
        the post's `sampled_boottime_ms` against a stored sample's, both read by one kernel of
        one boot (a producer is one boot), so a clock is compared only with itself and no
        Central receipt clock, of this replica or another, enters the judgement. A negative
        difference (a sample older than the stored one) stores. Host Management: one stored
        sample per producer per interval. App Manager: one per producer per (state, operation,
        fault) per interval, so an alternating stream (`preparing`/`refused` every poll) stores
        each state at most once an interval and never reaches the derived cap. The cost: a
        state that returns within one interval of its last stored sample is not stored again
        until that interval passes, so the newest stored state can lag by up to one interval
        plus the node's next resend. A node that misreports its boot clock defeats coalescing
        for its own producer only; the derived cap still bounds its intake."""
        window_ms = HOST_OBSERVATION_INTERVAL_SECONDS * 1000

        def within(stored) -> bool:
            return 0 <= observation.sampled_boottime_ms - stored.sampled_boottime_ms < window_ms

        if table == "node_host_observations":
            newest = conn.execute(_NEWEST_HOST_SQL.format(p="%s"), (producer_id,)).fetchone()
            return newest is not None and within(parse_host_observation(bytes(newest["payload"])))
        key = _preparation_key(observation)
        for row in conn.execute(_RECENT_PREPARATION_SQL, (producer_id,)).fetchall():
            stored = parse_manager_preparation(bytes(row["payload"]))
            if _preparation_key(stored) == key and within(stored):
                return True
        return False

    def fleet_hosts(self) -> dict:
        """G12: every active box's serial (its claim at boot) and current-boot host and App
        Manager samples, read under one snapshot.

        No fleet or device lock is taken, so a held lock never blocks this read. A superseded
        boot's sample is never served as `host`; its receipt alone is served, and only when
        the current boot has none."""
        self.sessions.require_enabled()
        with self.sessions.db.transaction() as conn:
            # Must precede the first data query (central/operator_snapshot.py).
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            read_at = self.sessions.clock.utc()
            rows = conn.execute(_FLEET_HOSTS_SQL, {"day": int(read_at) // 86400}).fetchall()
            bus_links = bus_links_in(conn, [row["device_id"] for row in rows])
        devices = []
        for row in rows:
            host = None
            if row["received_at"] is not None:
                sample = json.loads(bytes(row["payload"]))
                host = {"received_at": row["received_at"], "metrics": sample["metrics"],
                        "fault_code": sample["fault_code"]}
            preparation = None
            if row["preparation_received_at"] is not None:
                sample = json.loads(bytes(row["preparation_payload"]))
                preparation = {"received_at": row["preparation_received_at"],
                               **{key: sample[key] for key in ("state", "fault", "available_bytes",
                                                               "required_bytes")}}
            boot = None
            if row["current_admission"] is not None:
                # As node_lifecycle's qualification reads it: an admission adopted from a
                # legacy offer (history from the retired V1 path) has no node offer payload.
                payload = row["offer_payload"]
                boot = {"base_tag": None if payload is None
                        else parse_node_boot_offer(bytes(payload)).base.tag}
            facts = None
            if row["facts_payload"] is not None:
                # Tolerant: an older-shape row serves its missing facts as None, never fails
                # the read. `base_tag` is the node's own record of its base: a host report,
                # served beside `boot.base_tag` (Central's offer), never merged with it.
                # `facts.boot` is the node's boot stage report (None when absent or invalid).
                facts = {"first_received_at": row["facts_first_received_at"],
                         **stored_fact_values(bytes(row["facts_payload"]))}
            devices.append({"device_id": row["device_id"], "serial": row["serial"], "host": host,
                            "previous_boot_received_at": row["previous_received_at"] if host is None else None,
                            "intake_full": (row["intake_used"] or 0) >= OBSERVATION_DAILY_CAP,
                            "preparation_intake_full":
                                (row["preparation_intake_used"] or 0) >= PREPARATION_DAILY_CAP,
                            "boot": boot, "facts": facts, "preparation": preparation,
                            "bus_link": bus_links[row["device_id"]]})
        return {"read_at": read_at, "thresholds": thresholds_document(), "devices": devices}

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
                    "expires_at": row["expires_at"],
                    "manager_preparation": None if preparation is None else {
                        "sample": json.loads(bytes(preparation["payload"])), "received_at": preparation["received_at"],
                        "receipt_age_seconds": max(0, now-preparation["received_at"]), "authority_granted": False},
                    "projection": row["projection"], "host_observation": None if sample is None else {
                        "sample": json.loads(bytes(sample["payload"])),
                        "received_at": sample["received_at"], "receipt_age_seconds": max(
                            0, now - sample["received_at"])}})
            # `outstanding` and the responses come from ONE statement, so one snapshot: a
            # response committed mid-read cannot be served beside an `outstanding` that
            # predates it (response ingest does not take the fleet lock).
            commands = conn.execute("SELECT c.command_id,c.operator_audit_ref,c.issued_at,c.expires_at,"
                                    f"{OUTSTANDING_REBOOT_SQL} AS outstanding,"
                                    "(SELECT coalesce(json_agg(json_build_object("
                                    "'message',convert_from(r.payload,'UTF8')::json,"
                                    "'received_at',r.received_at) ORDER BY r.received_at),'[]'::json) "
                                    "FROM node_command_responses r WHERE r.command_id=c.command_id) AS responses,"
                                    "c.payload FROM node_reboot_commands c JOIN node_sessions s "
                                    "ON s.session_id=c.session_id WHERE s.device_id=%s "
                                    "ORDER BY c.issued_at DESC LIMIT 64", (now, device_id)).fetchall()
            audit = []
            for command in commands:
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
                              "command": json.loads(bytes(command["payload"]))})
            claims = conn.execute("SELECT kernel_boot_id,offer_id,created_at,refusal FROM node_boot_offers "
                "WHERE device_id=%s AND device_generation=%s ORDER BY created_at DESC,offer_id LIMIT 64",
                (device_id, generation)).fetchall()
            boot_claims = [{"kernel_boot_id": str(item["kernel_boot_id"]), "offer_id": str(item["offer_id"]),
                "first_received_at": item["created_at"], "offer_refusal": item["refusal"],
                "physical_identity": "unverified"} for item in claims]
            return {"boot_claims": boot_claims,
                    "device_id": device_id, "device_generation": generation, "read_at": now,
                    "sessions": sessions, "reboot_commands": audit, "physical_output": "unknown",
                    "display_outputs": display_outputs_in(conn, device_id, generation),
                    "runtime_reconciliation": "asynchronous_output_evidence"}
