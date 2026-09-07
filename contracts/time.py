"""Clock interfaces and safe UTC-to-monotonic scheduling; no pipeline time coupling."""

import math
import time
from dataclasses import dataclass
from typing import Literal, Protocol


class Clock(Protocol):
    def utc(self) -> float: ...
    def monotonic(self) -> float: ...


class SystemClock:
    def utc(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass
class ManualClock:
    wall: float
    mono: float = 0

    def __post_init__(self) -> None:
        if not math.isfinite(self.wall) or not math.isfinite(self.mono):
            raise ValueError("invalid initial clock")

    def utc(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        if seconds < 0 or not math.isfinite(seconds):
            raise ValueError("monotonic time cannot move backward or become nonfinite")
        self.wall += seconds
        self.mono += seconds

    def step_utc(self, seconds: float) -> None:
        if not math.isfinite(seconds):
            raise ValueError("invalid clock step")
        self.wall += seconds


ClockStatus = Literal[
    "healthy",
    "no_sample",
    "player_mismatch",
    "stale_epoch",
    "transport_time",
    "transport_drift",
    "uncertainty",
    "apply_age",
    "apply_drift",
    "mapping_age",
    "clock_step",
]


@dataclass(frozen=True)
class ClockDiagnostics:
    """Bounded raw probe measurements and cumulative process-local counters."""

    status: ClockStatus
    rtt: float | None = None
    central_offset: float | None = None
    apply_age: float | None = None
    drift: float | None = None
    transport_drift: float | None = None
    apply_drift: float | None = None
    mapping_age: float | None = None
    step: float | None = None
    uncertainty: float | None = None
    samples: int = 0
    accepted: int = 0
    rejected: int = 0


def _bounded(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return max(-86400.0, min(86400.0, value))


class TimeMapping:
    """A step invalidates coordinated readiness; remap only from a new trusted sample."""

    def __init__(self, clock: Clock, max_uncertainty: float = .100, max_step: float = .250,
                 max_age: float = 30):
        if any(not math.isfinite(v) or v <= 0 for v in (max_uncertainty, max_step, max_age)):
            raise ValueError("clock thresholds must be finite and positive")
        self.clock = clock
        self.max_uncertainty = max_uncertainty
        self.max_step = max_step
        self.max_age = max_age
        self._sample: tuple[float, float] | None = None
        self.uncertainty = math.inf
        self._samples = 0
        self._accepted = 0
        self._rejected = 0
        self._diagnostics = ClockDiagnostics("no_sample")

    @property
    def diagnostics(self) -> ClockDiagnostics:
        return self._diagnostics

    def _record(self, status: ClockStatus, *, accepted: bool = False, **values) -> bool:
        self._samples += 1
        if accepted:
            self._accepted += 1
        else:
            self._rejected += 1
            self._sample = None
            self.uncertainty = math.inf
        self._diagnostics = ClockDiagnostics(
            status=status,
            samples=self._samples,
            accepted=self._accepted,
            rejected=self._rejected,
            **{name: _bounded(value) for name, value in values.items()},
        )
        return accepted

    def establish(self, uncertainty: float) -> None:
        if uncertainty < 0 or not math.isfinite(uncertainty):
            raise ValueError("invalid uncertainty")
        sample = (self.clock.utc(), self.clock.monotonic())
        if not all(math.isfinite(v) for v in sample):
            self._sample = None
            raise ValueError("invalid clock sample")
        self._sample = sample
        self.uncertainty = uncertainty
        self._record("healthy", accepted=True, uncertainty=uncertainty, mapping_age=0)

    def apply_probe(
        self,
        *,
        sample_epoch: int,
        authority_epoch: int,
        sample_player_id: str | None = None,
        player_id: str | None = None,
        server_time: float,
        sent_utc: float,
        sent_monotonic: float,
        received_utc: float,
        received_monotonic: float,
        applied_utc: float,
        applied_monotonic: float,
        max_apply_age: float = 1,
        max_drift: float = .010,
    ) -> bool:
        """Validate and apply one independent authenticated central-time probe."""
        rtt = received_monotonic - sent_monotonic
        central_offset = server_time - (sent_utc + rtt / 2)
        apply_age = applied_monotonic - received_monotonic
        transport_drift = (received_utc - sent_utc) - rtt
        apply_drift = (applied_utc - received_utc) - apply_age
        uncertainty = rtt / 2 + abs(central_offset)
        values = dict(
            rtt=rtt,
            central_offset=central_offset,
            apply_age=apply_age,
            drift=max(abs(transport_drift), abs(apply_drift)),
            transport_drift=transport_drift,
            apply_drift=apply_drift,
            mapping_age=0,
            step=0,
            uncertainty=uncertainty,
        )
        if sample_player_id != player_id:
            return self._record("player_mismatch", **values)
        if sample_epoch != authority_epoch:
            return self._record("stale_epoch", **values)
        if not all(math.isfinite(value) for value in (
            server_time,
            sent_utc,
            sent_monotonic,
            received_utc,
            received_monotonic,
            applied_utc,
            applied_monotonic,
            rtt,
        )) or rtt < 0:
            return self._record("transport_time", **values)
        if abs(transport_drift) > max_drift:
            return self._record("transport_drift", **values)
        if not math.isfinite(uncertainty) or uncertainty > self.max_uncertainty:
            return self._record("uncertainty", **values)
        if not 0 <= apply_age <= max_apply_age:
            return self._record("apply_age", **values)
        if abs(apply_drift) > max_drift:
            return self._record("apply_drift", **values)
        self._sample = applied_utc, applied_monotonic
        self.uncertainty = uncertainty
        return self._record("healthy", accepted=True, **values)

    def healthy(self) -> bool:
        if self._sample is None or self.uncertainty > self.max_uncertainty:
            return False
        utc, mono = self._sample
        now_utc, now_mono = self.clock.utc(), self.clock.monotonic()
        age = now_mono - mono
        step = now_utc - (utc + age)
        if not all(math.isfinite(v) for v in (now_utc, now_mono, age, step)):
            self._sample = None
            self._diagnostics = ClockDiagnostics(
                "transport_time", samples=self._samples, accepted=self._accepted,
                rejected=self._rejected,
            )
            return False
        if not 0 <= age <= self.max_age:
            self._sample = None
            self._diagnostics = ClockDiagnostics(
                "mapping_age", mapping_age=_bounded(age), uncertainty=_bounded(self.uncertainty),
                samples=self._samples, accepted=self._accepted, rejected=self._rejected,
            )
            return False
        if abs(step) > self.max_step:
            self._sample = None
            self._diagnostics = ClockDiagnostics(
                "clock_step", mapping_age=_bounded(age), step=_bounded(step),
                uncertainty=_bounded(self.uncertainty), samples=self._samples,
                accepted=self._accepted, rejected=self._rejected,
            )
            return False
        self._diagnostics = ClockDiagnostics(
            "healthy", rtt=self._diagnostics.rtt,
            central_offset=self._diagnostics.central_offset,
            apply_age=self._diagnostics.apply_age, drift=self._diagnostics.drift,
            transport_drift=self._diagnostics.transport_drift,
            apply_drift=self._diagnostics.apply_drift,
            mapping_age=_bounded(age), step=_bounded(step),
            uncertainty=_bounded(self.uncertainty), samples=self._samples,
            accepted=self._accepted, rejected=self._rejected,
        )
        return True

    def deadline(self, utc_instant: float) -> float:
        if not self.healthy() or not math.isfinite(utc_instant):
            raise ValueError("unhealthy clock mapping")
        utc, mono = self._sample  # type: ignore[misc]
        return mono + utc_instant - utc
