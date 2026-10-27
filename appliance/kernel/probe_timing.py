"""Progress-probe timing: the published constants T, k and S and the timing they make. Nothing
kills an unanswered Player (issue #125), so there is no K.

This module is stdlib only (the kernel's rule allows `contracts` and `uplink` too): the broker's
probe clock (`appliance.apps.probe`) and the judge both import it.
"""
from __future__ import annotations

from dataclasses import dataclass

PROBE_PERIOD_MS = 2000  # T
MISS_LIMIT = 5  # k
STARTUP_BUDGET_MS = 20000  # S
OUTSTANDING_LIMIT = 8  # nonces remembered since the last answer


@dataclass(frozen=True, slots=True)
class ProbeTiming:
    period_ms: int = PROBE_PERIOD_MS
    miss_limit: int = MISS_LIMIT
    startup_ms: int = STARTUP_BUDGET_MS

    def __post_init__(self) -> None:
        for value in (self.period_ms, self.miss_limit, self.startup_ms):
            if type(value) is not int or value < 1:
                raise ValueError("probe_timing")


SHIPPED_TIMING = ProbeTiming()
