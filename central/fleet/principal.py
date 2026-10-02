"""Session admission time shared by the node session and command owners.

This module does not authenticate a request; node sessions do.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from contracts.time import Clock


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


def _clock_sample(clock: Clock) -> tuple[float, float]:
    utc, monotonic = clock.utc(), clock.monotonic()
    if (type(utc) not in (int, float) or not math.isfinite(utc)
            or type(monotonic) not in (int, float) or not math.isfinite(monotonic)):
        raise PrincipalError("current_time_invalid")
    return utc, monotonic
