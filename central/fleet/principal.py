"""OS command principal values and the generation admission guard.

This module does not authenticate a request. A future T1 gateway or T2 hardware
verifier must supply a principal; T0 serial claims and boot offers cannot do so.
The installation audience is an authenticated verifier result, never the
observational ``photo-wall-central-t0`` label.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from central.fleet.locks import lock_fleet_assets_in
from contracts.time import Clock

_DEVICE_ID = re.compile(r"device-[0-9a-f]{64}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class PrincipalError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SessionAdmission:
    """Time admitted under the session lock, reusable after later row waits."""

    admitted_at: float
    admitted_monotonic: float
    expires_at: float

    def ensure_current(self, clock: Clock) -> float:
        utc, monotonic = _clock_sample(clock)
        if monotonic < self.admitted_monotonic:
            raise PrincipalError("current_time_invalid")
        now = max(utc, self.admitted_at + monotonic - self.admitted_monotonic)
        if not math.isfinite(now):
            raise PrincipalError("current_time_invalid")
        if now >= self.expires_at:
            raise PrincipalError("os_command_session_unavailable")
        return now


@dataclass(frozen=True, slots=True)
class VerifiedOsPrincipal:
    """Validated verifier output; construction alone is no proof of provenance.

    Only a separately authenticated gateway/hardware verifier may pass this
    value to a future command route. T1 identifies an attachment, not a Pi.
    """

    device_id: str
    device_generation: int
    kernel_boot_id: UUID
    offer_id: UUID
    installation_audience: str
    trust_mode: Literal["t1", "t2"]
    command_session_id: UUID
    agent_key_sha256: str
    expires_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.device_id, str) or _DEVICE_ID.fullmatch(self.device_id) is None:
            raise PrincipalError("device_id_invalid")
        if (type(self.device_generation) is not int
                or not 1 <= self.device_generation <= 2**63 - 1):
            raise PrincipalError("device_generation_invalid")
        if not isinstance(self.kernel_boot_id, UUID):
            raise PrincipalError("kernel_boot_id_invalid")
        if not isinstance(self.offer_id, UUID):
            raise PrincipalError("offer_id_invalid")
        if (not isinstance(self.installation_audience, str)
                or not 1 <= len(self.installation_audience) <= 256
                or self.installation_audience == "photo-wall-central-t0"):
            raise PrincipalError("installation_audience_invalid")
        if self.trust_mode not in ("t1", "t2"):
            raise PrincipalError("trust_mode_invalid")
        if not isinstance(self.command_session_id, UUID):
            raise PrincipalError("command_session_id_invalid")
        if (not isinstance(self.agent_key_sha256, str)
                or _SHA256.fullmatch(self.agent_key_sha256) is None):
            raise PrincipalError("agent_key_sha256_invalid")
        if (type(self.expires_at) not in (int, float)
                or not math.isfinite(self.expires_at)):
            raise PrincipalError("expires_at_invalid")


def _clock_sample(clock: Clock) -> tuple[float, float]:
    utc, monotonic = clock.utc(), clock.monotonic()
    if (type(utc) not in (int, float) or not math.isfinite(utc)
            or type(monotonic) not in (int, float) or not math.isfinite(monotonic)):
        raise PrincipalError("current_time_invalid")
    return utc, monotonic


def require_current_principal_in(conn, principal: VerifiedOsPrincipal, *,
                                 clock: Clock) -> SessionAdmission:
    """Lock and validate the whole OS session before any future command admission.

    A generation change, retirement, revocation or expired session fails closed.
    Return the post-lock admitted time and a bounded monotonic continuation
    that callers can recheck after later attempt/artifact row waits.
    Callers must be inside the transaction that owns the attempted command
    effect, call this before locking attempt/artifact rows, and validate the
    attempt/drain separately. A caller using EquipmentDrain or Registry must
    first acquire Coordination then Runtime, before this guard takes the fleet
    offer lock. It must never acquire those locks after this guard.
    """
    if type(principal) is not VerifiedOsPrincipal:
        raise PrincipalError("verifier_principal_required")
    started_utc, started_monotonic = _clock_sample(clock)
    # Match fleet reservation and Registry.retire: offer lock, then device,
    # lifecycle and session rows. A multi-table FOR UPDATE has no documented
    # row-lock order and could deadlock with retirement. Current T0 offers
    # carry only a label and cannot satisfy the authenticated audience join;
    # F4 needs protected T1/T2 offer issuance or verifiable adoption first.
    lock_fleet_assets_in(conn)
    device = conn.execute("SELECT retired_at FROM devices WHERE device_id=%s FOR UPDATE",
                          (principal.device_id,)).fetchone()
    lifecycle = conn.execute("SELECT generation,revoked_at FROM fleet_device_lifecycle "
                             "WHERE device_id=%s FOR UPDATE", (principal.device_id,)).fetchone()
    session = conn.execute(
        "SELECT s.device_id,s.device_generation,s.kernel_boot_id,s.offer_id,"
        "s.installation_audience,s.trust_mode,s.agent_key_sha256,s.issued_at,"
        "s.expires_at,s.revoked_at,o.device_id AS offer_device_id,"
        "o.kernel_boot_id AS offer_boot_id,o.installation_audience AS offer_audience "
        "FROM fleet_os_command_sessions s "
        "JOIN fleet_boot_offers o ON o.offer_id=s.offer_id "
        "WHERE s.command_session_id=%s FOR UPDATE OF s",
        (principal.command_session_id,),
    ).fetchone()
    current_utc, current_monotonic = _clock_sample(clock)
    if current_monotonic < started_monotonic:
        raise PrincipalError("current_time_invalid")
    # A lock wait can outlive the session. A backward UTC step during that
    # wait must not restore authority that elapsed monotonic time consumed.
    now = max(current_utc, started_utc + (current_monotonic - started_monotonic))
    if not math.isfinite(now):
        raise PrincipalError("current_time_invalid")
    if (device is None or lifecycle is None or session is None
            or session["device_id"] != principal.device_id
            or session["device_generation"] != principal.device_generation
            or lifecycle["generation"] != principal.device_generation
            or session["kernel_boot_id"] != principal.kernel_boot_id
            or session["offer_id"] != principal.offer_id
            or session["installation_audience"] != principal.installation_audience
            or session["offer_device_id"] != principal.device_id
            or session["offer_boot_id"] != principal.kernel_boot_id
            or session["offer_audience"] != principal.installation_audience
            or session["trust_mode"] != principal.trust_mode
            or session["agent_key_sha256"] != principal.agent_key_sha256
            or session["expires_at"] != principal.expires_at
            or session["issued_at"] > now or session["expires_at"] <= now
            or session["revoked_at"] is not None or lifecycle["revoked_at"] is not None
            or device["retired_at"] is not None):
        raise PrincipalError("os_command_session_unavailable")
    return SessionAdmission(now, current_monotonic, principal.expires_at)
