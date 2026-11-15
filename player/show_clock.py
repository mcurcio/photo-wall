"""The Player's show timeline: Central's time, carried forward by this machine's monotonic clock.

Central's clock is the only timeline a plan is written in. Each round trip to Central bounds where
that timeline is now; between round trips the local monotonic clock, which only ever subtracts
from itself, carries it. The Pi's wall clock is never read, so its being wrong, or stepping, cannot
touch what is shown (2026-11-08: a Pi 975,402 s behind Central refused every plan and stayed black).

Stdlib and contracts.time only.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

from contracts.time import DRIFT_RATE

SampleOutcome = Literal["anchored", "kept", "impossible"]
ShowClockStatus = Literal["no_sample", "anchored"]


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
    for the process. Its bound is a quality, reported, never a gate (decision 0032): any answer
    anchors, however slow, and nothing here refuses a photo."""

    def __init__(self, monotonic: Callable[[], float], *, drift_rate: float = DRIFT_RATE) -> None:
        if not (math.isfinite(drift_rate) and drift_rate > 0):
            raise ValueError("drift_rate must be finite and positive")
        self._monotonic = monotonic
        self._drift_rate = drift_rate
        self._anchor: _Anchor | None = None
        self._rtt: float | None = None
        self._samples = 0
        self._accepted = 0
        self._rejected = 0

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
        return self._bound_at(anchor, self._monotonic())

    @property
    def diagnostics(self) -> ShowClockDiagnostics:
        anchor = self._anchor
        if anchor is None:
            return ShowClockDiagnostics("no_sample", self._rtt, None, None, self._samples,
                                        self._accepted, self._rejected)
        monotonic = self._monotonic()
        return ShowClockDiagnostics("anchored", self._rtt, self._bound_at(anchor, monotonic),
                                    monotonic - anchor.at, self._samples, self._accepted,
                                    self._rejected)

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

    def sample(self, *, server_time: float, sent_monotonic: float,
               received_monotonic: float) -> SampleOutcome:
        """One round trip to Central. `rtt` = received - sent; Central's reading was taken inside
        it, so at `received_monotonic` Central's time lies in I = [server_time, server_time + rtt].

        `impossible` (non-finite, or a negative rtt) is counted and changes nothing. Unanchored,
        any answer anchors at I's midpoint, bound rtt / 2, however slow. Anchored, the estimate's
        own interval [e - u, e + u] is met with I as J: J empty means the old anchor was wrong, and
        I anchors afresh; e inside J is `kept` (timebase unmoved, so round-trip jitter never moves a
        picture), its bound tightened when J's far edge from e is nearer than u and never loosened
        by a slow consistent answer; otherwise J's midpoint anchors, bound half J's width."""
        rtt = received_monotonic - sent_monotonic
        self._samples += 1
        self._rtt = rtt if math.isfinite(rtt) else None
        finite = all(math.isfinite(value) for value in (server_time, sent_monotonic,
                                                        received_monotonic, rtt))
        if not finite or rtt < 0:
            self._rejected += 1
            return "impossible"
        self._accepted += 1
        low, high = server_time, server_time + rtt
        anchor = self._anchor
        if anchor is not None:
            estimate = anchor.timebase + received_monotonic
            bound = self._bound_at(anchor, received_monotonic)
            j_low, j_high = max(estimate - bound, low), min(estimate + bound, high)
            if j_low <= j_high:
                if j_low <= estimate <= j_high:
                    far = max(estimate - j_low, j_high - estimate)
                    if far < bound:
                        self._anchor = replace(anchor, bound=far, at=received_monotonic)
                    return "kept"
                return self._anchor_on(j_low, j_high, received_monotonic)
        return self._anchor_on(low, high, received_monotonic)

    def _anchor_on(self, low: float, high: float, received_monotonic: float) -> SampleOutcome:
        half = (high - low) / 2
        self._anchor = _Anchor(low + half - received_monotonic, half, received_monotonic)
        return "anchored"

    def _bound_at(self, anchor: _Anchor, monotonic: float) -> float:
        return anchor.bound + self._drift_rate * (monotonic - anchor.at)
