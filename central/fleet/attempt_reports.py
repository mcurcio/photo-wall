"""Store bounded OS attempt reports under an already verified OS principal.

The T1/T2 carrier is authenticated by a future verifier, not by this service.
Report contents, including any local app proof, remain claims and cannot mark
an artifact accepted or an Output healthy.
"""

from __future__ import annotations

import json
import math
from typing import Literal

from pydantic import ValidationError

from central.db import Database
from central.fleet.models import FleetError
from central.fleet.principal import (
    PrincipalError,
    VerifiedOsPrincipal,
    require_current_principal_in,
)
from contracts.os_attempt_report import MAX_ATTEMPT_REPORT_BYTES, OsAttemptReport
from contracts.time import Clock

ReportDisposition = Literal["stored", "replayed"]
_REPORT_PHASES = frozenset(("stop_committed", "installing", "starting", "operational",
                            "observed_failed", "expired_unknown", "recovery_required"))


class AttemptReportStore:
    """Append claims in per-attempt order; never interpret them as acceptance."""

    def __init__(self, db: Database, clock: Clock) -> None:
        self.db, self.clock = db, clock

    def record(self, principal: VerifiedOsPrincipal,
               report: OsAttemptReport) -> ReportDisposition:
        if type(report) is not OsAttemptReport:
            raise FleetError("attempt_report_invalid", 422)
        try:
            encoded = json.dumps(report.model_dump(mode="json", by_alias=True,
                                                   warnings="error"),
                                 sort_keys=True, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise FleetError("attempt_report_invalid", 422) from exc
        if len(encoded) > MAX_ATTEMPT_REPORT_BYTES:
            raise FleetError("attempt_report_too_large", 422)
        # Pydantic model_copy(update=...) bypasses validation. Reparse at this
        # boundary before an otherwise authenticated report can be persisted.
        try:
            validated = OsAttemptReport.model_validate_json(encoded)
        except ValidationError as exc:
            raise FleetError("attempt_report_invalid", 422) from exc
        if validated != report:
            raise FleetError("attempt_report_invalid", 422)
        started_monotonic = self.clock.monotonic()
        if (type(started_monotonic) not in (int, float)
                or not math.isfinite(started_monotonic)):
            raise PrincipalError("current_time_invalid")
        now = self.clock.utc()
        with self.db.transaction() as conn:
            # No transport is mounted. A caller must supply a T1/T2 verifier
            # result; T0 observations cannot construct current authority.
            require_current_principal_in(conn, principal, now=now)
            if (report.device_id != principal.device_id
                    or report.device_generation != principal.device_generation
                    or report.kernel_boot_id != principal.kernel_boot_id
                    or report.offer_id != principal.offer_id
                    or report.installation_audience != principal.installation_audience
                    or report.command_session_id != principal.command_session_id):
                raise FleetError("attempt_report_principal_mismatch", 409)
            attempt = conn.execute(
                "SELECT * FROM fleet_app_attempts WHERE attempt_id=%s FOR UPDATE",
                (report.attempt_id,),
            ).fetchone()
            if (attempt is None or attempt["attempt_schema"] != 1
                    or attempt["device_id"] != principal.device_id
                    or attempt["device_generation"] != principal.device_generation
                    or attempt["kernel_boot_id"] != principal.kernel_boot_id
                    or attempt["offer_id"] != principal.offer_id
                    or attempt["installation_audience"] != principal.installation_audience
                    or attempt["command_id"] != report.command_id
                    or attempt["drain_id"] != report.drain_id
                    or attempt["phase"] not in _REPORT_PHASES):
                raise FleetError("attempt_report_attempt_mismatch", 409)
            # The initial guard may have waited on fleet/session or attempt
            # locks. Recheck expiry at the actual admission point, without
            # reacquiring earlier locks after the attempt lock. Monotonic
            # elapsed time also closes a backward UTC step during that wait.
            current_utc = self.clock.utc()
            current_monotonic = self.clock.monotonic()
            if (type(current_utc) not in (int, float) or not math.isfinite(current_utc)
                    or type(current_monotonic) not in (int, float)
                    or not math.isfinite(current_monotonic)
                    or current_monotonic < started_monotonic):
                raise PrincipalError("current_time_invalid")
            received_at = max(current_utc, now + (current_monotonic - started_monotonic))
            if not math.isfinite(received_at):
                raise PrincipalError("current_time_invalid")
            if received_at >= principal.expires_at:
                raise PrincipalError("os_command_session_unavailable")
            # Revocation of a committed attempt does not erase the local repair
            # obligation; the current OS session can still report its outcome.
            existing = conn.execute(
                "SELECT report_json FROM fleet_os_attempt_reports "
                "WHERE attempt_id=%s AND report_sequence=%s",
                (report.attempt_id, report.report_sequence),
            ).fetchone()
            if existing is not None:
                if existing["report_json"] != encoded.decode("utf-8"):
                    raise FleetError("attempt_report_replay_conflict", 409)
                return "replayed"
            latest = conn.execute(
                "SELECT max(report_sequence) AS sequence FROM fleet_os_attempt_reports "
                "WHERE attempt_id=%s", (report.attempt_id,),
            ).fetchone()["sequence"]
            if latest is not None and report.report_sequence <= latest:
                raise FleetError("attempt_report_sequence_stale", 409)
            conn.execute(
                "INSERT INTO fleet_os_attempt_reports(attempt_id,report_sequence,"
                "command_session_id,carrier_trust_mode,report_json,received_at) "
                "VALUES(%s,%s,%s,%s,%s,%s)",
                (report.attempt_id, report.report_sequence, principal.command_session_id,
                 principal.trust_mode, encoded.decode("utf-8"), received_at),
            )
            return "stored"
