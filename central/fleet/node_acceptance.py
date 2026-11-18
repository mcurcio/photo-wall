"""Explicit representative-media qualification for device-scoped V2 rollback roots.

This owner never advances a fleet frontier. Re-reading old evidence cannot extend
its age or satisfy the sustained window; missing witnesses leave the request open.
"""
from __future__ import annotations

from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from central.fleet.acceptance_evidence import current_control_receipt_matches
from central.fleet.acceptance_query import load_current_app_control_in
from central.fleet.device_fence import fence_in
from central.fleet.node_app_links import load_current_node_app_link_for_player_in
from central.fleet.node_sessions import NodeControlError, NodeSessions
from central.infra.node_releases import environment_reference_in
from central.infra.player_contact_store import heard_at_in
from central.installation_repository import PostgresInstallationRepository
from central.transaction_locks import acquire_runtime_locks
from contracts.liveness import HEARD_WRITE_SECONDS
from contracts.models import Plan, Readiness
from contracts.node_boot import parse_node_boot_offer
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
    reports = conn.execute("SELECT output_id,observation FROM outputs WHERE player_id=%s "
                           "AND observation IS NOT NULL ORDER BY output_id",
                           (player["id"],)).fetchall()
    rows = conn.execute("SELECT DISTINCT ON(e.output_id) e.output_id,e.request,e.received_at "
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
        if not observed.connected or not report["observation"].get("connected"):
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
        identifier(qualification_id)
        digest(environment_sha256)
        token(operator_audit_ref, 256)
        with self.sessions.db.transaction() as conn:
            generation = fence_in(conn, device_id, "act").generation()
            old = conn.execute("SELECT * FROM node_app_qualifications WHERE qualification_id=%s",
                               (qualification_id,)).fetchone()
            if old:
                if (old["device_id"], old["device_generation"], old["environment_sha256"],
                    old["operator_audit_ref"]) != (device_id, generation, environment_sha256, operator_audit_ref):
                    raise NodeControlError("node_qualification_identity_conflict")
            else:
                if environment_reference_in(conn, environment_sha256) is None:
                    raise NodeControlError("node_environment_unknown", 404)
                conn.execute("INSERT INTO node_app_qualifications VALUES(%s,%s,%s,%s,%s,%s)",
                             (qualification_id, device_id, generation, environment_sha256,
                              operator_audit_ref, self.sessions.clock.utc()))
        return {"qualification_id": str(qualification_id), "status": "awaiting_representative_media"}

    def sample(self, qualification_id: UUID) -> dict:
        identifier(qualification_id)
        with self.sessions.db.transaction() as conn:
            acquire_runtime_locks(conn)
            request = conn.execute("SELECT * FROM node_app_qualifications WHERE qualification_id=%s",
                                   (qualification_id,)).fetchone()
            if request is None:
                raise NodeControlError("node_qualification_unknown", 404)
            generation = fence_in(conn, request["device_id"], "act").generation()
            if generation != request["device_generation"]:
                raise NodeControlError("node_qualification_generation_changed")
            accepted = conn.execute("SELECT acceptance_id FROM node_environment_acceptances WHERE qualification_id=%s",
                                    (qualification_id,)).fetchone()
            if accepted:
                return {"status": "accepted", "acceptance_id": str(accepted["acceptance_id"])}
            now = self.sessions.clock.utc()
            control = load_current_app_control_in(conn, request["device_id"], lock=True)
            # Central re-sends the applied delivery while the state is unchanged, so its age
            # is not liveness: the control witness is that the link's receipt is the current
            # applied one (below); the Player's contact carries liveness (below).
            if control is None or control.last_result != "applied" or control.applied_at is None:
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
            environment = environment_reference_in(conn, link.environment_sha256)
            if (environment.base_abi, environment.graphics_abi, environment.plugin_abi) != (
                    offer.base.base_abi, offer.base.graphics_abi, offer.base.plugin_abi):
                raise NodeControlError("node_qualification_base_abi_mismatch")
            feedback = conn.execute("SELECT readiness FROM player_feedback WHERE player_id=%s "
                                    "AND authority_epoch=%s", (control.player_id, control.authority_epoch)).fetchone()
            # Readiness is sent once per change, so its age says nothing: the Player is live when
            # Central heard from it within the gap (its contact is written at most every
            # HEARD_WRITE_SECONDS, central/infra/player_contact_store.py).
            heard = heard_at_in(conn, control.player_id, control.authority_epoch)
            if (feedback is None or heard is None
                    or now - heard > self.MAX_GAP_SECONDS + HEARD_WRITE_SECONDS):
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
            bindings = conn.execute("SELECT output_id FROM frames WHERE player_id=%s", (control.player_id,)).fetchall()
            required = {row["output_id"] for row in bindings}
            if not required or required != {layer.output_id for layer in media}:
                raise NodeControlError("node_representative_media_evidence_required")
            commits = {r["assignment_id"] for r in conn.execute("SELECT assignment_id FROM execution_commits "
                "WHERE player_id=%s AND authority_epoch=%s AND revision=%s AND valid",
                (control.player_id, control.authority_epoch, plan.revision)).fetchall()}
            by_output = {b.output_id: b for b in plan.bindings}
            buffers, buffer_samples = {}, {}
            for output in required:
                latest = conn.execute("SELECT e.request,e.received_at FROM node_display_exchanges e "
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
                # The explicit qualification scenario is a single full-opacity
                # media layer per Output in force now: committed when it awaits a grant, else
                # prepared alone (decision 0032: a grant only for a change). General
                # overlap/fade semantics remain renderer-owned and are not reconstructed here.
                active = [layer for layer in plan.layers if layer.output_id == output
                          and (layer.assignment_id in commits or not layer.awaits_grant)
                          and layer.start <= now < layer.end]
                if (len(active) != 1 or active[0].presentation != "media"
                        or active[0].assignment_id not in ready.prepared
                        or active[0].opacity != 1 or active[0].fade_in or active[0].fade_out):
                    raise NodeControlError("node_representative_single_media_required")
                binding = by_output[output]
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
                # A new witness is new buffers on every Output. Readiness is sent once per
                # change, so a Player steadily showing its media keeps one readiness number: its
                # liveness is the contact checked above, never an advancing readiness.
                if any(buffers[k] == previous["buffers"].get(k)
                       or buffer_samples[k] <= previous["buffer_samples"].get(k, -1) for k in buffers):
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
