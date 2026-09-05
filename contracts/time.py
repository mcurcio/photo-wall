"""Clock interfaces and safe UTC-to-monotonic scheduling; no pipeline time coupling."""

import math
import time
from dataclasses import dataclass
from typing import Protocol


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

    def establish(self, uncertainty: float) -> None:
        if uncertainty < 0 or not math.isfinite(uncertainty):
            raise ValueError("invalid uncertainty")
        sample = (self.clock.utc(), self.clock.monotonic())
        if not all(math.isfinite(v) for v in sample):
            self._sample = None
            raise ValueError("invalid clock sample")
        self._sample = sample
        self.uncertainty = uncertainty

    def healthy(self) -> bool:
        if self._sample is None or self.uncertainty > self.max_uncertainty:
            return False
        utc, mono = self._sample
        now_utc, now_mono = self.clock.utc(), self.clock.monotonic()
        if (not all(math.isfinite(v) for v in (now_utc, now_mono))
                or not 0 <= now_mono - mono <= self.max_age
                or abs(now_utc - (utc + now_mono - mono)) > self.max_step):
            self._sample = None
            return False
        return True

    def deadline(self, utc_instant: float) -> float:
        if not self.healthy() or not math.isfinite(utc_instant):
            raise ValueError("unhealthy clock mapping")
        utc, mono = self._sample  # type: ignore[misc]
        return mono + utc_instant - utc
