"""Narrow post-stop repair authority for a verified current loader-OS carrier.

This is an unmounted data-only seam. A lease never selects a target, authorizes
another stop, extends a drain, or accepts an artifact. Its id is a CAS marker,
not a bearer secret. The external T1/T2 verifier supplies every principal.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import ValidationError

from central.assets.reader import Opened
from central.db import Database
from central.fleet.attempts import AttemptService, AttemptSnapshot
from central.fleet.bytes import OfferByteReader
from central.fleet.models import FleetError, OfferAsset
from central.fleet.principal import VerifiedOsPrincipal, require_current_principal_in
from contracts.os_recovery_report import MAX_RECOVERY_REPORT_BYTES, OsRecoveryReport
from contracts.time import Clock

RecoveryRole = Literal["target", "fallback"]
ReportDisposition = Literal["stored", "replayed"]
_REPAIR_PHASES = frozenset(("stop_committed", "installing", "starting", "operational",
                            "observed_failed", "expired_unknown", "recovery_required"))


@dataclass(frozen=True, slots=True)
class RecoveryLease:
    lease_id: UUID
    attempt_id: UUID
    lease_sequence: int
    carrier_session_id: UUID
    issued_at: float
    expires_at: float
    attempt: AttemptSnapshot

    @classmethod
    def from_rows(cls, lease: dict, attempt: dict) -> RecoveryLease:
        return cls(lease["lease_id"], lease["attempt_id"], lease["lease_sequence"],
                   lease["carrier_session_id"], lease["issued_at"],
                   lease["expires_at"], AttemptSnapshot.from_row(attempt))


class RecoveryService:
    """CAS leases, exact retained byte access, and append-only repair claims."""

    MAX_LEASE_SECONDS = 120

    def __init__(self, db: Database, clock: Clock, bytes_reader: OfferByteReader) -> None:
        self.db, self.clock, self.bytes_reader = db, clock, bytes_reader

    @staticmethod
    def _eligible_in(conn, principal: VerifiedOsPrincipal, attempt_id: UUID, *,
                     mutate: bool) -> dict:
        # Caller already owns fleet → device → lifecycle → current session.
        # Retirement also takes the fleet lock, so this order cannot race it.
        player = conn.execute(
            "SELECT id,retired_at FROM players WHERE device_id=%s FOR SHARE",
            (principal.device_id,),
        ).fetchone()
        attempt = conn.execute(
            "SELECT * FROM fleet_app_attempts WHERE attempt_id=%s " +
            ("FOR UPDATE" if mutate else "FOR SHARE"), (attempt_id,),
        ).fetchone()
        if (player is None or player["retired_at"] is not None or attempt is None
                or attempt["attempt_schema"] != 1
                or attempt["device_id"] != principal.device_id
                or attempt["device_generation"] != principal.device_generation
                or attempt["command_session_id"] is None
                or attempt["command_id"] is None or attempt["drain_id"] is None
                or attempt["phase"] not in _REPAIR_PHASES
                or attempt["root_released_at"] is not None):
            raise FleetError("recovery_attempt_unavailable", 404)
        drain = conn.execute(
            "SELECT player_id,attempt_id,boot_id,fleet_drain_id,phase "
            "FROM equipment_drains WHERE player_id=%s FOR SHARE", (player["id"],),
        ).fetchone()
        if (drain is None or drain["phase"] != "stop_committed"
                or drain["attempt_id"] != str(attempt_id)
                or drain["boot_id"] != str(attempt["kernel_boot_id"])
                or drain["fleet_drain_id"] != attempt["drain_id"]):
            raise FleetError("recovery_drain_unavailable", 409)
        # A phase flag or manually linked drain alone is not a stop right.
        # The immutable permit proves this exact attempt/command/drain crossed
        # the ready-to-stop transaction. Its short deadline does not end the
        # later repair obligation.
        permit = conn.execute(
            "SELECT p.permit_id FROM fleet_app_stop_permits p "
            "JOIN fleet_app_commands c ON c.command_id=p.command_id "
            "WHERE p.attempt_id=%s AND p.command_id=%s AND p.drain_id=%s "
            "AND c.player_id=%s AND c.device_id=%s AND c.device_generation=%s "
            "AND c.command_session_id=%s",
            (attempt_id, attempt["command_id"], attempt["drain_id"], player["id"],
             principal.device_id, principal.device_generation,
             attempt["command_session_id"]),
        ).fetchone()
        if permit is None:
            raise FleetError("recovery_stop_permit_unavailable", 409)
        # A committed attempt may have been superseded or revoked while repair
        # remains obligatory. Explicit root release, generation revocation,
        # drain reconciliation, or lease revocation removes this authority.
        if not AttemptService._rooted_in(conn, attempt):
            raise FleetError("attempt_root_unavailable", 503)
        return attempt

    @staticmethod
    def _current_lease_in(conn, principal: VerifiedOsPrincipal, attempt_id: UUID,
                          lease_id: UUID) -> dict:
        lease = conn.execute(
            "SELECT * FROM fleet_recovery_leases WHERE attempt_id=%s AND lease_id=%s "
            "FOR SHARE", (attempt_id, lease_id),
        ).fetchone()
        if (lease is None or lease["device_id"] != principal.device_id
                or lease["device_generation"] != principal.device_generation
                or lease["carrier_session_id"] != principal.command_session_id
                or lease["carrier_boot_id"] != principal.kernel_boot_id
                or lease["carrier_offer_id"] != principal.offer_id
                or lease["carrier_audience"] != principal.installation_audience
                or lease["carrier_trust_mode"] != principal.trust_mode
                or lease["superseded_at"] is not None
                or lease["revoked_at"] is not None):
            raise FleetError("recovery_lease_unavailable", 403)
        return lease

    def claim(self, principal: VerifiedOsPrincipal, attempt_id: UUID, *, lease_id: UUID,
              expected_lease_id: UUID | None) -> RecoveryLease:
        """Replace one lease by exact CAS, including across a base reboot.

        Caller-chosen UUID makes a lost response retry safe; it conveys no
        authority without the current verifier principal and database checks.
        The previous lease remains immutable history after supersession.
        """
        if (not isinstance(attempt_id, UUID) or not isinstance(lease_id, UUID)
                or (expected_lease_id is not None
                    and not isinstance(expected_lease_id, UUID))
                or lease_id == expected_lease_id):
            raise FleetError("recovery_lease_request_invalid", 422)
        with self.db.transaction() as conn:
            admission = require_current_principal_in(conn, principal, clock=self.clock)
            attempt = self._eligible_in(conn, principal, attempt_id, mutate=True)
            latest = conn.execute(
                "SELECT * FROM fleet_recovery_leases WHERE attempt_id=%s "
                "ORDER BY lease_sequence DESC LIMIT 1 FOR UPDATE", (attempt_id,),
            ).fetchone()
            now = admission.ensure_current(self.clock)
            if latest is not None and latest["lease_id"] == lease_id:
                if (latest["predecessor_lease_id"] != expected_lease_id
                        or latest["carrier_session_id"] != principal.command_session_id
                        or latest["superseded_at"] is not None
                        or latest["revoked_at"] is not None
                        or latest["expires_at"] <= now):
                    raise FleetError("recovery_lease_conflict")
                admission.ensure_current(self.clock)
                return RecoveryLease.from_rows(latest, attempt)
            if ((latest is None and expected_lease_id is not None)
                    or (latest is not None and latest["lease_id"] != expected_lease_id)):
                raise FleetError("recovery_lease_conflict")
            # An explicit lifecycle revocation closes repair authority for
            # this attempt. Expiry and supersession remain CAS-replaceable.
            if latest is not None and latest["revoked_at"] is not None:
                raise FleetError("recovery_lease_revoked", 403)
            if latest is not None and now < latest["issued_at"]:
                raise FleetError("recovery_clock_regressed", 503)
            if conn.execute("SELECT 1 FROM fleet_recovery_leases WHERE lease_id=%s",
                            (lease_id,)).fetchone():
                raise FleetError("recovery_lease_conflict")
            expiry = min(principal.expires_at, now + self.MAX_LEASE_SECONDS)
            if expiry <= now:
                raise FleetError("recovery_lease_unavailable", 403)
            if latest is not None and latest["superseded_at"] is None:
                conn.execute("UPDATE fleet_recovery_leases SET superseded_at=%s "
                             "WHERE lease_id=%s", (now, latest["lease_id"]))
            row = conn.execute(
                "INSERT INTO fleet_recovery_leases(lease_id,attempt_id,lease_sequence,"
                "predecessor_lease_id,device_id,device_generation,issuing_session_id,"
                "carrier_session_id,carrier_boot_id,carrier_offer_id,carrier_audience,"
                "carrier_trust_mode,issued_at,expires_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (lease_id, attempt_id, 1 if latest is None else latest["lease_sequence"] + 1,
                 expected_lease_id, principal.device_id, principal.device_generation,
                 attempt["command_session_id"], principal.command_session_id,
                 principal.kernel_boot_id, principal.offer_id,
                 principal.installation_audience, principal.trust_mode, now, expiry),
            ).fetchone()
            admission.ensure_current(self.clock)
            return RecoveryLease.from_rows(row, attempt)

    def _snapshot(self, principal: VerifiedOsPrincipal, attempt_id: UUID,
                  lease_id: UUID) -> RecoveryLease:
        if not isinstance(attempt_id, UUID) or not isinstance(lease_id, UUID):
            raise FleetError("recovery_lease_request_invalid", 422)
        with self.db.transaction() as conn:
            admission = require_current_principal_in(conn, principal, clock=self.clock)
            attempt = self._eligible_in(conn, principal, attempt_id, mutate=False)
            lease = self._current_lease_in(conn, principal, attempt_id, lease_id)
            now = admission.ensure_current(self.clock)
            if lease["expires_at"] <= now:
                raise FleetError("recovery_lease_unavailable", 403)
            return RecoveryLease.from_rows(lease, attempt)

    def resolve(self, principal: VerifiedOsPrincipal, attempt_id: UUID,
                lease_id: UUID, role: RecoveryRole) -> OfferAsset:
        if role not in ("target", "fallback"):
            raise FleetError("recovery_artifact_role_invalid", 422)
        snapshot = self._snapshot(principal, attempt_id, lease_id)
        return snapshot.attempt.assets[0 if role == "target" else 1]

    async def preflight(self, principal: VerifiedOsPrincipal, attempt_id: UUID,
                        lease_id: UUID) -> RecoveryLease:
        before = await asyncio.to_thread(self._snapshot, principal, attempt_id, lease_id)
        await self.bytes_reader.preflight(before.attempt.assets)
        after = await asyncio.to_thread(self._snapshot, principal, attempt_id, lease_id)
        if after != before:
            raise FleetError("recovery_artifact_changed")
        return after

    async def open_role(self, principal: VerifiedOsPrincipal, attempt_id: UUID,
                        lease_id: UUID, role: RecoveryRole) -> Opened:
        before = await asyncio.to_thread(self.resolve, principal, attempt_id, lease_id, role)
        opened = await self.bytes_reader.open_exact(before)
        try:
            after = await asyncio.to_thread(self.resolve, principal, attempt_id, lease_id, role)
            if after != before:
                raise FleetError("recovery_artifact_changed")
            return opened
        except BaseException:
            os.close(opened.fd)
            raise

    def record(self, principal: VerifiedOsPrincipal,
               report: OsRecoveryReport) -> ReportDisposition:
        """Append a carrier claim; it cannot promote bytes or clear the drain."""
        if type(report) is not OsRecoveryReport:
            raise FleetError("recovery_report_invalid", 422)
        try:
            encoded = json.dumps(report.model_dump(mode="json", by_alias=True,
                                                   warnings="error"), sort_keys=True,
                                 separators=(",", ":")).encode("utf-8")
            validated = OsRecoveryReport.model_validate_json(encoded)
        except (TypeError, ValueError, ValidationError) as exc:
            raise FleetError("recovery_report_invalid", 422) from exc
        if validated != report or len(encoded) > MAX_RECOVERY_REPORT_BYTES:
            raise FleetError("recovery_report_invalid", 422)
        with self.db.transaction() as conn:
            admission = require_current_principal_in(conn, principal, clock=self.clock)
            if (report.device_id != principal.device_id
                    or report.device_generation != principal.device_generation
                    or report.installation_audience != principal.installation_audience
                    or report.kernel_boot_id != principal.kernel_boot_id
                    or report.offer_id != principal.offer_id
                    or report.command_session_id != principal.command_session_id):
                raise FleetError("recovery_report_principal_mismatch", 409)
            attempt = self._eligible_in(conn, principal, report.attempt_id, mutate=True)
            if (report.command_id != attempt["command_id"]
                    or report.drain_id != attempt["drain_id"]
                    or report.active_sha256 not in
                    (None, attempt["target_sha256"], attempt["fallback_sha256"])):
                raise FleetError("recovery_report_attempt_mismatch", 409)
            lease = self._current_lease_in(conn, principal, report.attempt_id,
                                           report.lease_id)
            now = admission.ensure_current(self.clock)
            if lease["expires_at"] <= now:
                raise FleetError("recovery_lease_unavailable", 403)
            existing = conn.execute(
                "SELECT report_json FROM fleet_recovery_observations "
                "WHERE attempt_id=%s AND carrier_session_id=%s AND report_sequence=%s",
                (report.attempt_id, principal.command_session_id, report.report_sequence),
            ).fetchone()
            if existing is not None:
                if existing["report_json"] != encoded.decode("utf-8"):
                    raise FleetError("recovery_report_replay_conflict")
                admission.ensure_current(self.clock)
                return "replayed"
            latest = conn.execute(
                "SELECT max(report_sequence) AS sequence FROM fleet_recovery_observations "
                "WHERE attempt_id=%s AND carrier_session_id=%s",
                (report.attempt_id, principal.command_session_id),
            ).fetchone()["sequence"]
            if latest is not None and report.report_sequence <= latest:
                raise FleetError("recovery_report_sequence_stale")
            conn.execute(
                "INSERT INTO fleet_recovery_observations(attempt_id,carrier_session_id,"
                "report_sequence,lease_id,report_json,received_at) "
                "VALUES(%s,%s,%s,%s,%s,%s)",
                (report.attempt_id, principal.command_session_id, report.report_sequence,
                 report.lease_id, encoded.decode("utf-8"), now),
            )
            if lease["expires_at"] <= admission.ensure_current(self.clock):
                raise FleetError("recovery_lease_unavailable", 403)
            return "stored"
