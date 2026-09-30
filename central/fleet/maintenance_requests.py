"""Durable operator maintenance intent, separate from command and Runtime effects.

Every write shares the fleet asset lock with policy selection and retirement,
then locks device and lifecycle rows. The selected release is frozen into the
request. An operator request never queues an AppAttempt or authorizes a stop.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from central.fleet.models import (
    Artifact,
    FleetError,
    MaintenanceRequestCancel,
    MaintenanceRequestWrite,
)
from contracts.player_payload import FORMAT as PAYLOAD_FORMAT


class MaintenanceRequestStore:
    """Transaction-scoped writes and the single status projection query."""

    @staticmethod
    def lock_current_device_in(conn, device_id: str, *, expected_generation: int | None) -> int:
        device = conn.execute(
            "SELECT retired_at FROM devices WHERE device_id=%s FOR UPDATE", (device_id,)
        ).fetchone()
        if device is None:
            raise FleetError("device_not_found", 404)
        lifecycle = conn.execute(
            "SELECT generation,revoked_at FROM fleet_device_lifecycle "
            "WHERE device_id=%s FOR UPDATE", (device_id,)
        ).fetchone()
        if (device["retired_at"] is not None or lifecycle is None
                or lifecycle["revoked_at"] is not None):
            raise FleetError("device_retired")
        generation = lifecycle["generation"]
        if expected_generation is not None and generation != expected_generation:
            raise FleetError("device_generation_conflict")
        return generation

    @staticmethod
    def _expire_in(conn, *, device_id: str, generation: int, now: float) -> None:
        conn.execute(
            "UPDATE fleet_maintenance_requests SET status='expired',revision=revision+1,"
            "reason='request_expired',changed_at=%s "
            "WHERE device_id=%s AND device_generation=%s AND status='queued' "
            "AND expires_at<=%s", (now, device_id, generation, now)
        )

    @staticmethod
    def _document(row: dict[str, Any], *, read_at: float | None = None) -> dict[str, Any]:
        expired_in_projection = (read_at is not None and row["status"] == "queued"
                                 and row["expires_at"] <= read_at)
        result = {
            "request_id": str(row["request_id"]),
            "device_id": row["device_id"],
            "device_generation": row["device_generation"],
            "revision": row["revision"],
            "status": "expired" if expired_in_projection else row["status"],
            "policy_source": row["policy_source"],
            "policy_revision": row["policy_revision"],
            "target": {
                "tag": row["target_tag"], "sha256": row["target_sha256"],
                "size": row["target_size"], "format": row["target_format"],
                "base_abi": row["target_base_abi"],
                "source_manifest": row["target_source_manifest"],
            },
            "requested_at": row["requested_at"],
            "expires_at": row["expires_at"],
            "reason": "request_expired" if expired_in_projection else row["reason"],
        }
        return result

    @classmethod
    def latest_by_device_in(cls, conn, *, device_ids: list[str],
                            read_at: float) -> dict[str, dict[str, Any]]:
        """One current-generation request per device from the caller's MVCC read."""
        if not device_ids:
            return {}
        rows = conn.execute(
            "SELECT DISTINCT ON(request.device_id) request.* "
            "FROM fleet_maintenance_requests AS request "
            "JOIN fleet_device_lifecycle AS lifecycle "
            "ON lifecycle.device_id=request.device_id "
            "AND lifecycle.generation=request.device_generation "
            "JOIN devices AS device ON device.device_id=request.device_id "
            "WHERE device.retired_at IS NULL AND lifecycle.revoked_at IS NULL "
            "AND request.device_id=ANY(%s) "
            "ORDER BY request.device_id,request.request_ordinal DESC",
            (device_ids,),
        ).fetchall()
        return {row["device_id"]: cls._document(row, read_at=read_at) for row in rows}

    @classmethod
    def create_in(cls, conn, device_id: str, generation: int,
                  request: MaintenanceRequestWrite, *, selected: dict[str, Any],
                  validate_app: Callable[[Any, Artifact], None], now: float) -> dict[str, Any]:
        """Caller holds fleet, device, lifecycle locks, in that order."""
        cls._expire_in(conn, device_id=device_id, generation=generation, now=now)
        existing = conn.execute(
            "SELECT * FROM fleet_maintenance_requests WHERE request_id=%s FOR UPDATE",
            (request.request_id,),
        ).fetchone()
        if existing is not None:
            if (existing["device_id"] != device_id
                    or existing["device_generation"] != request.expected_device_generation
                    or existing["policy_source"] != request.expected_policy_source
                    or existing["policy_revision"] != request.expected_policy_revision
                    or existing["target_sha256"] != request.expected_target_sha256
                    or existing["ttl_seconds"] != request.ttl_seconds):
                raise FleetError("maintenance_request_id_conflict")
            return cls._document(existing, read_at=now)

        target = selected["target"]
        if (selected["source"] != request.expected_policy_source
                or selected["revision"] != request.expected_policy_revision
                or target is None or target["sha256"] != request.expected_target_sha256):
            raise FleetError("maintenance_policy_conflict")
        if target["format"] != PAYLOAD_FORMAT:
            raise FleetError("maintenance_target_unavailable")
        validate_app(conn, Artifact(tag=target["tag"], sha256=target["sha256"],
                                    size=target["size"]))
        release = conn.execute(
            "SELECT payload_sha256,payload_size,payload_format,payload_base_abi,"
            "payload_source_manifest FROM app_releases WHERE tag=%s FOR SHARE",
            (target["tag"],),
        ).fetchone()
        if (release is None or release["payload_sha256"] != target["sha256"]
                or release["payload_size"] != target["size"]
                or release["payload_format"] != PAYLOAD_FORMAT
                or not release["payload_base_abi"]
                or release["payload_source_manifest"] != "manifest.v2.json"):
            raise FleetError("maintenance_target_unavailable")
        live = conn.execute(
            "SELECT request_id FROM fleet_maintenance_requests "
            "WHERE device_id=%s AND device_generation=%s AND status='queued'",
            (device_id, generation),
        ).fetchone()
        if live is not None:
            raise FleetError("maintenance_request_already_queued")
        row = conn.execute(
            "INSERT INTO fleet_maintenance_requests(request_id,device_id,device_generation,"
            "status,policy_source,policy_revision,target_tag,target_sha256,target_size,"
            "target_format,target_base_abi,target_source_manifest,ttl_seconds,requested_at,"
            "expires_at,changed_at,reason) "
            "VALUES(%s,%s,%s,'queued',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "'awaiting_command_authority') RETURNING *",
            (request.request_id, device_id, generation, selected["source"],
             selected["revision"], target["tag"], target["sha256"], target["size"],
             PAYLOAD_FORMAT, release["payload_base_abi"],
             release["payload_source_manifest"], request.ttl_seconds, now,
             now + request.ttl_seconds, now),
        ).fetchone()
        return cls._document(row)

    @classmethod
    def cancel_in(cls, conn, device_id: str, generation: int, request_id: UUID,
                  request: MaintenanceRequestCancel, *, now: float) -> dict[str, Any]:
        """CAS cancellation never retracts a dispatched execution."""
        cls._expire_in(conn, device_id=device_id, generation=generation, now=now)
        row = conn.execute(
            "SELECT * FROM fleet_maintenance_requests WHERE request_id=%s "
            "AND device_id=%s FOR UPDATE", (request_id, device_id),
        ).fetchone()
        if row is None:
            raise FleetError("maintenance_request_not_found", 404)
        if row["device_generation"] != generation:
            raise FleetError("device_generation_conflict")
        if row["revision"] != request.expected_revision:
            raise FleetError("maintenance_request_revision_conflict")
        if row["status"] != "queued":
            raise FleetError("maintenance_request_not_queued")
        canceled = conn.execute(
            "UPDATE fleet_maintenance_requests SET status='canceled',revision=revision+1,"
            "changed_at=%s,reason='operator_canceled' WHERE request_id=%s RETURNING *",
            (now, request_id),
        ).fetchone()
        return cls._document(canceled)

    @staticmethod
    def dispatch_in(conn, request_id: UUID, *, expected_revision: int,
                    now: float) -> dict[str, Any]:
        """Consume a queued intent inside the command owner's transaction.

        The caller has already locked and validated the request, attempt and
        drain. This transition cannot by itself issue a command or permit.
        """
        row = conn.execute(
            "UPDATE fleet_maintenance_requests SET status='dispatched',"
            "revision=revision+1,changed_at=GREATEST(changed_at,%s),"
            "reason='command_dispatched' WHERE request_id=%s AND status='queued' "
            "AND revision=%s AND expires_at>%s RETURNING *",
            (now, request_id, expected_revision, now),
        ).fetchone()
        if row is None:
            raise FleetError("maintenance_request_not_dispatchable")
        return row
