"""Read-only fleet management projection from one consistent database snapshot.

The app policy, T0 serial claims and F0 app control are projected elsewhere.
These records describe Central's attempt bookkeeping and T1/T2-carried
OS claims. Neither a recorded session nor a reported process proves physical
connectivity, rendered Output content, or artifact acceptance.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from contracts.os_attempt_report import OsAttemptReport


def _age(read_at: float, received_at: float) -> float:
    return max(0.0, read_at - received_at)


def _session_doc(row: dict | None, read_at: float) -> dict[str, Any]:
    if row is None:
        return {"state": "none", "source": "none", "assurance": "none"}
    state = ("clock_inconsistent" if row["issued_at"] > read_at else
             "recorded_unexpired" if row["expires_at"] > read_at else "expired")
    return {
        "state": state,
        "source": "commissioned_os_session", "assurance": "record_only",
        "command_session_id": str(row["command_session_id"]),
        "trust_mode": row["trust_mode"], "boot_id": str(row["kernel_boot_id"]),
        "offer_id": str(row["offer_id"]), "issued_at": row["issued_at"],
        "expires_at": row["expires_at"],
        "physical_connectivity": "unknown",
    }


def _attempt_doc(row: dict | None) -> dict[str, Any]:
    if row is None:
        return {"state": "none", "source": "none"}
    return {
        "state": row["phase"], "source": "central_attempt_record",
        "selection": "nonqueued_by_created_at" if row["phase"] != "queued"
                     else "queued_record_by_created_at",
        "queued_count": row["queued_count"],
        "nonqueued_count": row["nonqueued_count"],
        "attempt_id": str(row["attempt_id"]),
        "target_digest": row["target_sha256"],
        "fallback_digest": row["fallback_sha256"],
        "issuing_session_id": str(row["command_session_id"])
        if row["command_session_id"] else None,
        "command_id": str(row["command_id"]) if row["command_id"] else None,
        "drain_id": str(row["drain_id"]) if row["drain_id"] else None,
        "attempt_revoked_at": row["revoked_at"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "root_retention": "retention_obligation",
        "byte_availability": "unknown",
    }


def _attempt_report_doc(row: dict | None, attempt: dict | None,
                        session: dict | None, read_at: float) -> dict[str, Any]:
    if row is None or attempt is None:
        return {"state": "none", "source": "none"}
    base = {"source": "authenticated_os_carrier_claim",
            "assurance": "t1_t2_carrier_claim",
            "trust_mode": row["carrier_trust_mode"],
            "report_sequence": row["report_sequence"],
            "received_at": row["received_at"],
            "age_seconds": _age(read_at, row["received_at"]),
            "physical_output": "unknown"}
    try:
        report = OsAttemptReport.model_validate_json(row["report_json"])
    except (ValueError, ValidationError):
        return {**base, "state": "invalid_stored_report"}
    if (report.attempt_id != attempt["attempt_id"]
            or report.device_id != attempt["device_id"]
            or report.device_generation != attempt["device_generation"]
            or report.command_session_id != attempt["command_session_id"]
            or report.installation_audience != attempt["installation_audience"]
            or report.kernel_boot_id != attempt["kernel_boot_id"]
            or report.offer_id != attempt["offer_id"]
            or report.command_id != attempt["command_id"]
            or report.drain_id != attempt["drain_id"]
            or report.report_sequence != row["report_sequence"]
            or row["command_session_id"] != attempt["command_session_id"]):
        return {**base, "state": "context_mismatch"}
    session_current = (session is not None
                       and session["command_session_id"] == row["command_session_id"]
                       and session["trust_mode"] == row["carrier_trust_mode"]
                       and session["kernel_boot_id"] == report.kernel_boot_id
                       and session["offer_id"] == report.offer_id
                       and session["issued_at"] <= read_at
                       and session["expires_at"] > read_at)
    return {
        **base, "state": "reported", "executor_state": report.executor_state,
        "active_digest": report.active_sha256,
        "running_digest": report.running_sha256,
        "process": report.running_process.model_dump() if report.running_process else None,
        "fault_code": report.fault_code,
        "boot_id": str(report.kernel_boot_id),
        "carrier_session_relation": "recorded_current" if session_current
                                    else "historical_or_unavailable",
    }


def management_status_in(conn, device_ids: Sequence[str], *,
                         read_at: float) -> dict[str, dict[str, Any]]:
    """Read all devices set-wise within FleetService's repeatable-read cut."""
    if not device_ids:
        return {}
    ids = list(device_ids)
    attempts = {row["device_id"]: row for row in conn.execute(
        "WITH ranked AS (SELECT a.*,"
        "row_number() OVER (PARTITION BY a.device_id ORDER BY "
        "CASE WHEN a.phase='queued' THEN 1 ELSE 0 END,"
        "a.created_at DESC,a.attempt_id DESC) AS display_rank,"
        "count(*) FILTER (WHERE a.phase='queued') OVER "
        "(PARTITION BY a.device_id) AS queued_count,"
        "count(*) FILTER (WHERE a.phase<>'queued') OVER "
        "(PARTITION BY a.device_id) AS nonqueued_count "
        "FROM fleet_app_attempts AS a "
        "JOIN fleet_device_lifecycle AS l ON l.device_id=a.device_id "
        "WHERE a.device_id=ANY(%s) AND a.attempt_schema=1 "
        "AND a.device_generation=l.generation AND l.revoked_at IS NULL "
        "AND a.root_released_at IS NULL "
        "AND (a.revoked_at IS NULL OR (a.phase NOT IN ('queued','prepared') "
        "AND a.command_id IS NOT NULL AND a.drain_id IS NOT NULL))) "
        "SELECT * FROM ranked WHERE display_rank=1", (ids,),
    ).fetchall()}
    sessions = {row["device_id"]: row for row in conn.execute(
        "SELECT s.* FROM fleet_os_command_sessions AS s "
        "JOIN fleet_device_lifecycle AS l ON l.device_id=s.device_id "
        "WHERE s.device_id=ANY(%s) AND s.device_generation=l.generation "
        "AND l.revoked_at IS NULL AND s.revoked_at IS NULL", (ids,),
    ).fetchall()}
    attempt_ids = [row["attempt_id"] for row in attempts.values()]
    reports = {}
    if attempt_ids:
        reports = {row["attempt_id"]: row for row in conn.execute(
            "SELECT latest.* FROM unnest(%s::uuid[]) AS wanted(attempt_id) "
            "JOIN LATERAL (SELECT report.* FROM fleet_os_attempt_reports AS report "
            "WHERE report.attempt_id=wanted.attempt_id "
            "ORDER BY report.report_sequence DESC LIMIT 1) AS latest ON TRUE",
            (attempt_ids,),
        ).fetchall()}
    result = {}
    for device_id in ids:
        attempt = attempts.get(device_id)
        session = sessions.get(device_id)
        report = reports.get(attempt["attempt_id"]) if attempt else None
        result[device_id] = {
            "attempt": _attempt_doc(attempt),
            "os_session": _session_doc(session, read_at),
            "latest_attempt_report": _attempt_report_doc(
                report, attempt, session, read_at),
        }
    return result
