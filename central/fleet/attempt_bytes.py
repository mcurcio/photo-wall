"""Authenticated, data-only access to one immutable AppAttempt's exact bytes.

This module does not authenticate a request or authorize a stop. A T1/T2
verifier must supply the principal to the separate data-only HTTP route;
production does not mount that route until D14 supplies a trusted verifier.
Expired-session or cross-boot recovery needs a separate authority path. An
already-open descriptor leases bytes for its stream after a later revocation.
"""

from __future__ import annotations

import asyncio
import os
from typing import Literal
from uuid import UUID

from central.assets.reader import Opened
from central.db import Database
from central.fleet.attempts import AttemptService, AttemptSnapshot
from central.fleet.bytes import OfferByteReader
from central.fleet.models import FleetError, OfferAsset
from central.fleet.principal import VerifiedOsPrincipal, require_current_principal_in
from contracts.time import Clock

AttemptRole = Literal["target", "fallback"]
_REPAIR_PHASES = frozenset(("stop_committed", "installing", "starting", "operational",
                            "observed_failed", "expired_unknown", "recovery_required"))


class AttemptByteAccess:
    """Resolve frozen attempt roots and verify bytes on the calling pod."""

    def __init__(self, db: Database, clock: Clock, bytes_reader: OfferByteReader) -> None:
        self.db, self.clock, self.bytes_reader = db, clock, bytes_reader

    def resolve(self, principal: VerifiedOsPrincipal, attempt_id: UUID,
                role: AttemptRole) -> OfferAsset:
        """Short transaction; never hold a database lock across cache I/O."""
        if not isinstance(attempt_id, UUID) or role not in ("target", "fallback"):
            raise FleetError("attempt_artifact_request_invalid", 422)
        snapshot = self._snapshot(principal, attempt_id)
        return snapshot.assets[0 if role == "target" else 1]

    def _snapshot(self, principal: VerifiedOsPrincipal, attempt_id: UUID) -> AttemptSnapshot:
        with self.db.transaction() as conn:
            # Full order: Coordination → Runtime (for mutators only) → fleet
            # offer → device → lifecycle → session → attempt → artifact refs.
            admission = require_current_principal_in(conn, principal, clock=self.clock)
            row = conn.execute("SELECT * FROM fleet_app_attempts WHERE attempt_id=%s "
                               "FOR SHARE", (attempt_id,)).fetchone()
            if (row is None or row["attempt_schema"] != 1
                    or row["device_id"] != principal.device_id
                    or row["device_generation"] != principal.device_generation
                    or row["kernel_boot_id"] != principal.kernel_boot_id
                    or row["offer_id"] != principal.offer_id
                    or row["installation_audience"] != principal.installation_audience
                    or row["command_session_id"] != principal.command_session_id
                    or row["root_released_at"] is not None):
                raise FleetError("attempt_artifact_unavailable", 404)
            # A superseded committed attempt may still need both frozen files
            # to reach a stable local state. Revocation of an unused attempt
            # cannot grant a new download. Device/session retirement is already
            # denied by require_current_principal_in above.
            if row["revoked_at"] is not None and not (
                row["phase"] in _REPAIR_PHASES and row["command_id"] is not None
                and row["drain_id"] is not None
            ):
                raise FleetError("attempt_artifact_unavailable", 404)
            if not AttemptService._rooted_in(conn, row):
                raise FleetError("attempt_root_unavailable", 503)
            admission.ensure_current(self.clock)
            return AttemptSnapshot.from_row(row)

    async def preflight(self, principal: VerifiedOsPrincipal,
                        attempt_id: UUID) -> AttemptSnapshot:
        """Hash both files here; this is not an agent RAM-stage receipt."""
        if not isinstance(attempt_id, UUID):
            raise FleetError("attempt_artifact_request_invalid", 422)
        before = await asyncio.to_thread(self._snapshot, principal, attempt_id)
        await self.bytes_reader.preflight(before.assets)
        after = await asyncio.to_thread(self._snapshot, principal, attempt_id)
        if after != before:
            raise FleetError("attempt_artifact_changed")
        return after

    async def open_role(self, principal: VerifiedOsPrincipal, attempt_id: UUID,
                        role: AttemptRole) -> Opened:
        """Caller owns the returned fd; revalidate after bounded cache I/O."""
        before = await asyncio.to_thread(self.resolve, principal, attempt_id, role)
        opened = await self.bytes_reader.open_exact(before)
        try:
            after = await asyncio.to_thread(self.resolve, principal, attempt_id, role)
            if after != before:
                raise FleetError("attempt_artifact_changed")
            return opened
        except BaseException:
            os.close(opened.fd)
            raise
