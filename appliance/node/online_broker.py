"""Restart-safe online app effect owner; preparation never implies stop authority."""
from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

from appliance.node.broker import RunningApp
from appliance.node.capacity import EMERGENCY_HEADROOM, memory_values
from appliance.node.clock import boottime_ms
from appliance.node.recovery import RESTORE_BUDGET_MS, STOP_BUDGET_MS, RecoveryObligation
from appliance.node.stop_operation import (
    StopGuaranteeUnavailable,
    StopRequest,
    stop_request_digest,
    stop_request_document,
    stop_request_from,
)
from contracts.node_lifecycle import (
    AppEffectEventV2,
    StageCommandV2,
    StageReadyV2,
    StopPermitReceiptV2,
    StopPermitV2,
    encode_app_effect_event,
    encode_stage_command,
    encode_stage_ready,
    encode_stop_permit,
    parse_app_effect_event,
    parse_stage_command,
    parse_stage_ready,
    ready_digest,
)
from contracts.node_protocol import NodeCommandResponseV2, encode_node_message
from contracts.strict_json import loads_object


class OnlineEffectBroker:
    def __init__(self, store, driver, session, recovery):
        self.store, self.driver, self.session = store, driver, session
        self.recovery = recovery

    @property
    def record(self):
        return self.store.read("online")

    def _save(self, record):
        self.store.write("online", record)

    def _bound(self, command: StageCommandV2) -> None:
        grant = self.session.grant
        if (grant is None or command.producer != grant.producer
                or command.command_session_id != grant.session_id
                or command.offer_id != grant.offer_id
                or boottime_ms() >= min(command.expires_boottime_ms, grant.expires_boottime_ms)):
            raise ValueError("online_command_binding")

    def _old(self, command: StageCommandV2) -> RunningApp:
        running = self.driver.current()
        if (running is None or running.process != command.old_process
                or running.app_epoch != command.old_app_epoch
                or running.environment != command.old_environment):
            raise ValueError("online_old_process_changed")
        return running

    def accept(self, command: StageCommandV2) -> None:
        self._bound(command)
        prior = self.record
        raw = encode_stage_command(command).decode()
        if prior is not None:
            if str(command.operation_id) in prior.get("sealed_operations", {}) and prior["command"] != raw:
                raise ValueError("online_executor_sealed")
            if prior["command"] == raw:
                return
            if prior["phase"] not in ("running", "fallback_running", "cancelled_before_stop", "no_stop_quiescent"):
                raise ValueError("online_prior_operation_unresolved")
            if prior["phase"] == "cancelled_before_stop" and not prior.get("stage_closed"):
                raise ValueError("online_cancellation_not_closed")
            if prior["phase"] == "no_stop_quiescent" and not prior.get("no_effect_released"):
                raise ValueError("online_no_effect_not_revalidated")
            if prior["pending"]:
                raise ValueError("online_prior_evidence_pending")
        self._old(command)
        response = NodeCommandResponseV2(command.producer, command.command_id, command.command_sha256,
            command.command_session_id, "app_effect", "accepted", "preparation_only")
        self._save({"command": raw, "phase": "preparing", "response": encode_node_message(response).decode(),
                    "response_pending": True, "ready": None,
                    "permit": None, "pending": [], "sequence": 0, "readiness_sequence": 0,
                    "sealed_operations": prior.get("sealed_operations", {}) if prior else {}})

    def ready(self) -> StageReadyV2:
        record = self.record
        if record is None or record["phase"] not in ("preparing", "awaiting_permit"):
            raise ValueError("online_readiness_phase")
        command = parse_stage_command(record["command"].encode())
        self._bound(command)
        self._old(command)
        for reference in (command.target, command.fallback):
            if reference is not None and self.driver.verify(reference) is not True:
                raise ValueError("online_root_unverified")
        _, available = memory_values()
        if available <= EMERGENCY_HEADROOM:
            raise ValueError("online_runtime_capacity")
        ready = StageReadyV2(command.producer, command.operation_id, command.command_id,
                             command.command_sha256, command.command_session_id, uuid4(),
                             record.get("readiness_sequence", 0) + 1, boottime_ms(), command.old_process,
                             command.old_app_epoch, command.target.environment_sha256,
                             command.fallback.environment_sha256 if command.fallback else None,
                             True, True)
        self._save({**record, "ready": encode_stage_ready(ready).decode(), "phase": "awaiting_permit", "readiness_sequence": ready.sequence})
        return ready

    def cancel_expired(self, *, worker_quiescent: bool) -> bool:
        record = self.record
        if (record is None or record.get("executor_sealed") or record.get("permit")
                or record["phase"] not in ("preparing", "awaiting_permit") or record["pending"]):
            return False
        command = parse_stage_command(record["command"].encode())
        if boottime_ms() < command.expires_boottime_ms or not worker_quiescent:
            return False
        old = self._old(command)
        if not self.driver.quiescent(old):
            return False
        self._event("cancelled_before_stop", running=old)
        return True

    def _event(self, phase: str, *, running=None, fault=None, stop_request=None, recovery=None):
        record = self.record
        seal = phase in ("cancelled_before_stop", "no_stop_quiescent")
        # A cancellation seal may add one later post-permit-expiry observation;
        # neither permits any future effect. It retains the same terminal seal.
        observation_after_cancel = (phase == "no_stop_quiescent" and record.get("stage_cancelled")
                                    and not record.get("seal_event"))
        if record.get("executor_sealed") and not observation_after_cancel:
            raise ValueError("online_executor_sealed")
        if seal and record.get("intent_stop_written"):
            raise ValueError("online_stop_intent_not_no_effect")
        if seal and record["pending"]:
            raise ValueError("online_unreported_journal")
        command = parse_stage_command(record["command"].encode())
        stop_metadata = {}
        if phase == "intent_stop":
            if (type(stop_request) is not StopRequest or type(recovery) is not RecoveryObligation
                    or stop_request.operation_id != command.operation_id
                    or stop_request.command_sha256 != command.command_sha256
                    or recovery.operation_id != stop_request.operation_id
                    or recovery.boot_id != stop_request.boot_id
                    or recovery.request_sha256 != stop_request_digest(stop_request)):
                raise ValueError("online_stop_intent_binding")
            stop_metadata = {"stop_request": stop_request_document(stop_request),
                             "recovery": recovery.document()}
        elif stop_request is not None or recovery is not None:
            raise ValueError("online_stop_intent_phase")
        if len(record["pending"]) >= 128:
            raise ValueError("online_evidence_capacity")
        permit = record.get("permit_id")
        event = AppEffectEventV2(command.producer, command.operation_id, command.command_id,
            command.command_sha256, command.command_session_id, uuid4(), record["sequence"] + 1,
            boottime_ms(), phase, UUID(permit) if permit else None,
            running.process if running else None, running.app_epoch if running else None,
            running.environment.environment_sha256 if running else None, fault,
            executor_sealed=seal,
            journal_watermark=record["sequence"] + 1 if seal else None)
        seals = record.get("sealed_operations", {})
        if seal:
            if str(command.operation_id) not in seals and len(seals) >= 1024:
                raise ValueError("online_seal_capacity")
            seals = {**seals, str(command.operation_id): command.command_sha256}
        seal_metadata = ({"quiescent_event_id": str(event.event_id), "quiescent_at": event.occurred_boottime_ms,
                          "revalidation_id": str(uuid4()), "seal_event": encode_app_effect_event(event).decode()}
                         if phase == "no_stop_quiescent" else {})
        self._save({**record, **seal_metadata, **stop_metadata, "phase": phase, "sequence": event.sequence,
                    "intent_stop_written": record.get("intent_stop_written", False) or phase == "intent_stop",
                    "stop_consumed": record.get("stop_consumed", False) or phase == "stopped",
                    "executor_sealed": record.get("executor_sealed", False) or seal, "sealed_operations": seals,
                    "stage_cancelled": record.get("stage_cancelled", False) or phase == "cancelled_before_stop",
                    "pending": [*record["pending"], encode_app_effect_event(event).decode()]})
        return event

    def retain_permit_receipt(self, receipt: StopPermitReceiptV2) -> None:
        if type(receipt) is not StopPermitReceiptV2 or not receipt.recovery_only:
            raise ValueError("online_recovery_receipt_required")
        record = self.record
        command = parse_stage_command(record["command"].encode())
        permit = receipt.permit
        if (permit.producer != command.producer or permit.operation_id != command.operation_id
                or permit.command_id != command.command_id or permit.command_sha256 != command.command_sha256
                or permit.command_session_id != command.command_session_id
                or permit.old_process != command.old_process or permit.old_app_epoch != command.old_app_epoch
                or record["ready"] is None or permit.ready_sha256 != ready_digest(parse_stage_ready(record["ready"].encode()))):
            raise ValueError("online_recovery_receipt_binding")
        encoded = encode_stop_permit(permit).decode()
        if record.get("permit") not in (None, encoded):
            raise ValueError("online_recovery_permit_conflict")
        if record["phase"] not in ("awaiting_permit", "awaiting_recovery", "cancelled_before_stop"):
            return
        self._save({**record, "permit": encoded, "permit_id": str(permit.permit_id),
                    "permit_recovery_only": True, "phase": "awaiting_recovery"})

    def execute(self, permit: StopPermitV2) -> None:
        record = self.record
        if record is None or record.get("executor_sealed") or record.get("permit_recovery_only") or record["phase"] != "awaiting_permit":
            raise ValueError("online_stop_not_ready")
        command = parse_stage_command(record["command"].encode())
        ready = parse_stage_ready(record["ready"].encode())
        if str(command.operation_id) in record.get("sealed_operations", {}):
            raise ValueError("online_executor_sealed")
        self._bound(command)
        if (permit.producer != command.producer or permit.operation_id != command.operation_id
                or permit.command_id != command.command_id or permit.command_sha256 != command.command_sha256
                or permit.command_session_id != command.command_session_id
                or permit.ready_sha256 != ready_digest(ready)
                or permit.old_process != command.old_process or permit.old_app_epoch != command.old_app_epoch):
            raise ValueError("online_stop_permit_binding")
        encoded_permit = encode_stop_permit(permit).decode()
        if record.get("permit") not in (None, encoded_permit):
            raise ValueError("online_stop_permit_conflict")
        self._save({**record, "permit": encoded_permit, "permit_id": str(permit.permit_id)})
        if not permit.issued_boottime_ms <= boottime_ms() < permit.expires_boottime_ms:
            return
        if command.fallback is None:
            raise ValueError("online_accepted_fallback_required")
        for reference in (command.target, command.fallback):
            if self.driver.verify(reference) is not True:
                raise ValueError("online_root_unverified")
        self._bound(command)
        old = self._old(command)
        if boottime_ms() >= permit.expires_boottime_ms:
            return
        request = StopRequest(command.operation_id, command.producer.kernel_boot_id,
            command.command_sha256, permit.permit_id, hashlib.sha256(encode_stop_permit(permit)).hexdigest(),
            old, min(permit.expires_boottime_ms, command.expires_boottime_ms,
                     self.session.grant.expires_boottime_ms))
        now = boottime_ms()
        obligation = RecoveryObligation(request.boot_id, request.operation_id,
            stop_request_digest(request), old.process, old.app_epoch,
            old.environment.environment_sha256, command.target.environment_sha256,
            command.fallback.environment_sha256, now, now + STOP_BUDGET_MS,
            now + STOP_BUDGET_MS + RESTORE_BUDGET_MS)
        # One durable transition binds the immutable recovery deadlines and request
        # to intent. A crash cannot leave awaiting_permit with replaceable deadlines.
        self._event("intent_stop", running=old, stop_request=request, recovery=obligation)
        # Host acknowledgment is required before dispatch. Lost acknowledgment is
        # recovered by immutable replay; neither owner can extend the deadlines.
        self._arm_recovery(obligation)
        try:
            operation = self.driver.stop(request, reattach_only=False)
            self._consume_stop(operation, command)
        except StopGuaranteeUnavailable as error:
            self._stop_fault(error)

    def _arm_recovery(self, obligation):
        if self.recovery.arm(obligation) != obligation.receipt:
            raise ValueError("online_recovery_receipt")

    def _stop_fault(self, error):
        record = self.record
        diagnostic = {"code": error.code, "diagnostic_id": error.diagnostic_id}
        if record.get("stop_failure") != diagnostic:
            self._save({**record, "stop_failure": diagnostic})
        if self.record["phase"] != "effect_unknown":
            self._event("effect_unknown", fault=error.code)

    def _consume_stop(self, operation, command):
        # The persisted lifecycle transition owns exactly-once consumption.
        if self.record.get("stop_consumed") or not operation.done():
            return
        completed = operation.result()
        if stop_request_document(completed.request) != self.record["stop_request"]:
            raise StopGuaranteeUnavailable("stop_completion_mismatch")
        self._event("stopped", running=completed.request.old)
        self._start(command, command.target, fallback=False)

    def service(self):
        self.driver.service(now_ms=boottime_ms(), budget_ms=100)
        self.reconcile()
        record = self.record
        if record is None or "recovery" not in record:
            return
        obligation = RecoveryObligation.parse(record["recovery"])
        proof = self.store.read("local-app-control")
        if record["phase"] in ("running", "fallback_running") and proof is not None:
            if proof["operation_id"] == str(obligation.operation_id):
                self.recovery.advance(obligation, proof["progress"])
        elif record.get("stop_consumed"):
            self.recovery.advance(obligation, {"kind": "stopped"})

    def _start(self, command, reference, *, fallback: bool):
        record = self.record
        if record.get("executor_sealed") or str(command.operation_id) in record.get("sealed_operations", {}):
            raise ValueError("online_executor_sealed")
        if record.get("recovery"):
            self._arm_recovery(RecoveryObligation.parse(record["recovery"]))
        # The intent is durable before selection/spawn, including fallback budget.
        self._save({**record, "phase": "fallback_intent" if fallback else "target_intent"})
        try:
            if not self.driver.absent_and_quiescent():
                raise ValueError("online_start_not_quiescent")
            self.driver.select(reference)
            running = self.driver.start(reference, command.operation_id)
            if running.environment != reference or running.operation_id != command.operation_id:
                raise ValueError("online_started_identity_mismatch")
        except Exception:
            if not fallback and self.driver.absent_and_quiescent():
                self._event("target_failed", fault="target_start_failed")
                self._start(command, command.fallback, fallback=True)
            else:
                self._event("effect_unknown", fault="fallback_failed" if fallback else "start_outcome_unknown")
            return
        self._event("fallback_starting" if fallback else "starting_new", running=running)
        self._event("fallback_running" if fallback else "running", running=running)

    def reconcile(self) -> None:
        record = self.record
        if record is None or record.get("executor_sealed") or record["phase"] in ("preparing", "awaiting_permit", "awaiting_recovery", "running", "fallback_running", "cancelled_before_stop", "no_stop_quiescent"):
            return
        command = parse_stage_command(record["command"].encode())
        if record.get("intent_stop_written") and not record.get("stop_consumed") and record.get("stop_request"):
            request = stop_request_from(record["stop_request"])
            self._arm_recovery(RecoveryObligation.parse(record["recovery"]))
            try:
                self._consume_stop(self.driver.stop(request, reattach_only=True), command)
            except StopGuaranteeUnavailable as error:
                self._stop_fault(error)
            return
        running = self.driver.current()
        if running is not None and running.operation_id == command.operation_id:
            if running.environment == command.target:
                self._event("running", running=running)
                return
            if running.environment == command.fallback:
                self._event("fallback_running", running=running)
                return
        if running is None and self.driver.absent_and_quiescent():
            if record["phase"] == "stopped":
                self._start(command, command.target, fallback=False)
                return
            if record["phase"] == "target_intent" and command.fallback is not None:
                self._event("target_failed", fault="target_absent_after_restart")
                self._start(command, command.fallback, fallback=True)
                return
        # Ambiguous stop or spawn is never blindly retried after a broker restart.
        if record["phase"] != "effect_unknown":
            self._event("effect_unknown", fault="restart_reconciliation_required")

    def flush(self) -> None:
        record = self.record
        if record is not None and record.get("response_pending"):
            status, _ = self.session.transport.request("POST", "/v2/node/app-responses", record["response"].encode(), self.session.claim)
            if status == 200:
                self._save({**record, "response_pending": False})
        for _ in range(4):
            record = self.record
            if record is None or not record["pending"]:
                return
            status, raw = self.session.transport.request("POST", "/v2/node/app-effects",
                record["pending"][0].encode(), self.session.claim)
            if status != 200:
                return
            event = parse_app_effect_event(record["pending"][0].encode())
            receipt = {}
            if event.phase == "cancelled_before_stop":
                response = loads_object(raw, max_bytes=8192)
                if response is None or type(response.get("stage_closed")) is not bool:
                    raise ValueError("online_cancellation_receipt_invalid")
                receipt = {"stage_closed": response["stage_closed"]}
            self._save({**record, **receipt, "pending": record["pending"][1:]})
