"""Restart-safe online app effect owner: converge locally on the desired stage.

The broker is the single writer of the local intent journal. Once roots are
verified it checks the old process, journals intent, arms the host recovery
obligation, owns the stop, then starts the target and falls back if needed. Each
transition is an effect event queued for Central; reporting never gates progress.
"""
from __future__ import annotations

import logging
from uuid import uuid4

from appliance.central_session.session import REFUSED
from appliance.clock import boottime_ms
from appliance.node.broker import RunningApp
from appliance.node.capacity import EMERGENCY_HEADROOM, memory_values
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
    encode_app_effect_event,
    encode_stage_command,
    parse_stage_command,
)
from contracts.node_protocol import NodeCommandResponseV2, encode_node_message

# A newer desired stage replaces the current record only before its stop intent
# or after the switch completed. A stop or start in flight always finishes first;
# an ambiguous outcome (effect_unknown) belongs to the host recovery obligation.
_REPLACEABLE = ("preparing", "running", "fallback_running")
LOG = logging.getLogger(__name__)


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
        if grant is None or command.producer != grant.producer or command.offer_id != grant.offer_id:
            raise ValueError("online_command_binding")

    def _old(self, command: StageCommandV2) -> RunningApp:
        running = self.driver.current()
        if (running is None or running.process != command.old_process
                or running.app_epoch != command.old_app_epoch
                or running.environment != command.old_environment):
            raise ValueError("online_old_process_changed")
        return running

    def accept(self, command: StageCommandV2) -> None:
        """Adopt the latest desired stage; unreported events of a prior stage stay queued."""
        self._bound(command)
        prior = self.record
        raw = encode_stage_command(command).decode()
        if prior is not None:
            if prior["command"] == raw:
                return
            if prior["phase"] not in _REPLACEABLE:
                raise ValueError("online_prior_operation_unresolved")
        self._old(command)
        response = NodeCommandResponseV2(command.producer, command.command_id, command.command_sha256,
            command.command_session_id, "app_effect", "accepted", "switch_on_preparation")
        self._save({"command": raw, "phase": "preparing", "response": encode_node_message(response).decode(),
                    "response_pending": True, "pending": prior["pending"] if prior else [], "sequence": 0})

    def execute(self) -> None:
        """Switch once roots are verified: _old → intent_stop → owned stop → target → fallback."""
        record = self.record
        if record is None or record["phase"] != "preparing":
            raise ValueError("online_switch_not_preparing")
        command = parse_stage_command(record["command"].encode())
        for reference in (command.target, command.fallback):
            if self.driver.verify(reference) is not True:
                raise ValueError("online_root_unverified")
        _, available = memory_values()
        if available <= EMERGENCY_HEADROOM:
            raise ValueError("online_runtime_capacity")
        old = self._old(command)
        now = boottime_ms()
        request = StopRequest(command.operation_id, command.producer.kernel_boot_id,
                              command.command_sha256, old, now + STOP_BUDGET_MS)
        obligation = RecoveryObligation(request.boot_id, request.operation_id,
            stop_request_digest(request), old.process, old.app_epoch,
            old.environment.environment_sha256, command.target.environment_sha256,
            command.fallback.environment_sha256, now, now + STOP_BUDGET_MS,
            now + STOP_BUDGET_MS + RESTORE_BUDGET_MS)
        # One durable transition binds the immutable recovery deadlines and request
        # to intent. A crash cannot leave preparing with replaceable deadlines.
        self._event("intent_stop", running=old, stop_request=request, recovery=obligation)
        # Host acknowledgment is required before dispatch. Lost acknowledgment is
        # recovered by immutable replay; neither owner can extend the deadlines.
        self._arm_recovery(obligation)
        try:
            operation = self.driver.stop(request, reattach_only=False)
            self._consume_stop(operation, command)
        except StopGuaranteeUnavailable as error:
            self._stop_fault(error)

    def _event(self, phase: str, *, running=None, fault=None, stop_request=None, recovery=None):
        record = self.record
        command = parse_stage_command(record["command"].encode())
        stop_metadata = {}
        if phase == "intent_stop":
            if (record.get("intent_stop_written") or type(stop_request) is not StopRequest
                    or type(recovery) is not RecoveryObligation
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
        event = AppEffectEventV2(command.producer, command.operation_id, command.command_id,
            command.command_sha256, command.command_session_id, uuid4(), record["sequence"] + 1,
            boottime_ms(), phase, running.process if running else None,
            running.app_epoch if running else None,
            running.environment.environment_sha256 if running else None, fault)
        self._save({**record, **stop_metadata, "phase": phase, "sequence": event.sequence,
                    "intent_stop_written": record.get("intent_stop_written", False) or phase == "intent_stop",
                    "stop_consumed": record.get("stop_consumed", False) or phase == "stopped",
                    "pending": [*record["pending"], encode_app_effect_event(event).decode()]})
        return event

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
        if record is None or record["phase"] in ("preparing", "running", "fallback_running"):
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
            if record["phase"] == "target_intent":
                self._event("target_failed", fault="target_absent_after_restart")
                self._start(command, command.fallback, fallback=True)
                return
        # Ambiguous stop or spawn is never blindly retried after a broker restart.
        if record["phase"] != "effect_unknown":
            self._event("effect_unknown", fault="restart_reconciliation_required")

    def flush(self) -> None:
        """Report queued response and effects in order; an unreachable Central only delays them
        and an event Central permanently refuses is dropped, never blocking the ones behind it."""
        record = self.record
        if record is not None and record.get("response_pending"):
            status, _ = self.session.request("POST", "/v2/node/app-responses", record["response"].encode())
            if status == 200:
                self._save({**record, "response_pending": False})
        for _ in range(4):
            record = self.record
            if record is None or not record["pending"]:
                return
            status, _ = self.session.request("POST", "/v2/node/app-effects", record["pending"][0].encode())
            if status == 200:
                self._save({**record, "pending": record["pending"][1:]})
            elif refused_permanently(status):
                # Central will never accept this event: drop it so it cannot block the rest.
                LOG.warning("app effect report refused by Central with status %d; dropped", status)
                self._save({**record, "pending": record["pending"][1:],
                            "refused_reports": record.get("refused_reports", 0) + 1,
                            "last_refused_status": status})
            else:
                return


def refused_permanently(status: int) -> bool:
    """A 4xx Central repeats for the same report. Session refusal (401/403) re-enrolls and
    retries; timeout/backpressure (408/429), 5xx and network errors retry the same head."""
    return 400 <= status < 500 and status not in (*REFUSED, 408, 429)
