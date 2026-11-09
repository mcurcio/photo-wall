"""Clock interfaces: wall and monotonic clocks, real and manual; no pipeline time coupling."""

import math
import time
from dataclasses import dataclass
from typing import Protocol


class Clock(Protocol):
    def utc(self) -> float: ...
    def monotonic(self) -> float: ...


class Monotonic(Protocol):
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
