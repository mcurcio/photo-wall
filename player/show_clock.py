"""The Player's show timeline: Central's time, carried forward by this machine's monotonic clock.

Central's clock is the only timeline a plan is written in. Each round trip to Central bounds where
that timeline is now; between round trips the local monotonic clock, which only ever subtracts
from itself, carries it. The Pi's wall clock is never read, so its being wrong, or stepping, cannot
touch what is shown (2026-11-08: a Pi 975,402 s behind Central refused every plan and stayed black).

Stdlib only.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Final, Literal

MAX_UNCERTAINTY: Final = 0.100
"""Seconds. The most Central's time may be in doubt while the Player still claims to follow it."""

DRIFT_RATE: Final = 100e-6
"""Seconds per second: RFC 5905 dispersion, undisciplined crystals both ends."""

SampleOutcome = Literal["anchored", "kept", "too_slow", "impossible"]
ShowClockStatus = Literal["healthy", "no_sample", "too_uncertain"]


@dataclass(frozen=True, slots=True)
class ShowClockDiagnostics:
    """Bounded, JSON-safe readings of the clock: None rather than a non-finite value."""

    status: ShowClockStatus
    rtt: float | None = None
    uncertainty: float | None = None
    sample_age: float | None = None
    samples: int = 0
    accepted: int = 0
    rejected: int = 0


@dataclass(frozen=True, slots=True)
class _Anchor:
    """One value, so a reader on another thread never sees half of a new anchor."""

    timebase: float             # Central's time less this machine's monotonic time
    bound: float                # how far the estimate can be off, as of `at`
    at: float                   # the monotonic reading that bound was taken at


class ShowClock:
    """Central's reading carried forward by this machine's monotonic clock. Only the local
    monotonic clock is subtracted from itself. There is no forget: once anchored it stays anchored
    for the process."""

    def __init__(self, monotonic: Callable[[], float], *, max_uncertainty: float = MAX_UNCERTAINTY,
                 drift_rate: float = DRIFT_RATE) -> None:
        if not all(math.isfinite(value) and value > 0 for value in (max_uncertainty, drift_rate)):
            raise ValueError("clock thresholds must be finite and positive")
        self._monotonic = monotonic
        self._max_uncertainty = max_uncertainty
        self._drift_rate = drift_rate
        self._anchor: _Anchor | None = None
        self._rtt: float | None = None
        self._samples = 0
        self._accepted = 0
        self._rejected = 0

    @property
    def max_uncertainty(self) -> float:
        return self._max_uncertainty

    @property
    def anchored(self) -> bool:
        return self._anchor is not None

    @property
    def uncertainty(self) -> float:
        """The bound at the last kept or anchored sample, plus drift over its age; infinite until
        anchored."""
        anchor = self._anchor
        if anchor is None:
            return math.inf
        return anchor.bound + self._drift_rate * (self._monotonic() - anchor.at)

    @property
    def diagnostics(self) -> ShowClockDiagnostics:
        anchor = self._anchor
        if anchor is None:
            status: ShowClockStatus = "no_sample"
            uncertainty = sample_age = None
        else:
            status = "healthy" if self.healthy() else "too_uncertain"
            uncertainty = self.uncertainty
            sample_age = self._monotonic() - anchor.at
        return ShowClockDiagnostics(status, self._rtt, uncertainty, sample_age, self._samples,
                                    self._accepted, self._rejected)

    def now(self) -> float:
        """Plan timeline now; -inf until anchored (nothing is due, nothing has expired); always
        freewheels."""
        anchor = self._anchor
        return -math.inf if anchor is None else anchor.timebase + self._monotonic()

    def timebase(self) -> float:
        """`now()` less the monotonic reading: constant while the anchor stands; -inf until
        anchored."""
        anchor = self._anchor
        return -math.inf if anchor is None else anchor.timebase

    def healthy(self) -> bool:
        return self.anchored and self.uncertainty <= self._max_uncertainty

    def sample(self, *, server_time: float, sent_monotonic: float,
               received_monotonic: float) -> SampleOutcome:
        """One round trip to Central. `rtt` = received - sent; Central's reading was taken inside
        it, so at `received_monotonic` Central's time lies in [server_time, server_time + rtt].

        `impossible` (non-finite, or a negative rtt) and `too_slow` (rtt / 2 past max_uncertainty)
        are counted and change nothing. Otherwise, a current estimate inside that interval, with
        its far edge within max_uncertainty, is `kept`: anchor and timebase unmoved, the bound
        becomes that distance, so round-trip jitter never moves a picture. Anything else is
        `anchored` at the midpoint, bound rtt / 2: Central's clock moved, or this is the first."""
        rtt = received_monotonic - sent_monotonic
        self._samples += 1
        self._rtt = rtt if math.isfinite(rtt) else None
        finite = all(math.isfinite(value) for value in (server_time, sent_monotonic,
                                                        received_monotonic, rtt))
        if not finite or rtt < 0:
            self._rejected += 1
            return "impossible"
        half = rtt / 2
        if half > self._max_uncertainty:
            self._rejected += 1
            return "too_slow"
        self._accepted += 1
        low, high = server_time, server_time + rtt
        anchor = self._anchor
        if anchor is not None:
            estimate = anchor.timebase + received_monotonic
            far_edge = max(estimate - low, high - estimate)
            if low <= estimate <= high and far_edge <= self._max_uncertainty:
                self._anchor = replace(anchor, bound=far_edge, at=received_monotonic)
                return "kept"
        self._anchor = _Anchor(server_time + half - received_monotonic, half, received_monotonic)
        return "anchored"
