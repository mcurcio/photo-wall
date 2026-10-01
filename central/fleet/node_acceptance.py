"""Explicit representative-media qualification for device-scoped V2 rollback roots.

This owner never advances a fleet frontier. Re-reading old evidence cannot extend
its age or satisfy the sustained window; missing witnesses leave the request open.
"""
from __future__ import annotations

from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from central.fleet.acceptance_evidence import current_control_receipt_matches
from central.fleet.acceptance_query import load_current_app_control_in
from central.fleet.node_app_links import load_current_node_app_link_for_player_in
from central.fleet.node_sessions import (
    NodeControlError,
    NodeSessions,
    grant_boottime_at,
    node_sample_fresh,
)
from central.installation_repository import PostgresInstallationRepository
from central.transaction_locks import acquire_runtime_locks
from contracts.app_environment import AppEnvironmentRefV2
from contracts.models import Plan, Readiness
from contracts.node_boot import parse_node_boot_offer
from contracts.node_commands import parse_session_grant
from contracts.node_display import parse_display_exchange
from contracts.node_frame import frame_witness_tag
from contracts.node_protocol import digest, identifier, token
from contracts.player_control import ControlAppliedReceipt


def current_cohort_in(conn, device_id: str, generation: int, now: float) -> dict:
    """Current hardware mode/capacity declarations, separate from authored binding.

    Binding changes still undergo the caller's hard compatibility and withdrawal
    checks. Connection/mode generations fence an intervening physical mode change.
    """
    player = conn.execute("SELECT id FROM players WHERE device_id=%s AND retired_at IS NULL",
                          (device_id,)).fetchone()
    if player is None:
        raise NodeControlError("node_player_unavailable")
    reports = conn.execute("SELECT output_id,observation FROM outputs WHERE player_id=%s ORDER BY output_id",
                           (player["id"],)).fetchall()
    rows = conn.execute("SELECT DISTINCT ON(e.output_id) e.output_id,e.request,e.received_at,s.grant_payload,s.expires_at "
                        "FROM node_display_exchanges e JOIN node_sessions s USING(session_id) "
                        "JOIN node_producers p ON p.producer_id=e.producer_id "
                        "JOIN node_boot_admissions b USING(admission_id) "
                        "WHERE s.device_id=%s AND s.device_generation=%s AND s.revoked_at IS NULL "
                        "AND s.expires_at>%s AND b.superseded_at IS NULL "
                        "ORDER BY e.output_id,e.sampled_boottime_ms DESC",
                        (device_id, generation, now)).fetchall()
    display = {row["output_id"]: row for row in rows}
    cohort = []
    for report in reports:
        row = display.get(report["output_id"])
        if row is None or now-row["received_at"] > 10:
            raise NodeControlError("node_output_cohort_unavailable")
        observed = parse_display_exchange(bytes(row["request"]))
        local_now = grant_boottime_at(parse_session_grant(bytes(row["grant_payload"])), row["expires_at"], now)
        if (not node_sample_fresh(local_now, observed.sampled_boottime_ms, max_age_ms=10000)
                or not observed.connected or not report["observation"].get("connected")):
            raise NodeControlError("node_output_cohort_unavailable")
        cohort.append({"output_id": report["output_id"], "observation": report["observation"],
                       "connection_generation": observed.output.connection_generation,
                       "mode_generation": observed.output.mode_generation})
    if not cohort:
        raise NodeControlError("node_output_cohort_unavailable")
    return {"outputs": cohort, "capacity_basis": "representative_media_with_capacity_ok"}


