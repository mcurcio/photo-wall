"""V2 app switch desired state; the Player converges locally and reports effects.

A stage row is desired state and the latest stage for a device wins. A new
operation never resolves mutable desired policy after issue. Central does not
authorize the stop: the broker prepares, verifies roots, stops the old process it
was told about, starts the target, falls back if needed, and reports each effect.
Central projects operation state from the latest reported effect. Exact roots
remain retained by their published deployment/acceptance owners. Frame-bound
Players are refused at stage time until a bound switch policy is selected.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from uuid import UUID

from psycopg.types.json import Jsonb

from central.fleet.models import OfferAsset
from central.fleet.node_acceptance import current_cohort_in
from central.fleet.node_app_links import load_current_node_app_link_in
from central.fleet.node_boot import parse_node_deployment
from central.fleet.node_sessions import NodeControlError, NodeSessions, claim_intake_in
from central.fleet.rollout_gate import RolloutEffectGate
from central.transaction_locks import acquire_runtime_locks
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_boot import parse_node_boot_offer
from contracts.node_lifecycle import (
    RUNNING_PHASES,
    StageCommandV2,
    encode_app_effect_event,
    encode_stage_command,
    parse_app_effect_event,
    parse_stage_command,
    stage_digest,
)
from contracts.node_protocol import counter, identifier, token

# Projection of an operation's latest reported effect phase. Phases not listed
# (intent_stop, stopped, starting_new, target_failed, fallback_starting) are mid-switch.
_TERMINAL_STATES = {"running": "target_running", "fallback_running": "fallback_running",
                    "effect_unknown": "effect_unknown"}


@dataclass(frozen=True, slots=True)
class OperatorAppStage:
    operation_id: UUID
    command_id: UUID
    session_id: UUID
    device_generation: int
    deployment_id: UUID
    rollout_generation: int
    operator_audit_ref: str

    def __post_init__(self):
        for value in (self.operation_id, self.command_id, self.session_id, self.deployment_id):
            identifier(value)
        counter(self.device_generation, 1)
        counter(self.rollout_generation, 1)
        token(self.operator_audit_ref, 256)


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
    def _boot_of(producer):
        return (producer.installation_audience, producer.device_id, producer.device_generation,
                producer.kernel_boot_id)

    def _same_boot(self, principal, command):
        if self._boot_of(principal.grant.producer) != self._boot_of(command.producer):
            raise NodeControlError("node_app_operation_scope_changed", 403)

    def _command_current_in(self, conn, principal, row, command, gate):
        """The command targets this exact broker producer; session renewal keeps it current."""
        self._same_boot(principal, command)
        if (principal.grant.scope != "app_effect" or principal.grant.producer != command.producer
                or row["rollout_generation"] != gate.generation or row["rollout_scope_sha256"] != gate.scope_sha256):
            raise NodeControlError("node_app_command_scope_changed", 403)
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
            now = principal.admission.ensure_current(self.sessions.clock)
            old = conn.execute("SELECT * FROM node_app_operations WHERE operation_id=%s",
                               (request.operation_id,)).fetchone()
            if old:
                if old["request_sha256"] != request_hash:
                    raise NodeControlError("node_app_operation_identity_conflict")
                command = parse_stage_command(bytes(old["command_payload"]))
                self._command_current_in(conn, principal, old, command, gate)
                return {"command": json.loads(bytes(old["command_payload"])), "duplicate": True}
            link = load_current_node_app_link_in(conn, principal)
            if link is None:
                raise NodeControlError("node_app_current_process_unlinked")
            # Owner default (Q1): a Frame-bound Player is refused here; a Frame bound
            # after staging follows the reboot rule from the observed process exit.
            if conn.execute("SELECT 1 FROM bindings WHERE player_id=%s", (link.player_id,)).fetchone():
                raise NodeControlError("bound_switch_policy_unselected")
            if conn.execute("SELECT 1 FROM active_equipment_drains WHERE player_id=%s",
                            (link.player_id,)).fetchone():
                raise NodeControlError("node_app_existing_drain")
            boot = self._boot_in(conn, principal)
            publication = conn.execute("SELECT document FROM node_deployments WHERE deployment_id=%s",
                                       (request.deployment_id,)).fetchone()
            if publication is None:
                raise NodeControlError("node_deployment_unknown", 404)
            deployment = parse_node_deployment(bytes(publication["document"]))
            if deployment.base != boot.base or deployment.app_environment is None:
                raise NodeControlError("node_app_target_base_mismatch")
            old_environment = self._reference_in(conn, link.environment_sha256)
            fallback = self._qualified_fallback_in(conn, device_id, producer.device_generation,
                                                   boot.base.content_key,
                                                   deployment.app_environment.environment_sha256, now)
            command = StageCommandV2(request.operation_id, request.command_id, "0"*64, producer,
                request.session_id, principal.grant.offer_id, link.process, link.app_epoch, old_environment,
                deployment.app_environment, fallback)
            command = replace(command, command_sha256=stage_digest(command))
            claim_intake_in(conn, device_id, "command", now)
            conn.execute("INSERT INTO node_app_operations(operation_id,command_id,device_id,device_generation,"
                "player_id,authority_epoch,producer_id,session_id,deployment_id,request_sha256,command_sha256,"
                "command_payload,rollout_generation,rollout_scope_sha256,created_at,operator_audit_ref) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (request.operation_id, request.command_id, device_id, producer.device_generation, link.player_id,
                 link.authority_epoch, principal.producer_id, request.session_id, request.deployment_id, request_hash,
                 command.command_sha256, encode_stage_command(command), gate.generation, gate.scope_sha256, now,
                 request.operator_audit_ref))
            gate.ensure_current_in(conn)
            principal.admission.ensure_current(self.sessions.clock)
            return {"command": json.loads(encode_stage_command(command)), "duplicate": False}

    def _qualified_fallback_in(self, conn, device_id, generation, base_content_key, target_sha256, now):
        """The broker restores this accepted environment if the target fails to start."""
        try:
            cohort = current_cohort_in(conn, device_id, generation, now)
        except NodeControlError as exc:
            raise NodeControlError("node_app_qualified_fallback_required") from exc
        accepted = conn.execute("SELECT environment_sha256 FROM node_environment_acceptances "
            "WHERE device_id=%s AND device_generation=%s AND base_content_key=%s AND cohort=%s "
            "AND environment_sha256<>%s ORDER BY accepted_at DESC LIMIT 1",
            (device_id, generation, base_content_key, Jsonb(cohort), target_sha256)).fetchone()
        if accepted is None:
            raise NodeControlError("node_app_qualified_fallback_required")
        return self._reference_in(conn, accepted["environment_sha256"])

    def desired(self, session_id: UUID, credential: str, *, effects: bool = False) -> dict:
        """The latest stage for this device is desired; an earlier boot's stage is not."""
        self.sessions.require_enabled()
        with self.sessions.db.transaction() as conn:
            located = conn.execute("SELECT rollout_generation FROM node_app_operations o JOIN node_sessions s "
                "ON s.device_id=o.device_id AND s.device_generation=o.device_generation WHERE s.session_id=%s "
                "ORDER BY o.sequence DESC LIMIT 1", (session_id,)).fetchone()
            gate = (self.gate.require_open_in(conn, expected_generation=located["rollout_generation"])
                    if effects and located else None)
            principal = self.sessions.authenticate_in(conn, session_id, credential)
            owner = "app_effect_broker" if effects else "app_manager"
            if principal.grant.producer.owner != owner:
                raise NodeControlError("node_app_scope_required", 403)
            row = conn.execute("SELECT * FROM node_app_operations WHERE device_id=%s AND device_generation=%s "
                "ORDER BY sequence DESC LIMIT 1", (principal.grant.producer.device_id,
                principal.grant.producer.device_generation)).fetchone()
            commands = []
            if row is not None:
                command = parse_stage_command(bytes(row["command_payload"]))
                if self._boot_of(command.producer) == self._boot_of(principal.grant.producer) and (
                        not effects or command.producer == principal.grant.producer):
                    if effects:
                        self._command_current_in(conn, principal, row, command, gate)
                    commands.append(json.loads(bytes(row["command_payload"])))
            return {"commands": commands, "scope": "app_effect" if effects else "preparation_read_only"}

    def artifact(self, session_id: UUID, credential: str, operation_id: UUID, role: str) -> OfferAsset:
        with self.sessions.db.transaction() as conn:
            principal = self.sessions.authenticate_in(conn, session_id, credential)
            if principal.grant.producer.owner not in {"app_manager", "app_effect_broker"}:
                raise NodeControlError("node_app_scope_required", 403)
            _, command = self._operation_in(conn, operation_id)
            self._same_boot(principal, command)
            ref = command.target if role == "target" else command.fallback if role == "fallback" else None
            if ref is None:
                raise NodeControlError("node_app_artifact_unavailable", 404)
            return OfferAsset("environment", "node-app", ref.environment_sha256,
                              ref.environment_sha256, ref.size_bytes, "sealed-environment-v2")

    def effect(self, session_id: UUID, credential: str, raw: bytes) -> dict:
        """Record one reported local effect; it is evidence, never authority."""
        event = parse_app_effect_event(raw)
        canonical = encode_app_effect_event(event)
        with self.sessions.db.transaction() as conn:
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
                return {"stored": True, "duplicate": True, "received_at": prior["received_at"]}
            if event.phase in RUNNING_PHASES:
                expected = command.fallback if event.phase.startswith("fallback") else command.target
                if (event.environment_sha256 != expected.environment_sha256
                        or event.process == command.old_process or event.app_epoch <= command.old_app_epoch):
                    raise NodeControlError("node_app_effect_environment_mismatch")
            collision = conn.execute("SELECT 1 FROM node_app_effects WHERE operation_id=%s AND producer_id=%s "
                                     "AND sequence=%s", (event.operation_id, principal.producer_id, event.sequence)).fetchone()
            if collision:
                raise NodeControlError("node_app_effect_sequence_conflict")
            now = principal.admission.ensure_current(self.sessions.clock)
            claim_intake_in(conn, command.producer.device_id, "evidence", now)
            conn.execute("INSERT INTO node_app_effects VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (event.event_id, event.operation_id, principal.producer_id, event.sequence, event.phase, canonical, now))
            return {"stored": True, "duplicate": False, "received_at": now,
                    "disposition": "current" if principal.current else "historical"}

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

    def status(self, device_id: str) -> dict:
        """Project the latest operation from its latest reported effect and every earlier one as
        superseded; transport is never an effect."""
        self.sessions.require_enabled()
        with self.sessions.db.transaction() as conn:
            generation = self.sessions.lock_device_generation_in(conn, device_id)
            now = self.sessions.clock.utc()
            current = conn.execute("SELECT kernel_boot_id FROM node_boot_admissions WHERE device_id=%s "
                                   "AND device_generation=%s AND superseded_at IS NULL",
                                   (device_id, generation)).fetchone()
            operations = conn.execute("SELECT * FROM node_app_operations WHERE device_id=%s "
                "AND device_generation=%s ORDER BY sequence DESC LIMIT 25", (device_id, generation)).fetchall()
            results = []
            for index, operation in enumerate(operations):
                event = conn.execute("SELECT phase,received_at,sequence FROM node_app_effects "
                    "WHERE operation_id=%s ORDER BY sequence DESC LIMIT 1", (operation["operation_id"],)).fetchone()
                response = conn.execute("SELECT decision,received_at FROM node_app_responses WHERE command_id=%s ORDER BY received_at DESC LIMIT 1",
                                        (operation["command_id"],)).fetchone()
                command = parse_stage_command(bytes(operation["command_payload"]))
                # A later stage replaces this one whatever it reported; latest_effect keeps that detail.
                state = ("superseded" if index > 0
                         else _TERMINAL_STATES.get(event["phase"], "switching") if event else "staged")
                rebooted = current is not None and current["kernel_boot_id"] != command.producer.kernel_boot_id
                if rebooted and state in ("staged", "switching", "effect_unknown"):
                    state = "interrupted_by_reboot"
                results.append({"operation_id": str(operation["operation_id"]),
                    "command_id": str(operation["command_id"]), "operator_audit_ref": operation["operator_audit_ref"],
                    "state": state, "command_response": dict(response) if response else None,
                    "latest_effect": {"phase": event["phase"], "sequence": event["sequence"],
                                      "received_at": event["received_at"]} if event else None,
                    "physical_output": "unknown", "artifact_roots_retained": True})
            return {"device_id": device_id, "generation": generation, "read_at": now, "operations": results}
