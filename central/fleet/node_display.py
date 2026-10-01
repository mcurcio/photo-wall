"""Display control owner: current Registry/process authority plus explicit Runtime decision.

Incoming compositor observations never authorize themselves. Exact handoff
completion references an immutable earlier decision and later native presentation.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from central.fleet.node_app_links import (
    load_current_node_app_link_for_player_in,
    load_current_node_app_link_in,
)
from central.fleet.node_sessions import (
    NodeControlError,
    NodeSessions,
    claim_intake_in,
    grant_boottime_at,
    node_sample_fresh,
    session_boottime_at,
)
from central.installation_repository import PostgresInstallationRepository
from central.transaction_locks import acquire_runtime_locks
from contracts.node_commands import parse_session_grant
from contracts.node_display import (
    DisplayDecision,
    Surface,
    encode_display_decision,
    encode_display_exchange,
    parse_display_decision,
    parse_display_exchange,
)


class NodeDisplay:
    def __init__(
        self, sessions: NodeSessions, *, runtime, decision_ms: int = 3000, receipt_ms: int = 5000
    ):
        if not 1 <= decision_ms <= 5000 or not 1 <= receipt_ms <= 5000:
            raise ValueError("display_timing_bound")
        self.sessions, self.runtime = sessions, runtime
        self.trials = None
        self.installation = PostgresInstallationRepository(sessions.clock)
        self.decision_ms, self.receipt_ms = decision_ms, receipt_ms

    def exchange(self, session_id: UUID, credential: str, raw: bytes) -> bytes:
        request = parse_display_exchange(raw)
        canonical = encode_display_exchange(request)
        with self.sessions.db.transaction() as conn:
            acquire_runtime_locks(conn)
            principal = self.sessions.authenticate_in(conn, session_id, credential)
            if request.producer != principal.grant.producer:
                raise NodeControlError("display_producer_mismatch", 403)
            now = principal.admission.ensure_current(self.sessions.clock)
            self._fresh_request(principal, request, now)
            prior = conn.execute(
                "SELECT request,response FROM node_display_exchanges "
                "WHERE producer_id=%s AND request_id=%s",
                (principal.producer_id, request.request_id),
            ).fetchone()
            if prior:
                if bytes(prior["request"]) != canonical:
                    raise NodeControlError("display_request_conflict", 409)
                return bytes(prior["response"])
            latest = conn.execute(
                "SELECT sampled_boottime_ms,request FROM node_display_exchanges "
                "WHERE producer_id=%s AND output_id=%s "
                "ORDER BY sampled_boottime_ms DESC LIMIT 1",
                (principal.producer_id, request.output.output_id),
            ).fetchone()
            if latest:
                old = parse_display_exchange(bytes(latest["request"]))
                if (
                    request.sampled_boottime_ms <= latest["sampled_boottime_ms"]
                    or request.output.connection_generation < old.output.connection_generation
                    or request.output.mode_generation < old.output.mode_generation
                ):
                    raise NodeControlError("display_observation_stale", 409)
            if request.sampled_boottime_ms >= principal.grant.expires_boottime_ms:
                raise NodeControlError("display_session_expired", 401)
            link = load_current_node_app_link_in(conn, principal)
            configuration = (
                self.installation.configuration_in(conn, link.player_id, link.authority_epoch)
                if link
                else None
            )
            binding = (
                next(
                    (
                        b
                        for b in configuration["bindings"]
                        if b.output_id == request.output.output_id
                    ),
                    None,
                )
                if configuration
                else None
            )
            surface = (
                Surface(
                    request.output,
                    link.process,
                    link.app_epoch,
                    binding.generation,
                    binding.configuration_revision,
                    binding.frame_id,
                )
                if binding
                else None
            )
            operation, reason, fence, receipt = "retain", "no_current_binding", None, None
            decision_surface = surface
            if link is not None and request.connected:
                self._complete_in(conn, principal, request, surface, link, now)
                previous = request.admitted or request.candidate
                if previous is not None and (surface is None or previous.frame_id != surface.frame_id
                        or previous.process != surface.process or previous.app_epoch != surface.app_epoch
                        or previous.binding_generation != surface.binding_generation):
                    if not ((latest and previous in (old.admitted, old.candidate))
                            or self._authorized_previous_in(conn, principal, request, previous)):
                        raise NodeControlError("display_withdrawal_previous_unknown", 409)
                    admission = self.runtime.display_withdrawal_in(conn, player_id=link.player_id,
                        authority_epoch=link.authority_epoch, previous_surface=previous,
                        expected_surface=surface)
                    if admission.allowed:
                        operation, reason, fence = "withdraw", admission.reason, admission.fence
                        decision_surface = previous
                    else:
                        reason = admission.reason
                elif surface is not None:
                    operation, reason = self._operation(request, surface)
                    if operation != "retain":
                        admission = self.runtime.display_admission_in(
                            conn, player_id=link.player_id, authority_epoch=link.authority_epoch,
                            output_id=binding.output_id, frame_id=binding.frame_id,
                            binding_generation=binding.generation,
                            config_revision=binding.configuration_revision,
                            process=link.process, app_epoch=link.app_epoch,
                            environment_sha256=link.environment_sha256, phase=operation)
                        if admission.allowed:
                            fence, reason = admission.fence, admission.reason
                            receipt = request.receipt if operation == "handoff" else None
                        else:
                            operation, reason = "retain", admission.reason
            decision = DisplayDecision(
                request.producer,
                request.request_id,
                uuid4(),
                request.output,
                operation,
                min(
                    request.sampled_boottime_ms + self.decision_ms,
                    principal.grant.expires_boottime_ms,
                ),
                reason,
                decision_surface if operation != "retain" else None,
                receipt,
                fence,
            )
            if self.trials is not None:
                from dataclasses import replace

                trial = self.trials.deliver_in(conn, principal, request, link, binding, now)
                decision = replace(decision, trial=trial)
            response = encode_display_decision(decision)
            claim_intake_in(conn, request.producer.device_id, "display", now)
            conn.execute(
                "INSERT INTO node_display_exchanges(producer_id,request_id,session_id,"
                "output_id,sampled_boottime_ms,request,response,decision_id,received_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    principal.producer_id,
                    request.request_id,
                    session_id,
                    request.output.output_id,
                    request.sampled_boottime_ms,
                    canonical,
                    response,
                    decision.decision_id,
                    now,
                ),
            )
            self._fresh_request(principal, request, principal.admission.ensure_current(self.sessions.clock))
            return response

    def _authorized_previous_in(self, conn, principal, request, previous) -> bool:
        """Recognize an issued role for removal, never for admission or recovery.

        Native revision promotion can precede its next upload and Registry may
        unbind in that interval. The same authenticated producer can report this
        historical effect after session renewal; issuance is not reinterpreted as
        a current command. A fresh exact receipt and immutable authorization are
        both required. Current Runtime withdrawal authority is checked separately.
        """
        receipt = request.receipt
        if receipt is None or receipt.surface != previous:
            return False
        rows = conn.execute(
            "SELECT request,response FROM node_display_exchanges "
            "WHERE producer_id=%s AND output_id=%s "
            "ORDER BY sampled_boottime_ms DESC LIMIT 128",
            (principal.producer_id, request.output.output_id),
        ).fetchall()
        for row in rows:
            decision = parse_display_decision(bytes(row["response"]))
            issued = parse_display_exchange(bytes(row["request"]))
            if (
                decision.operation in ("revision", "handoff")
                and decision.producer == request.producer
                and decision.output == request.output
                and decision.surface == previous
                and decision.runtime_fence is not None
                and issued.sampled_boottime_ms < receipt.sampled_boottime_ms
                and (
                    decision.operation == "revision"
                    or (decision.receipt.grant_id == receipt.grant_id
                        and decision.receipt.buffer_id != receipt.buffer_id)
                )
            ):
                return True
        return False

    def _fresh_request(self, principal, request, now):
        local_now = session_boottime_at(principal, now)
        if not node_sample_fresh(local_now, request.sampled_boottime_ms):
            raise NodeControlError("display_sample_stale", 409)
        if request.receipt and not node_sample_fresh(local_now,
                request.receipt.sampled_boottime_ms, max_age_ms=self.receipt_ms):
            raise NodeControlError("display_receipt_stale", 409)

    def current_frame_in(self, conn, frame_id: str):
        """Operator-side read under Runtime locks, then the same Fleet/session order."""
        row = conn.execute(
            "SELECT b.player_id,b.output_id,p.authority_epoch,p.device_id "
            "FROM bindings b JOIN players p ON p.id=b.player_id WHERE b.frame_id=%s "
            "AND p.retired_at IS NULL",
            (frame_id,),
        ).fetchone()
        if row is None:
            raise NodeControlError("trial_frame_unbound", 409)
        generation = self.sessions.lock_device_generation_in(conn, row["device_id"])
        now = self.sessions.clock.utc()
        session = conn.execute(
            "SELECT s.producer_id,s.grant_payload,s.expires_at FROM node_sessions s "
            "JOIN node_producers p USING(producer_id) JOIN node_boot_admissions b USING(admission_id) "
            "WHERE s.device_id=%s AND s.device_generation=%s AND s.owner='display_host' "
            "AND s.revoked_at IS NULL AND s.expires_at>%s AND b.superseded_at IS NULL FOR UPDATE OF s",
            (row["device_id"], generation, now),
        ).fetchone()
        if session is None:
            raise NodeControlError("trial_display_unavailable", 409)
        grant = parse_session_grant(bytes(session["grant_payload"]))
        link = load_current_node_app_link_for_player_in(
            conn, row["player_id"], row["authority_epoch"], now
        )
        configuration = self.installation.configuration_in(
            conn, row["player_id"], row["authority_epoch"]
        )
        binding = (
            next((b for b in configuration["bindings"] if b.frame_id == frame_id), None)
            if configuration
            else None
        )
        observed = conn.execute(
            "SELECT request,received_at FROM node_display_exchanges "
            "WHERE producer_id=%s AND output_id=%s ORDER BY sampled_boottime_ms DESC LIMIT 1",
            (session["producer_id"], row["output_id"]),
        ).fetchone()
        if (
            link is None
            or binding is None
            or observed is None
            or now - observed["received_at"] >= 10
        ):
            raise NodeControlError("trial_current_output_required", 409)
        request = parse_display_exchange(bytes(observed["request"]))
        local_now = grant_boottime_at(grant, session["expires_at"], self.sessions.clock.utc())
        if not node_sample_fresh(local_now, request.sampled_boottime_ms) or (
                request.receipt is not None and not node_sample_fresh(local_now,
                    request.receipt.sampled_boottime_ms, max_age_ms=self.receipt_ms)):
            raise NodeControlError("trial_display_evidence_stale", 409)
        expected = Surface(
            request.output,
            link.process,
            link.app_epoch,
            binding.generation,
            binding.configuration_revision,
            binding.frame_id,
        )
        if (
            request.producer != grant.producer
            or not request.connected
            or request.admitted != expected
            or request.receipt is None
            or request.receipt.surface != expected
            or request.sampled_boottime_ms - request.receipt.sampled_boottime_ms >= self.receipt_ms
        ):
            raise NodeControlError("trial_admitted_surface_required", 409)
        admission = self.runtime.display_admission_in(
            conn,
            player_id=link.player_id,
            authority_epoch=link.authority_epoch,
            output_id=binding.output_id,
            frame_id=binding.frame_id,
            binding_generation=binding.generation,
            config_revision=binding.configuration_revision,
            process=link.process,
            app_epoch=link.app_epoch,
            environment_sha256=link.environment_sha256,
            phase="revision",
        )
        if not admission.allowed:
            raise NodeControlError(admission.reason, 409)
        return CurrentDisplay(session["producer_id"], link, binding, request)

    def _operation(self, request, surface) -> tuple[str, str]:
        if request.admitted == surface:
            return "retain", "already_admitted"
        if request.admitted is not None:
            old = request.admitted
            if (old.output, old.process, old.app_epoch, old.binding_generation, old.frame_id) == (
                surface.output,
                surface.process,
                surface.app_epoch,
                surface.binding_generation,
                surface.frame_id,
            ) and surface.config_revision > old.config_revision:
                return "revision", "revision_requires_runtime"
            return "retain", "authorized_withdrawal_required"
        if request.candidate == surface:
            receipt = request.receipt
            if (
                receipt
                and receipt.surface == surface
                and (request.sampled_boottime_ms - receipt.sampled_boottime_ms < self.receipt_ms)
            ):
                return "handoff", "handoff_requires_runtime"
            return "retain", "awaiting_compositor_presentation"
        if request.candidate is None:
            return "candidate", "candidate_requires_runtime"
        return "retain", "candidate_identity_changed"

    def _complete_in(self, conn, principal, request, surface, link, now) -> None:
        if request.completed_decision_id is None:
            return
        row = conn.execute(
            "SELECT request,response FROM node_display_exchanges WHERE decision_id=%s AND producer_id=%s",
            (request.completed_decision_id, principal.producer_id),
        ).fetchone()
        if row is None:
            raise NodeControlError("display_handoff_decision_unknown", 409)
        decision = parse_display_decision(bytes(row["response"]))
        issued = parse_display_exchange(bytes(row["request"]))
        if decision.operation == "withdraw":
            if (request.output != decision.output or request.admitted == decision.surface or request.candidate == decision.surface
                    or not issued.sampled_boottime_ms <= request.completed_boottime_ms < decision.expires_boottime_ms):
                raise NodeControlError("display_withdrawal_completion_mismatch", 409)
            conn.execute("INSERT INTO node_display_withdrawals(decision_id,receipt_request_id,received_at) "
                "VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                (decision.decision_id, request.request_id, now))
            return  # Recording removal grants no Runtime recovery or successor role.
        receipt = request.receipt
        if (
            decision.operation != "handoff"
            or request.completed_boottime_ms >= decision.expires_boottime_ms
            or request.completed_boottime_ms <= decision.receipt.sampled_boottime_ms
            or decision.surface != surface
            or request.admitted != surface
            or receipt is None
            or receipt.surface != surface
            or receipt.grant_id != decision.receipt.grant_id
            or receipt.buffer_id == decision.receipt.buffer_id
            or receipt.sampled_boottime_ms <= decision.receipt.sampled_boottime_ms
            or request.sampled_boottime_ms - receipt.sampled_boottime_ms >= self.receipt_ms
        ):
            raise NodeControlError("display_handoff_receipt_mismatch", 409)
        admission = self.runtime.display_admission_in(
            conn,
            player_id=link.player_id,
            authority_epoch=link.authority_epoch,
            output_id=surface.output.output_id,
            frame_id=surface.frame_id,
            binding_generation=surface.binding_generation,
            config_revision=surface.config_revision,
            process=surface.process,
            app_epoch=surface.app_epoch,
            environment_sha256=link.environment_sha256,
            phase="handoff",
        )
        if not admission.allowed or admission.fence != decision.runtime_fence:
            raise NodeControlError("display_handoff_authority_changed", 409)
        inserted = conn.execute(
            "INSERT INTO node_display_handoffs(decision_id,receipt_request_id,received_at) "
            "VALUES(%s,%s,%s) ON CONFLICT DO NOTHING RETURNING decision_id",
            (decision.decision_id, request.request_id, now),
        ).fetchone()
        if inserted:
            # The owning Runtime port rechecks the current Registry tuple. No Run is settled.
            self.runtime.reconcile_node_output_in(
                conn,
                player_id=link.player_id,
                authority_epoch=link.authority_epoch,
                output_id=surface.output.output_id,
                frame_id=surface.frame_id,
                binding_generation=surface.binding_generation,
                configuration_revision=surface.config_revision,
                recovering=True,
                producer_id=principal.producer_id,
                evidence_id=decision.decision_id,
                evidence_kind="display_handoff",
                detail={
                    "decision_id": str(decision.decision_id),
                    "grant_id": str(receipt.grant_id),
                    "buffer_id": receipt.buffer_id,
                    "runtime_fence": decision.runtime_fence,
                },
            )


@dataclass(frozen=True)
class CurrentDisplay:
    producer_id: UUID
    link: object
    binding: object
    request: object