class NodeAcceptance:
    WINDOW_SECONDS = 30
    MAX_GAP_SECONDS = 5

    def __init__(self, sessions: NodeSessions):
        self.sessions = sessions

    def begin(self, device_id: str, qualification_id: UUID, environment_sha256: str,
              operator_audit_ref: str) -> dict:
        self.sessions.require_enabled()
        identifier(qualification_id)
        digest(environment_sha256)
        token(operator_audit_ref, 256)
        with self.sessions.db.transaction() as conn:
            generation = self.sessions.lock_device_generation_in(conn, device_id)
            old = conn.execute("SELECT * FROM node_app_qualifications WHERE qualification_id=%s",
                               (qualification_id,)).fetchone()
            if old:
                if (old["device_id"], old["device_generation"], old["environment_sha256"],
                    old["operator_audit_ref"]) != (device_id, generation, environment_sha256, operator_audit_ref):
                    raise NodeControlError("node_qualification_identity_conflict")
            else:
                if not conn.execute("SELECT 1 FROM node_environment_catalog WHERE environment_sha256=%s",
                                    (environment_sha256,)).fetchone():
                    raise NodeControlError("node_environment_unknown", 404)
                conn.execute("INSERT INTO node_app_qualifications VALUES(%s,%s,%s,%s,%s,%s)",
                             (qualification_id, device_id, generation, environment_sha256,
                              operator_audit_ref, self.sessions.clock.utc()))
        return {"qualification_id": str(qualification_id), "status": "awaiting_representative_media"}

    def sample(self, qualification_id: UUID) -> dict:
        self.sessions.require_enabled()
        identifier(qualification_id)
        with self.sessions.db.transaction() as conn:
            acquire_runtime_locks(conn)
            request = conn.execute("SELECT * FROM node_app_qualifications WHERE qualification_id=%s",
                                   (qualification_id,)).fetchone()
            if request is None:
                raise NodeControlError("node_qualification_unknown", 404)
            generation = self.sessions.lock_device_generation_in(conn, request["device_id"])
            if generation != request["device_generation"]:
                raise NodeControlError("node_qualification_generation_changed")
            accepted = conn.execute("SELECT acceptance_id FROM node_environment_acceptances WHERE qualification_id=%s",
                                    (qualification_id,)).fetchone()
            if accepted:
                return {"status": "accepted", "acceptance_id": str(accepted["acceptance_id"])}
            now = self.sessions.clock.utc()
            control = load_current_app_control_in(conn, request["device_id"])
            if (control is None or control.last_result != "applied" or control.applied_at is None
                    or now-control.applied_at > self.MAX_GAP_SECONDS):
                raise NodeControlError("node_qualification_control_stale")
            link = load_current_node_app_link_for_player_in(conn, control.player_id, control.authority_epoch, now)
            if link is None or link.environment_sha256 != request["environment_sha256"]:
                raise NodeControlError("node_qualification_process_changed")
            if not current_control_receipt_matches(ControlAppliedReceipt.model_validate_json(link.control_receipt), control):
                raise NodeControlError("node_qualification_control_unlinked")
            cohort = current_cohort_in(conn, request["device_id"], generation, now)
            boot = conn.execute("SELECT o.offer_payload FROM node_boot_admissions b JOIN node_boot_offers o "
                                "ON o.offer_id=b.offer_id WHERE b.device_id=%s AND b.device_generation=%s "
                                "AND b.superseded_at IS NULL", (request["device_id"], generation)).fetchone()
            if boot is None:
                raise NodeControlError("node_qualification_base_unavailable")
            offer = parse_node_boot_offer(bytes(boot["offer_payload"]))
            reference = conn.execute("SELECT reference FROM node_environment_catalog WHERE environment_sha256=%s",
                                     (link.environment_sha256,)).fetchone()
            environment = AppEnvironmentRefV2(**reference["reference"])
            if (environment.base_abi, environment.graphics_abi, environment.plugin_abi) != (
                    offer.base.base_abi, offer.base.graphics_abi, offer.base.plugin_abi):
                raise NodeControlError("node_qualification_base_abi_mismatch")
            feedback = conn.execute("SELECT readiness,received_at FROM player_feedback WHERE player_id=%s "
                                    "AND authority_epoch=%s", (control.player_id, control.authority_epoch)).fetchone()
            if feedback is None or now-feedback["received_at"] > self.MAX_GAP_SECONDS:
                raise NodeControlError("node_qualification_readiness_stale")
            ready = Readiness.model_validate(feedback["readiness"])
            plan_row = conn.execute("SELECT manifest FROM plan_offers WHERE player_id=%s AND authority_epoch=%s "
                                    "AND revision=%s", (control.player_id, control.authority_epoch, ready.revision)).fetchone()
            if plan_row is None or not ready.capacity_ok or ready.failures:
                raise NodeControlError("node_qualification_readiness_unavailable")
            plan = Plan.model_validate(plan_row["manifest"])
            configuration = PostgresInstallationRepository(self.sessions.clock).configuration_in(
                conn, control.player_id, control.authority_epoch)
            if configuration is None or plan.bindings != tuple(configuration["bindings"]) or plan.valid_until <= now:
                raise NodeControlError("node_qualification_plan_changed")
            media = [layer for layer in plan.layers if layer.presentation == "media"
                     and layer.assignment_id in ready.prepared and layer.start<=now<layer.end]
            bindings = conn.execute("SELECT output_id FROM bindings WHERE player_id=%s", (control.player_id,)).fetchall()
            required = {row["output_id"] for row in bindings}
            if not required or required != {layer.output_id for layer in media}:
                raise NodeControlError("node_representative_media_evidence_required")
            commits = {r["assignment_id"] for r in conn.execute("SELECT assignment_id FROM execution_commits "
                "WHERE player_id=%s AND authority_epoch=%s AND revision=%s AND valid",
                (control.player_id, control.authority_epoch, plan.revision)).fetchall()}
            by_output = {b.output_id: b for b in plan.bindings}
            buffers, buffer_samples = {}, {}
            for output in required:
                latest = conn.execute("SELECT e.request,e.received_at,s.grant_payload,s.expires_at FROM node_display_exchanges e "
                    "JOIN node_sessions s USING(session_id) WHERE s.device_id=%s AND s.device_generation=%s "
                    "AND s.revoked_at IS NULL AND e.output_id=%s ORDER BY e.sampled_boottime_ms DESC LIMIT 1",
                    (request["device_id"], generation, output)).fetchone()
                exchange = parse_display_exchange(bytes(latest["request"])) if latest else None
                if (exchange is None or now-latest["received_at"] > self.MAX_GAP_SECONDS
                        or exchange.admitted is None or exchange.receipt is None
                        or exchange.receipt.surface != exchange.admitted
                        or exchange.sampled_boottime_ms-exchange.receipt.sampled_boottime_ms > self.MAX_GAP_SECONDS*1000
                        or exchange.admitted.process != link.process or exchange.admitted.app_epoch != link.app_epoch
                        or exchange.completed_decision_id is None
                        or not conn.execute("SELECT 1 FROM node_display_handoffs WHERE decision_id=%s",
                                            (exchange.completed_decision_id,)).fetchone()):
                    raise NodeControlError("node_qualification_handoff_unavailable")
                local_now = grant_boottime_at(parse_session_grant(bytes(latest["grant_payload"])), latest["expires_at"], now)
                if not node_sample_fresh(local_now, exchange.receipt.sampled_boottime_ms, max_age_ms=self.MAX_GAP_SECONDS*1000):
                    raise NodeControlError("node_qualification_original_sample_stale")
                # The explicit qualification scenario is a single full-opacity
                # committed media layer per Output. General overlap/fade semantics
                # remain renderer-owned and are not reconstructed here.
                active = [layer for layer in plan.layers if layer.output_id == output
                          and layer.assignment_id in commits and layer.start <= now < layer.end]
                if (len(active) != 1 or active[0].presentation != "media"
                        or active[0].assignment_id not in ready.prepared
                        or active[0].opacity != 1 or active[0].fade_in or active[0].fade_out):
                    raise NodeControlError("node_representative_single_media_required")
                binding = by_output[output]
                if binding.preview is not None:
                    raise NodeControlError("node_qualification_preview_active")
                expected_tag = frame_witness_tag(output_id=output, frame_id=binding.frame_id, binding_generation=binding.generation,
                    config_revision=binding.configuration_revision, calibration=binding.calibration.model_dump(mode="json"),
                    layers=({"assignment_id": active[0].assignment_id, "variant_sha256": active[0].variant.sha256},))
                if exchange.receipt.frame_tag != expected_tag:
                    raise NodeControlError("node_representative_frame_witness_mismatch")
                buffers[output] = exchange.receipt.buffer_id
                buffer_samples[output] = exchange.receipt.sampled_boottime_ms
            witness = {"process": [link.process.pid, link.process.start_ticks, str(link.process.invocation_id)],
                "app_epoch": link.app_epoch, "authority_epoch": link.authority_epoch,
                "control_sequence": control.applied_sequence, "readiness_sequence": ready.sequence,
                "buffers": buffers, "buffer_samples": buffer_samples,
                "bindings": [[b.output_id, b.frame_id, b.generation, b.configuration_revision] for b in plan.bindings],
                "media_types": sorted({layer.variant.media_type for layer in media}),
                "media_assignments": sorted([[layer.output_id, layer.assignment_id, layer.variant.sha256] for layer in media])}
            samples = conn.execute("SELECT * FROM node_app_qualification_samples WHERE qualification_id=%s "
                                   "ORDER BY observed_at DESC", (qualification_id,)).fetchall()
            if samples:
                previous = samples[0]["witness"]
                if (witness["control_sequence"] <= previous["control_sequence"]
                        or witness["readiness_sequence"] <= previous["readiness_sequence"]
                        or any(buffers[k] == previous["buffers"].get(k)
                               or buffer_samples[k] <= previous["buffer_samples"].get(k, -1) for k in buffers)):
                    return {"status": "awaiting_new_witnesses", "accepted": False}
            sample_id = uuid4()
            conn.execute("INSERT INTO node_app_qualification_samples VALUES(%s,%s,%s,%s,%s,%s)",
                (qualification_id, sample_id, Jsonb(witness), Jsonb(cohort), offer.base.content_key, now))
            start, previous_at, ids = now, now, [str(sample_id)]
            for sample in samples:
                old = sample["witness"]
                if (previous_at-sample["observed_at"] > self.MAX_GAP_SECONDS or sample["cohort"] != cohort
                        or sample["base_content_key"] != offer.base.content_key
                        or any(old[k] != witness[k] for k in ("process", "app_epoch", "authority_epoch", "media_types", "media_assignments", "bindings"))):
                    break
                start = previous_at = sample["observed_at"]
                ids.append(str(sample["sample_id"]))
            if now-start < self.WINDOW_SECONDS:
                return {"status": "observing", "sustained_seconds": now-start, "accepted": False}
            acceptance_id = uuid4()
            conn.execute("INSERT INTO node_environment_acceptances VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (acceptance_id, qualification_id, request["device_id"], generation, offer.base.content_key,
                 request["environment_sha256"], Jsonb(cohort), Jsonb({"sample_ids": ids,
                    "representative_media_types": witness["media_types"], "physical_pixels": "unknown"}), now))
            return {"status": "accepted", "acceptance_id": str(acceptance_id), "fleet_frontier_changed": False}
