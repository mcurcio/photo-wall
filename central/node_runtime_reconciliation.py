"""Asynchronous, Output-scoped consequences of accepted node loss evidence.

Reboot initiation is risk, never cessation. Evidence only affects execution after
its exact kernel process is linked to the Registry's current app epoch and its
Output tuple matches the current DisplayHost and binding. Runs keep advancing.
"""
from __future__ import annotations

from central.coordination import Coordinator
from central.fleet.node_ingest import _projection
from central.fleet.node_sessions import NodeControlError, NodeSessions
from central.transaction_locks import acquire_runtime_locks
from contracts.node_app_link import parse_node_app_link
from contracts.node_commands import producer_from_document
from contracts.node_protocol import (
    AppProcessFact,
    NodeEventV2,
    RebootFact,
    SurfaceFact,
    fact_key,
    parse_node_message,
)


class NodeRuntimeReconciler:
    def __init__(self, sessions: NodeSessions, coordinator: Coordinator):
        self.sessions, self.coordinator = sessions, coordinator

    def advance(self, *, limit: int = 32) -> int:
        if self.sessions.config is None:
            return 0
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError("node_reconciliation_limit")
        with self.sessions.db.transaction() as conn:
            # Selection is lock-free. Each item is re-read after canonical locks.
            ids = [row["work_id"] for row in conn.execute(
                "SELECT work_id FROM node_reconciliation_work WHERE completed_at IS NULL "
                "AND next_attempt_at<=%s ORDER BY next_attempt_at,work_id LIMIT %s",
                (self.sessions.clock.utc(), limit)).fetchall()]
        return sum(self._one(work_id) for work_id in ids)

    def _one(self, work_id: int) -> int:
        with self.sessions.db.transaction() as conn:
            acquire_runtime_locks(conn)
            located = conn.execute("SELECT p.producer FROM node_reconciliation_work w "
                                   "JOIN node_producers p USING(producer_id) WHERE work_id=%s",
                                   (work_id,)).fetchone()
            if not located:
                return 0
            producer = producer_from_document(located["producer"])
            try:
                generation = self.sessions.lock_device_generation_in(conn, producer.device_id)
            except NodeControlError:
                return self._finish(conn, work_id, "retired_generation")
            row = conn.execute("SELECT w.*,e.payload,e.disposition FROM node_reconciliation_work w "
                               "JOIN node_evidence e USING(producer_id,kind,evidence_id) "
                               "WHERE w.work_id=%s FOR UPDATE OF w", (work_id,)).fetchone()
            if row is None or row["completed_at"] is not None:
                return 0
            if generation != producer.device_generation or row["disposition"] == "historical":
                return self._finish(conn, work_id, "historical")
            current = conn.execute("SELECT 1 FROM node_sessions s JOIN node_producers p USING(producer_id) "
                                   "JOIN node_boot_admissions b USING(admission_id) WHERE s.producer_id=%s "
                                   "AND s.revoked_at IS NULL AND b.superseded_at IS NULL",
                                   (row["producer_id"],)).fetchone()
            if not current:
                return self._finish(conn, work_id, "superseded_producer")
            message = parse_node_message(bytes(row["payload"]))
            if all(type(fact) is RebootFact for fact in message.facts):
                return self._finish(conn, work_id, "reboot_initiation_at_risk")
            link_row = conn.execute("SELECT l.payload FROM node_app_links l "
                                    "JOIN node_producers p USING(producer_id) "
                                    "JOIN node_boot_admissions b USING(admission_id) "
                                    "JOIN node_sessions ls ON ls.producer_id=p.producer_id AND ls.revoked_at IS NULL "
                                    "WHERE l.device_id=%s AND l.device_generation=%s "
                                    "AND l.superseded_at IS NULL AND b.superseded_at IS NULL",
                                    (producer.device_id, generation)).fetchone()
            if link_row is None:
                return self._pending(conn, work_id, "awaiting_process_link")
            link = parse_node_app_link(bytes(link_row["payload"])).challenge
            if link.producer.kernel_boot_id != producer.kernel_boot_id:
                return self._finish(conn, work_id, "historical_boot")
            # Current producer projections elect no boot: admission rows already did.
            producers = conn.execute("SELECT p.producer_id,p.producer,p.projection FROM node_sessions s "
                                     "JOIN node_producers p USING(producer_id) "
                                     "JOIN node_boot_admissions b USING(admission_id) "
                                     "WHERE s.device_id=%s AND s.device_generation=%s "
                                     "AND s.revoked_at IS NULL AND b.superseded_at IS NULL",
                                     (producer.device_id, generation)).fetchall()
            projections = {p["producer_id"]: _projection(producer_from_document(p["producer"]),
                                                         p["projection"]) for p in producers}
            current_projection = projections.get(row["producer_id"])
            if current_projection is None:
                return self._finish(conn, work_id, "historical_producer")
            sequence = (message.sequence if type(message) is NodeEventV2
                        else message.covered_through_sequence)
            sample = (message.occurred_boottime_ms if type(message) is NodeEventV2
                      else message.sampled_boottime_ms)
            if sample < link.sampled_boottime_ms:
                return self._finish(conn, work_id, "before_process_link")
            effective = {fact_key(item.fact): item for item in current_projection.facts}
            facts = [fact for fact in message.facts if fact_key(fact) in effective
                     and effective[fact_key(fact)].sequence == sequence
                     and effective[fact_key(fact)].fact == fact]
            surfaces = {}
            for projection in projections.values():
                for projected in projection.facts:
                    fact = projected.fact
                    if type(fact) is SurfaceFact:
                        prior = surfaces.get(fact.output.output_id)
                        if prior is None or (fact.output.connection_generation,
                                fact.output.mode_generation, projected.sequence) > (
                                prior.fact.output.connection_generation,
                                prior.fact.output.mode_generation, prior.sequence):
                            surfaces[fact.output.output_id] = projected
            handled = False
            for fact in facts:
                linked = (type(fact) in (AppProcessFact, SurfaceFact)
                          and fact.process == link.process and fact.app_epoch == link.app_epoch)
                if not linked:
                    continue
                candidates = (list(surfaces.values()) if type(fact) is AppProcessFact
                              and fact.environment_sha256 == link.environment_sha256
                              and fact.state == "exited" else
                              [surfaces[fact.output.output_id]] if type(fact) is SurfaceFact
                              and fact.output.output_id in surfaces else [])
                for projected in candidates:
                    surface = projected.fact
                    if surface.frame_id is None or surface.process != link.process or surface.app_epoch != link.app_epoch:
                        continue
                    if type(fact) is SurfaceFact and surface != fact:
                        continue
                    recovering = type(fact) is SurfaceFact and fact.state == "presented_to_compositor"
                    # First presentation is only candidate evidence. A separate
                    # authorized, observed handoff must release an interruption.
                    if recovering:
                        continue
                    if not recovering and type(fact) is SurfaceFact and fact.state not in (
                            "invalidated", "withdrawn"):
                        continue
                    detail = {"cause": "node_surface_recovered" if recovering else "node_output_lost",
                              "producer_id": str(row["producer_id"]),
                              "evidence_id": str(row["evidence_id"]), "evidence_kind": row["kind"],
                              "process_pid": link.process.pid,
                              "process_start_ticks": link.process.start_ticks,
                              "invocation_id": str(link.process.invocation_id),
                              "app_epoch": link.app_epoch, "kernel_boot_id": str(producer.kernel_boot_id),
                              "display_host_incarnation": str(surface.output.display_host_incarnation),
                              "connection_generation": surface.output.connection_generation,
                              "mode_generation": surface.output.mode_generation,
                              "observed_boottime_ms": sample, "received_at": row["received_at"],
                              "assurance": "lan_serial", "physical_pixels": "unknown"}
                    applied = self.coordinator.reconcile_node_output_in(
                        conn, player_id=link.player_id, authority_epoch=link.authority_epoch,
                        output_id=surface.output.output_id, binding_generation=surface.binding_generation,
                        configuration_revision=surface.config_revision, frame_id=surface.frame_id, recovering=recovering,
                        producer_id=row["producer_id"], evidence_id=row["evidence_id"],
                        evidence_kind=row["kind"], detail=detail)
                    handled = handled or applied
            if not handled and any(type(fact) is AppProcessFact and fact.state == "exited" for fact in facts):
                return self._pending(conn, work_id, "awaiting_output_link")
            return self._finish(conn, work_id, "reconciled" if handled else "no_current_loss")

    def _finish(self, conn, work_id: int, result: str) -> int:
        conn.execute("UPDATE node_reconciliation_work SET completed_at=%s,result=%s "
                     "WHERE work_id=%s AND completed_at IS NULL",
                     (self.sessions.clock.utc(), result, work_id))
        return 1

    def _pending(self, conn, work_id: int, result: str) -> int:
        conn.execute("UPDATE node_reconciliation_work SET result=%s,next_attempt_at=%s WHERE work_id=%s",
                     (result, self.sessions.clock.utc() + 5, work_id))
        return 0
