"""V2 immutable app preparation and separately authorized unbound withdrawal.

A new operation never resolves mutable desired policy after issue. Exact roots
remain retained by their published deployment/acceptance owners. D16 bound
withdrawal is deliberately unavailable until its product policy is selected.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from central.fleet.models import OfferAsset
from central.fleet.node_acceptance import current_cohort_in
from central.fleet.node_app_links import load_current_node_app_link_in
from central.fleet.node_boot import parse_node_deployment
from central.fleet.node_boot_claims import require_command_boot_in
from central.fleet.node_sessions import NodeControlError, NodeSessions, claim_intake_in
from central.fleet.rollout_gate import RolloutEffectGate
from central.transaction_locks import acquire_runtime_locks, holds_runtime_locks_in
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_boot import parse_node_boot_offer
from contracts.node_lifecycle import (
    StageCommandV2,
    StopPermitV2,
    encode_app_effect_event,
    encode_stage_command,
    encode_stage_ready,
    encode_stop_permit,
    parse_app_effect_event,
    parse_stage_command,
    parse_stage_ready,
    ready_digest,
    stage_digest,
)
from contracts.node_protocol import counter, identifier, token


@dataclass(frozen=True, slots=True)
class OperatorAppStage:
    operation_id: UUID
    command_id: UUID
    session_id: UUID
    device_generation: int
    deployment_id: UUID
    rollout_generation: int
    operator_audit_ref: str
    valid_for_seconds: int = 1800

    def __post_init__(self):
        for value in (self.operation_id, self.command_id, self.session_id, self.deployment_id):
            identifier(value)
        counter(self.device_generation, 1)
        counter(self.rollout_generation, 1)
        token(self.operator_audit_ref, 256)
        if type(self.valid_for_seconds) is not int or not 60 <= self.valid_for_seconds <= 3600:
            raise ValueError("app_stage_duration_invalid")


class NodeLifecycle:
    def __init__(self, sessions: NodeSessions, gate: RolloutEffectGate):
        self.sessions, self.gate = sessions, gate

    @staticmethod
    def _reference_in(conn, digest: str) -> AppEnvironmentRefV2:
        row = conn.execute("SELECT reference FROM node_environment_catalog WHERE environment_sha256=%s",
                           (digest,)).fetchone()
        if row is None:
            raise NodeControlError("node_environment_unknown", 404)
        return AppEnvironmentRefV2(**row["reference"])

    @staticmethod
    def _boot_in(conn, principal):
        row = conn.execute("SELECT offer_payload FROM node_boot_offers WHERE offer_id=%s",
                           (principal.grant.offer_id,)).fetchone()
        if row is None or row["offer_payload"] is None:
            raise NodeControlError("node_v2_boot_required")
        return parse_node_boot_offer(bytes(row["offer_payload"]))

    @staticmethod
    def _operation_in(conn, operation_id):
        row = conn.execute("SELECT * FROM node_app_operations WHERE operation_id=%s FOR UPDATE",
                           (operation_id,)).fetchone()
        if row is None:
            raise NodeControlError("node_app_operation_unknown", 404)
        return row, parse_stage_command(bytes(row["command_payload"]))

    @staticmethod
    def _same_boot(principal, command):
        p, c = principal.grant.producer, command.producer
        if (p.installation_audience, p.device_id, p.device_generation, p.kernel_boot_id) != (
                c.installation_audience, c.device_id, c.device_generation, c.kernel_boot_id):
            raise NodeControlError("node_app_operation_scope_changed", 403)

    def _command_current_in(self, conn, principal, row, command, gate):
        self._same_boot(principal, command)
        if (principal.grant.scope != "app_effect" or principal.grant.producer != command.producer
                or principal.grant.session_id != command.command_session_id
                or row["rollout_generation"] != gate.generation or row["rollout_scope_sha256"] != gate.scope_sha256):
            raise NodeControlError("node_app_command_scope_changed", 403)
        require_command_boot_in(conn, principal.grant.producer, principal.grant.offer_id)
        if principal.admission.ensure_current(self.sessions.clock) >= row["expires_at"]:
            raise NodeControlError("node_app_command_expired", 410)
        if conn.execute("SELECT 1 FROM node_app_stage_cancellations WHERE operation_id=%s", (command.operation_id,)).fetchone():
            raise NodeControlError("node_app_stage_cancelled")
        gate.ensure_current_in(conn)

    def stage(self, device_id: str, request: OperatorAppStage) -> dict:
        self.sessions.require_enabled()
        canonical = json.dumps({"device_id": device_id, **asdict(request)}, default=str,
                               sort_keys=True, separators=(",", ":")).encode()
        request_hash = sha256(canonical).hexdigest()
        with self.sessions.db.transaction() as conn:
            gate = self.gate.require_open_in(conn, expected_generation=request.rollout_generation)
            acquire_runtime_locks(conn)
            principal = self.sessions.load_operator_target_in(conn, request.session_id)
            producer = principal.grant.producer
            if (producer.device_id != device_id or producer.device_generation != request.device_generation
                    or principal.grant.scope != "app_effect"):
                raise NodeControlError("node_app_operator_target_changed", 403)
            require_command_boot_in(conn, producer, principal.grant.offer_id)
            now = principal.admission.ensure_current(self.sessions.clock)
            old = conn.execute("SELECT * FROM node_app_operations WHERE operation_id=%s",
                               (request.operation_id,)).fetchone()
            if old:
                if old["request_sha256"] != request_hash:
                    raise NodeControlError("node_app_operation_identity_conflict")
                command = parse_stage_command(bytes(old["command_payload"]))
                self._command_current_in(conn, principal, old, command, gate)
                return {"command": json.loads(bytes(old["command_payload"])), "duplicate": True}
            active = conn.execute("SELECT 1 FROM node_app_operations o WHERE o.device_id=%s "
                "AND o.device_generation=%s AND NOT EXISTS(SELECT 1 FROM node_app_stage_cancellations c WHERE c.operation_id=o.operation_id) AND NOT EXISTS(SELECT 1 FROM node_app_discharges d "
                "WHERE d.operation_id=o.operation_id) AND (o.expires_at>%s OR EXISTS(SELECT 1 FROM node_app_permits p "
                "WHERE p.operation_id=o.operation_id))", (device_id, producer.device_generation, now)).fetchone()
            if active:
                raise NodeControlError("node_app_prior_operation_unreconciled")
            link = load_current_node_app_link_in(conn, principal)
            if link is None:
                raise NodeControlError("node_app_current_process_unlinked")
            boot = self._boot_in(conn, principal)
            publication = conn.execute("SELECT document FROM node_deployments WHERE deployment_id=%s",
                                       (request.deployment_id,)).fetchone()
            if publication is None:
                raise NodeControlError("node_deployment_unknown", 404)
            deployment = parse_node_deployment(bytes(publication["document"]))
            if deployment.base != boot.base or deployment.app_environment is None:
                raise NodeControlError("node_app_target_base_mismatch")
            old_environment = self._reference_in(conn, link.environment_sha256)
            fallback = None
            try:
                cohort = current_cohort_in(conn, device_id, producer.device_generation, now)
            except NodeControlError:
                cohort = None  # Preparation is safe; absent qualification cannot authorize stop.
            if cohort:
                accepted = conn.execute("SELECT environment_sha256 FROM node_environment_acceptances "
                    "WHERE device_id=%s AND device_generation=%s AND base_content_key=%s AND cohort=%s "
                    "AND environment_sha256<>%s ORDER BY accepted_at DESC LIMIT 1",
                    (device_id, producer.device_generation, boot.base.content_key, Jsonb(cohort),
                     deployment.app_environment.environment_sha256)).fetchone()
                if accepted:
                    fallback = self._reference_in(conn, accepted["environment_sha256"])
            lifetime = min(request.valid_for_seconds, principal.admission.expires_at-now)
            if lifetime < 60:
                raise NodeControlError("node_app_session_too_short")
            # Translate elapsed server lease duration to the enrolled local boot clock.
            expires_ms = principal.grant.expires_boottime_ms-int((principal.admission.expires_at-(now+lifetime))*1000)
            command = StageCommandV2(request.operation_id, request.command_id, "0"*64, producer,
                request.session_id, principal.grant.offer_id, link.process, link.app_epoch, old_environment,
                deployment.app_environment, fallback, expires_ms)
            command = replace(command, command_sha256=stage_digest(command))
            claim_intake_in(conn, device_id, "command", now)
            conn.execute("INSERT INTO node_app_operations(operation_id,command_id,device_id,device_generation,"
                "player_id,authority_epoch,producer_id,session_id,deployment_id,request_sha256,command_sha256,"
                "command_payload,rollout_generation,rollout_scope_sha256,created_at,expires_at,operator_audit_ref) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (request.operation_id, request.command_id, device_id, producer.device_generation, link.player_id,
                 link.authority_epoch, principal.producer_id, request.session_id, request.deployment_id, request_hash,
                 command.command_sha256, encode_stage_command(command), gate.generation, gate.scope_sha256, now, now+lifetime, request.operator_audit_ref))
            gate.ensure_current_in(conn)
            principal.admission.ensure_current(self.sessions.clock)
            return {"command": json.loads(encode_stage_command(command)), "duplicate": False,
                    "stop_authorized": False, "fallback_qualified": fallback is not None}

    def desired(self, session_id: UUID, credential: str, *, effects: bool = False) -> dict:
        self.sessions.require_enabled()
        with self.sessions.db.transaction() as conn:
            located = conn.execute("SELECT rollout_generation FROM node_app_operations o JOIN node_sessions s "
                "ON s.device_id=o.device_id AND s.device_generation=o.device_generation WHERE s.session_id=%s "
                "ORDER BY o.created_at DESC LIMIT 1", (session_id,)).fetchone()
            gate = (self.gate.require_open_in(conn, expected_generation=located["rollout_generation"])
                    if effects and located else None)
            principal = self.sessions.authenticate_in(conn, session_id, credential)
            owner = "app_effect_broker" if effects else "app_manager"
            if principal.grant.producer.owner != owner:
                raise NodeControlError("node_app_scope_required", 403)
            if effects:
                require_command_boot_in(conn, principal.grant.producer, principal.grant.offer_id)
            rows = conn.execute("SELECT * FROM node_app_operations WHERE device_id=%s AND device_generation=%s "
                "AND expires_at>%s AND NOT EXISTS(SELECT 1 FROM node_app_stage_cancellations c WHERE c.operation_id=node_app_operations.operation_id) ORDER BY created_at DESC LIMIT 1", (principal.grant.producer.device_id,
                principal.grant.producer.device_generation, self.sessions.clock.utc())).fetchall()
            commands = []
            for row in rows:
                command = parse_stage_command(bytes(row["command_payload"]))
                self._same_boot(principal, command)
                if effects:
                    self._command_current_in(conn, principal, row, command, gate)
                commands.append(json.loads(bytes(row["command_payload"])))
            return {"commands": commands, "scope": "app_effect" if effects else "preparation_read_only"}

    def artifact(self, session_id: UUID, credential: str, operation_id: UUID, role: str) -> OfferAsset:
        with self.sessions.db.transaction() as conn:
            principal = self.sessions.authenticate_in(conn, session_id, credential)
            if principal.grant.producer.owner not in {"app_manager", "app_effect_broker"}:
                raise NodeControlError("node_app_scope_required", 403)
            row, command = self._operation_in(conn, operation_id)
            self._same_boot(principal, command)
            if row["expires_at"] <= self.sessions.clock.utc() and not conn.execute(
                    "SELECT 1 FROM active_node_app_drains WHERE operation_id=%s", (operation_id,)).fetchone():
                raise NodeControlError("node_app_artifact_authority_expired", 410)
            ref = command.target if role == "target" else command.fallback if role == "fallback" else None
            if ref is None:
                raise NodeControlError("node_app_artifact_unavailable", 404)
            return OfferAsset("environment", "node-app", ref.environment_sha256,
                              ref.environment_sha256, ref.size_bytes, "sealed-environment-v2")

    def ready(self, session_id: UUID, credential: str, raw: bytes) -> bytes:
        ready = parse_stage_ready(raw)
        canonical = encode_stage_ready(ready)
        with self.sessions.db.transaction() as conn:
            located = conn.execute("SELECT rollout_generation FROM node_app_operations WHERE operation_id=%s",
                                   (ready.operation_id,)).fetchone()
            if located is None:
                raise NodeControlError("node_app_operation_unknown", 404)
            gate = self.gate.require_open_in(conn, expected_generation=located["rollout_generation"])
            acquire_runtime_locks(conn)
            principal = self.sessions.authenticate_in(conn, session_id, credential)
            row, command = self._operation_in(conn, ready.operation_id)
            self._command_current_in(conn, principal, row, command, gate)
            if (ready.producer, ready.command_session_id, ready.command_id, ready.command_sha256,
                ready.old_process, ready.old_app_epoch, ready.target_sha256, ready.fallback_sha256) != (
                command.producer, command.command_session_id, command.command_id, command.command_sha256,
                command.old_process, command.old_app_epoch, command.target.environment_sha256,
                command.fallback.environment_sha256 if command.fallback else None):
                raise NodeControlError("node_app_readiness_scope_mismatch")
            if not ready.roots_verified or not ready.capacity_available:
                raise NodeControlError("node_app_staging_not_ready")
            now = principal.admission.ensure_current(self.sessions.clock)
            local_now = principal.grant.expires_boottime_ms-int((principal.admission.expires_at-now)*1000)
            prior = conn.execute("SELECT payload FROM node_app_readiness WHERE request_id=%s",
                                 (ready.request_id,)).fetchone()
            if prior and bytes(prior["payload"]) != canonical:
                raise NodeControlError("node_app_readiness_identity_conflict")
            permit = conn.execute("SELECT payload,expires_at,ready_request_id FROM node_app_permits WHERE operation_id=%s",
                                  (ready.operation_id,)).fetchone()
            if permit:
                if permit["ready_request_id"] != ready.request_id or permit["expires_at"] <= now:
                    raise NodeControlError("node_app_permit_not_renewable")
                return bytes(permit["payload"])
            if not 0 <= local_now-ready.sampled_boottime_ms <= 5000:
                raise NodeControlError("node_app_readiness_stale")
            link = load_current_node_app_link_in(conn, principal)
            if link is None or (link.process, link.app_epoch, link.environment_sha256,
                    link.player_id, link.authority_epoch) != (command.old_process, command.old_app_epoch,
                    command.old_environment.environment_sha256, row["player_id"], row["authority_epoch"]):
                raise NodeControlError("node_app_old_process_changed")
            if conn.execute("SELECT 1 FROM bindings WHERE player_id=%s", (link.player_id,)).fetchone():
                raise NodeControlError("bound_drain_policy_unselected")
            if conn.execute("SELECT 1 FROM active_equipment_drains WHERE player_id=%s UNION ALL "
                            "SELECT 1 FROM active_node_app_drains WHERE player_id=%s",
                            (link.player_id, link.player_id)).fetchone():
                raise NodeControlError("node_app_existing_drain")
            if command.fallback is None:
                raise NodeControlError("node_app_qualified_fallback_required")
            boot = self._boot_in(conn, principal)
            cohort = current_cohort_in(conn, command.producer.device_id, command.producer.device_generation, now)
            if not conn.execute("SELECT 1 FROM node_environment_acceptances WHERE device_id=%s AND device_generation=%s "
                "AND base_content_key=%s AND environment_sha256=%s AND cohort=%s",
                (command.producer.device_id, command.producer.device_generation, boot.base.content_key,
                 command.fallback.environment_sha256, Jsonb(cohort))).fetchone():
                raise NodeControlError("node_app_fallback_cohort_changed")
            if prior is None:
                conn.execute("INSERT INTO node_app_readiness VALUES(%s,%s,%s,%s,%s,%s)",
                    (ready.request_id, ready.operation_id, ready.sequence, canonical, ready_digest(ready), now))
            outputs = conn.execute("SELECT output_id,observation FROM outputs WHERE player_id=%s ORDER BY output_id",
                                   (link.player_id,)).fetchall()
            if not outputs:
                raise NodeControlError("node_app_output_inventory_missing")
            drain_id = uuid4()
            snapshot = {"admission_scope": "unbound_v2", "outputs": [
                {"output_id": output["output_id"], "frame_id": None, "observation": output["observation"]}
                for output in outputs]}
            conn.execute("INSERT INTO node_app_drains VALUES(%s,%s,%s,%s,%s,%s)",
                (drain_id, ready.operation_id, link.player_id, link.authority_epoch, Jsonb(snapshot), now))
            duration = min(30, row["expires_at"]-now, principal.admission.expires_at-now)
            permit = StopPermitV2(command.producer, command.operation_id, command.command_id,
                command.command_sha256, command.command_session_id, uuid4(), drain_id, ready_digest(ready),
                command.old_process, command.old_app_epoch, local_now, local_now+int(duration*1000))
            result = encode_stop_permit(permit)
            conn.execute("INSERT INTO node_app_permits VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (command.operation_id, permit.permit_id, drain_id, ready.request_id, result, now, now+duration))
            gate.ensure_current_in(conn)
            principal.admission.ensure_current(self.sessions.clock)
            return result

    def effect(self, session_id: UUID, credential: str, raw: bytes) -> dict:
        event = parse_app_effect_event(raw)
        canonical = encode_app_effect_event(event)
        with self.sessions.db.transaction() as conn:
            acquire_runtime_locks(conn)
            principal = self.sessions.authenticate_in(conn, session_id, credential, allow_historical=True)
            row, command = self._operation_in(conn, event.operation_id)
            self._same_boot(principal, command)
            if (event.producer, event.command_session_id, event.command_id, event.command_sha256) != (
                    command.producer, command.command_session_id, command.command_id, command.command_sha256
                    ) or principal.grant.producer != event.producer:
                raise NodeControlError("node_app_effect_scope_mismatch")
            prior = conn.execute("SELECT payload,received_at FROM node_app_effects WHERE event_id=%s",
                                 (event.event_id,)).fetchone()
            if prior:
                if bytes(prior["payload"]) != canonical:
                    raise NodeControlError("node_app_effect_identity_conflict")
                return {"stored": True, "duplicate": True, "received_at": prior["received_at"],
                        "stage_closed": self._stage_closed_in(conn, event.operation_id), "authority_granted": False}
            permit = conn.execute("SELECT permit_id FROM node_app_permits WHERE operation_id=%s",
                                  (event.operation_id,)).fetchone()
            if event.permit_id is not None and (permit is None or permit["permit_id"] != event.permit_id):
                raise NodeControlError("node_app_effect_permit_mismatch")
            if event.phase in {"starting_new", "running", "fallback_starting", "fallback_running"}:
                expected = command.fallback if event.phase.startswith("fallback") else command.target
                if (expected is None or event.environment_sha256 != expected.environment_sha256
                        or event.process == command.old_process or event.app_epoch <= command.old_app_epoch):
                    raise NodeControlError("node_app_effect_environment_mismatch")
            if event.phase in {"no_stop_quiescent", "cancelled_before_stop"} and (event.process, event.app_epoch, event.environment_sha256) != (
                    command.old_process, command.old_app_epoch, command.old_environment.environment_sha256):
                raise NodeControlError("node_app_no_effect_old_process_changed")
            collision = conn.execute("SELECT 1 FROM node_app_effects WHERE operation_id=%s AND producer_id=%s "
                                     "AND sequence=%s", (event.operation_id, principal.producer_id, event.sequence)).fetchone()
            if collision:
                raise NodeControlError("node_app_effect_sequence_conflict")
            now = principal.admission.ensure_current(self.sessions.clock)
            claim_intake_in(conn, command.producer.device_id, "evidence", now)
            conn.execute("INSERT INTO node_app_effects VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (event.event_id, event.operation_id, principal.producer_id, event.sequence, event.phase, canonical, now))
            if event.phase == "cancelled_before_stop" and permit is None and principal.current:
                self._no_stop_in(conn, event.operation_id)
                self._sealed_journal_in(conn, event.operation_id, event)
                conn.execute("INSERT INTO node_app_stage_cancellations VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                             (event.operation_id, event.event_id, now))
            return {"stored": True, "duplicate": False, "received_at": now,
                    "authority_granted": False, "disposition": "current" if principal.current else "historical",
                    "stage_closed": self._stage_closed_in(conn, event.operation_id)}

    @staticmethod
    def _stage_closed_in(conn, operation_id):
        return conn.execute("SELECT 1 FROM node_app_stage_cancellations WHERE operation_id=%s",
                            (operation_id,)).fetchone() is not None

    @staticmethod
    def _no_stop_in(conn, operation_id):
        if conn.execute("SELECT 1 FROM node_app_effects WHERE operation_id=%s AND phase IN "
                        "('intent_stop','stopped','starting_new','running','target_failed',"
                        "'fallback_starting','fallback_running','effect_unknown') LIMIT 1",
                        (operation_id,)).fetchone():
            raise NodeControlError("node_app_stop_outcome_not_no_effect")

    @staticmethod
    def _sealed_journal_in(conn, operation_id, event):
        # The broker terminal seal forbids future local mutations. Central also
        # requires every prior effect event, not just the last-arriving claim.
        counts = conn.execute("SELECT count(*) AS count,min(sequence) AS first,max(sequence) AS last, "
                              "count(*) FILTER (WHERE phase=%s) AS seals "
                              "FROM node_app_effects WHERE operation_id=%s", (event.phase, operation_id)).fetchone()
        if (not event.executor_sealed or event.journal_watermark != event.sequence
                or counts["first"] != 1 or counts["last"] != event.journal_watermark
                or counts["count"] != event.journal_watermark or counts["seals"] != 1):
            raise NodeControlError("node_app_effect_journal_not_sealed_contiguous")

    def revalidate(self, session_id: UUID, credential: str, operation_id: UUID,
                   quiescent_event_id: UUID, revalidation_id: UUID) -> bytes:
        from central.fleet.acceptance_query import load_current_app_control_in
        from contracts.node_lifecycle import (
            RevalidationGrantV2,
            encode_revalidation_grant,
            parse_stop_permit,
        )
        for value in (operation_id, quiescent_event_id, revalidation_id):
            identifier(value)
        with self.sessions.db.transaction() as conn:
            acquire_runtime_locks(conn)
            principal = self.sessions.authenticate_in(conn, session_id, credential)
            row, command = self._operation_in(conn, operation_id)
            self._same_boot(principal, command)
            if principal.grant.producer != command.producer or principal.grant.scope != "app_effect":
                raise NodeControlError("node_app_revalidation_scope_changed")
            require_command_boot_in(conn, principal.grant.producer, principal.grant.offer_id)
            now = principal.admission.ensure_current(self.sessions.clock)
            self._no_stop_in(conn, operation_id)
            prior = conn.execute("SELECT * FROM node_app_revalidations WHERE revalidation_id=%s",
                                 (revalidation_id,)).fetchone()
            if prior:
                if (prior["operation_id"], prior["quiescent_event_id"], prior["carrier_session_id"]) != (
                        operation_id, quiescent_event_id, session_id):
                    raise NodeControlError("node_app_revalidation_identity_conflict")
                if prior["expires_at"] <= now:
                    raise NodeControlError("node_app_revalidation_expired", 410)
                return bytes(prior["payload"])
            if conn.execute("SELECT 1 FROM node_app_revalidations WHERE operation_id=%s", (operation_id,)).fetchone():
                raise NodeControlError("node_app_seal_revalidation_not_renewable")
            permit_row = conn.execute("SELECT * FROM node_app_permits WHERE operation_id=%s", (operation_id,)).fetchone()
            if permit_row is None or now <= permit_row["expires_at"]+2:
                raise NodeControlError("node_app_permit_expiry_margin_open")
            permit = parse_stop_permit(bytes(permit_row["payload"]))
            event_row = conn.execute("SELECT * FROM node_app_effects WHERE event_id=%s AND operation_id=%s",
                                    (quiescent_event_id, operation_id)).fetchone()
            if event_row is None or now-event_row["received_at"] > 5:
                raise NodeControlError("node_app_quiescence_unavailable")
            event = parse_app_effect_event(bytes(event_row["payload"]))
            self._sealed_journal_in(conn, operation_id, event)
            local_now = principal.grant.expires_boottime_ms-int((principal.admission.expires_at-now)*1000)
            if (event.phase != "no_stop_quiescent" or event.permit_id != permit.permit_id
                    or not permit.expires_boottime_ms+2000 < event.occurred_boottime_ms <= local_now
                    or local_now-event.occurred_boottime_ms > 5000):
                raise NodeControlError("node_app_quiescence_before_expiry_or_stale")
            link = load_current_node_app_link_in(conn, principal)
            if link is None or (link.process, link.app_epoch, link.environment_sha256, link.authority_epoch) != (
                    command.old_process, command.old_app_epoch, command.old_environment.environment_sha256,
                    row["authority_epoch"]):
                raise NodeControlError("node_app_revalidation_old_process_changed")
            control = load_current_app_control_in(conn, command.producer.device_id)
            if control is None or control.authority_epoch != row["authority_epoch"]:
                raise NodeControlError("node_app_revalidation_control_unavailable")
            if not conn.execute("SELECT 1 FROM active_node_app_drains WHERE operation_id=%s", (operation_id,)).fetchone():
                raise NodeControlError("node_app_drain_not_active")
            duration = min(60, principal.admission.expires_at-now)
            grant = RevalidationGrantV2(command.producer, operation_id, revalidation_id,
                quiescent_event_id, session_id, command.old_process, command.old_app_epoch,
                command.old_environment.environment_sha256, control.issued_sequence, local_now+int(duration*1000))
            result = encode_revalidation_grant(grant)
            conn.execute("INSERT INTO node_app_revalidations VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (revalidation_id, operation_id, quiescent_event_id, session_id, result,
                 control.issued_sequence, now, now+duration))
            return result

    def no_effect(self, session_id: UUID, credential: str, raw: bytes) -> dict:
        from central.fleet.acceptance_evidence import current_control_receipt_matches
        from central.fleet.acceptance_query import load_current_app_control_in
        from contracts.node_app_link import encode_node_app_link
        from contracts.node_lifecycle import parse_no_effect_proof
        from contracts.player_control import ControlAppliedReceipt
        proof = parse_no_effect_proof(raw)
        challenge = proof.app_link.challenge
        with self.sessions.db.transaction() as conn:
            acquire_runtime_locks(conn)
            principal = self.sessions.authenticate_in(conn, session_id, credential)
            row, command = self._operation_in(conn, proof.operation_id)
            self._same_boot(principal, command)
            if (principal.grant.producer != command.producer or challenge.producer != command.producer
                    or challenge.command_session_id != session_id):
                raise NodeControlError("node_app_no_effect_scope_changed")
            require_command_boot_in(conn, principal.grant.producer, principal.grant.offer_id)
            self._no_stop_in(conn, proof.operation_id)
            now = principal.admission.ensure_current(self.sessions.clock)
            grant = conn.execute("SELECT * FROM node_app_revalidations WHERE revalidation_id=%s AND operation_id=%s",
                                 (proof.revalidation_id, proof.operation_id)).fetchone()
            if (grant is None or grant["quiescent_event_id"] != proof.quiescent_event_id
                    or grant["carrier_session_id"] != session_id or grant["expires_at"] <= now):
                raise NodeControlError("node_app_revalidation_unavailable")
            current = conn.execute("SELECT payload,admitted_at FROM node_app_links WHERE nonce=%s AND superseded_at IS NULL",
                                   (challenge.nonce,)).fetchone()
            if current is None or bytes(current["payload"]) != encode_node_app_link(proof.app_link):
                raise NodeControlError("node_app_no_effect_proof_not_admitted")
            if (challenge.process, challenge.app_epoch, challenge.environment_sha256,
                challenge.player_id, challenge.authority_epoch) != (command.old_process,
                command.old_app_epoch, command.old_environment.environment_sha256,
                row["player_id"], row["authority_epoch"]):
                raise NodeControlError("node_app_no_effect_old_process_changed")
            control = load_current_app_control_in(conn, command.producer.device_id)
            receipt = ControlAppliedReceipt.model_validate_json(challenge.control_receipt)
            quiescent = conn.execute("SELECT payload FROM node_app_effects WHERE event_id=%s",
                                    (proof.quiescent_event_id,)).fetchone()
            event = parse_app_effect_event(bytes(quiescent["payload"]))
            self._sealed_journal_in(conn, proof.operation_id, event)
            if (not current_control_receipt_matches(receipt, control)
                    or control.applied_sequence <= grant["control_floor"]
                    or control.applied_at <= grant["issued_at"] or current["admitted_at"] <= grant["issued_at"]
                    or challenge.sampled_boottime_ms <= event.occurred_boottime_ms):
                raise NodeControlError("node_app_no_effect_proof_not_fresh")
            drain = conn.execute("SELECT drain_id FROM active_node_app_drains WHERE operation_id=%s",
                                 (proof.operation_id,)).fetchone()
            if drain is None:
                old = conn.execute("SELECT basis,evidence FROM node_app_discharges WHERE operation_id=%s",
                                   (proof.operation_id,)).fetchone()
                if old and old["basis"] == "authorized_no_effect" and old["evidence"].get("revalidation_id") == str(proof.revalidation_id):
                    return {"released": True, "duplicate": True, "basis": "authorized_no_effect"}
                raise NodeControlError("node_app_drain_not_active")
            conn.execute("INSERT INTO node_app_discharges VALUES(%s,%s,'authorized_no_effect',%s,%s)",
                (proof.operation_id, drain["drain_id"], Jsonb({"revalidation_id": str(proof.revalidation_id),
                    "quiescent_event_id": str(proof.quiescent_event_id), "proof_nonce": challenge.nonce,
                    "control_sequence": receipt.delivery_sequence}), now))
            return {"released": True, "duplicate": False, "basis": "authorized_no_effect"}

    def response(self, session_id: UUID, credential: str, raw: bytes) -> dict:
        from contracts.node_protocol import (
            NodeCommandResponseV2,
            encode_node_message,
            parse_node_message,
        )
        message = parse_node_message(raw)
        if type(message) is not NodeCommandResponseV2:
            raise ValueError("node_app_response_required")
        canonical = encode_node_message(message)
        with self.sessions.db.transaction() as conn:
            principal = self.sessions.authenticate_in(conn, session_id, credential, allow_historical=True)
            row = conn.execute("SELECT command_payload FROM node_app_operations WHERE command_id=%s",
                               (message.command_id,)).fetchone()
            if row is None:
                raise NodeControlError("node_app_command_unknown", 404)
            command = parse_stage_command(bytes(row["command_payload"]))
            if (message.producer, message.command_session_id, message.command_sha256, message.scope) != (
                    command.producer, command.command_session_id, command.command_sha256, "app_effect"
                    ) or principal.grant.producer != message.producer:
                raise NodeControlError("node_app_response_scope_changed")
            rows = conn.execute("SELECT decision,payload FROM node_app_responses WHERE command_id=%s",
                                (message.command_id,)).fetchall()
            for prior in rows:
                if prior["decision"] == message.decision:
                    if bytes(prior["payload"]) != canonical:
                        raise NodeControlError("node_app_response_identity_conflict")
                    return {"stored": True, "duplicate": True, "effect_established": False}
                if prior["decision"] in {"accepted", "rejected"} and message.decision in {"accepted", "rejected"}:
                    raise NodeControlError("node_app_response_terminal_conflict")
            conn.execute("INSERT INTO node_app_responses VALUES(%s,%s,%s,%s)",
                (message.command_id, message.decision, canonical, principal.admission.ensure_current(self.sessions.clock)))
            return {"stored": True, "duplicate": False, "effect_established": False}

    def observe_current_link_in(self, conn, principal) -> None:
        """Use the accepted-current-proof cut; never grant a new effect or no-effect proof."""
        if not holds_runtime_locks_in(conn):
            raise NodeControlError("node_lifecycle_runtime_locks_required")
        producer = principal.grant.producer
        operations = conn.execute(
            "SELECT d.operation_id FROM active_node_app_drains d "
            "JOIN node_app_operations o USING(operation_id) "
            "WHERE o.device_id=%s AND o.device_generation=%s "
            "ORDER BY d.prepared_at,d.operation_id LIMIT 32",
            (producer.device_id, producer.device_generation)).fetchall()
        for operation in operations:
            try:
                self._reconcile_in(conn, operation["operation_id"])
            except NodeControlError:
                continue  # A refused operational cut does not reject valid linkage.

    def reconcile(self, *, limit: int = 32) -> int:
        """Observe completed exact unbound switches; never dispatch an effect."""
        if self.sessions.config is None:
            return 0
        with self.sessions.db.transaction() as conn:
            operations = [row["operation_id"] for row in conn.execute(
                "SELECT operation_id FROM active_node_app_drains ORDER BY prepared_at LIMIT %s", (limit,)).fetchall()]
        count = 0
        for operation_id in operations:
            with self.sessions.db.transaction() as conn:
                acquire_runtime_locks(conn)
                row = conn.execute("SELECT device_id FROM node_app_operations WHERE operation_id=%s", (operation_id,)).fetchone()
                try:
                    self.sessions.lock_device_generation_in(conn, row["device_id"])
                    count += self._reconcile_in(conn, operation_id)
                except NodeControlError:
                    continue
        return count

    def _reconcile_in(self, conn, operation_id) -> int:
        from central.fleet.acceptance_evidence import current_control_receipt_matches
        from central.fleet.acceptance_query import load_current_app_control_in
        from contracts.player_control import ControlAppliedReceipt
        row, command = self._operation_in(conn, operation_id)
        current = conn.execute("SELECT session_id FROM node_sessions WHERE device_id=%s AND device_generation=%s "
            "AND owner='app_effect_broker' AND revoked_at IS NULL", (command.producer.device_id,
            command.producer.device_generation)).fetchone()
        if current is None:
            return 0
        principal = self.sessions.load_operator_target_in(conn, current["session_id"])
        if principal.grant.producer != command.producer:
            return 0
        require_command_boot_in(conn, principal.grant.producer, principal.grant.offer_id)
        latest = conn.execute("SELECT payload FROM node_app_effects WHERE operation_id=%s ORDER BY sequence DESC LIMIT 1",
                              (operation_id,)).fetchone()
        if latest is None:
            return 0
        event = parse_app_effect_event(bytes(latest["payload"]))
        if event.phase not in {"running", "fallback_running"}:
            return 0
        phases = conn.execute("SELECT phase,min(sequence) AS sequence FROM node_app_effects WHERE operation_id=%s "
                              "AND phase IN ('intent_stop','stopped') GROUP BY phase", (operation_id,)).fetchall()
        cuts = {item["phase"]: item["sequence"] for item in phases}
        if not (0 < cuts.get("intent_stop", 0) < cuts.get("stopped", 0) < event.sequence):
            return 0
        link = load_current_node_app_link_in(conn, principal)
        if (link is None or link.authority_epoch <= row["authority_epoch"]
                or (link.process, link.app_epoch, link.environment_sha256) != (
                    event.process, event.app_epoch, event.environment_sha256)):
            return 0
        control = load_current_app_control_in(conn, command.producer.device_id)
        if not current_control_receipt_matches(ControlAppliedReceipt.model_validate_json(link.control_receipt), control):
            return 0
        if conn.execute("SELECT 1 FROM bindings WHERE player_id=%s", (link.player_id,)).fetchone():
            raise NodeControlError("node_app_unbound_cut_changed")
        drain = conn.execute("SELECT drain_id FROM active_node_app_drains WHERE operation_id=%s", (operation_id,)).fetchone()
        if drain is None:
            return 0
        basis = "operational_fallback" if event.phase == "fallback_running" else "operational_target"
        conn.execute("INSERT INTO node_app_discharges VALUES(%s,%s,%s,%s,%s)",
            (operation_id, drain["drain_id"], basis, Jsonb({"effect_event_id": str(event.event_id),
                "proof_nonce": link.nonce, "authority_epoch": link.authority_epoch,
                "qualification": "unbound_control_only_not_rollback_acceptance"}), self.sessions.clock.utc()))
        return 1


    def permit_receipt(self, session_id: UUID, credential: str, operation_id: UUID) -> bytes:
        """Read immutable history; the recovery-only wrapper is never effect authority."""
        from contracts.node_lifecycle import (
            StopPermitReceiptV2,
            encode_stop_permit_receipt,
            parse_stop_permit,
        )
        with self.sessions.db.transaction() as conn:
            principal = self.sessions.authenticate_in(conn, session_id, credential)
            _, command = self._operation_in(conn, operation_id)
            self._same_boot(principal, command)
            if principal.grant.producer != command.producer or principal.grant.scope != "app_effect":
                raise NodeControlError("node_app_receipt_scope_changed")
            row = conn.execute("SELECT payload FROM node_app_permits WHERE operation_id=%s", (operation_id,)).fetchone()
            if row is None:
                raise NodeControlError("node_app_permit_unknown", 404)
            return encode_stop_permit_receipt(StopPermitReceiptV2(parse_stop_permit(bytes(row["payload"]))))

    def status(self, device_id: str) -> dict:
        """Owner-derived durable progress; transport response is never an effect."""
        self.sessions.require_enabled()
        with self.sessions.db.transaction() as conn:
            acquire_runtime_locks(conn)
            generation = self.sessions.lock_device_generation_in(conn, device_id)
            now = self.sessions.clock.utc()
            operations = conn.execute("SELECT o.*,p.permit_id,p.issued_at AS permit_issued_at,p.expires_at AS permit_expires_at,"
                "r.revalidation_id,r.expires_at AS revalidation_expires_at,d.basis AS discharge_basis,"
                "EXISTS(SELECT 1 FROM active_node_app_drains a WHERE a.operation_id=o.operation_id) AS fenced "
                "FROM node_app_operations o LEFT JOIN node_app_permits p USING(operation_id) "
                "LEFT JOIN node_app_revalidations r USING(operation_id) LEFT JOIN node_app_discharges d USING(operation_id) "
                "WHERE o.device_id=%s AND o.device_generation=%s ORDER BY o.created_at DESC LIMIT 25",
                (device_id, generation)).fetchall()
            results = []
            for operation in operations:
                event = conn.execute("SELECT phase,received_at,sequence,payload FROM node_app_effects "
                    "WHERE operation_id=%s ORDER BY sequence DESC LIMIT 1", (operation["operation_id"],)).fetchone()
                response = conn.execute("SELECT decision,received_at FROM node_app_responses WHERE command_id=%s ORDER BY received_at DESC LIMIT 1",
                                        (operation["command_id"],)).fetchone()
                state = "staging"
                if self._stage_closed_in(conn, operation["operation_id"]):
                    state = "cancelled_before_stop"
                elif operation["discharge_basis"] and not operation["fenced"]:
                    state = operation["discharge_basis"]
                elif operation["fenced"]:
                    state = "effect_unresolved"
                    if operation["revalidation_expires_at"] is not None:
                        state = "revalidating" if now < operation["revalidation_expires_at"] else "recovery_required"
                    elif operation["permit_expires_at"] <= now:
                        state = "awaiting_sealed_no_effect_or_recovery"
                elif operation["expires_at"] <= now:
                    state = "staging_expired"
                results.append({"operation_id": str(operation["operation_id"]),
                    "command_id": str(operation["command_id"]), "operator_audit_ref": operation["operator_audit_ref"],
                    "state": state, "runtime_fenced": operation["fenced"],
                    "command_response": dict(response) if response else None,
                    "latest_effect": {"phase": event["phase"], "sequence": event["sequence"],
                                      "received_at": event["received_at"]} if event else None,
                    "permit_expires_at": operation["permit_expires_at"],
                    "revalidation_expires_at": operation["revalidation_expires_at"],
                    "physical_output": "unknown", "artifact_roots_retained": True})
            return {"device_id": device_id, "generation": generation, "read_at": now, "operations": results}
