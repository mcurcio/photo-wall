"""Progress-probe timing for one app run: pure rules, published constants.

The broker's probe thread sends a nonce every T and asks this clock, once per turn, what
the run's unanswered time amounts to. Only an answer to a nonce this clock sent counts.
Unanswered time runs from when the run was first seen (its launch, or broker start for a
run already running) or from the last answer; an interval whose turn ran more than T/2
late is the broker's own stall, not the app's, and is not counted. No miss is reported
before the startup budget S has passed, so "no channel since launch" is judged against S.

Stdlib only: the judge (B9) imports these constants and must not pull in the broker.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

PROBE_PERIOD_MS = 2000  # T
MISS_LIMIT = 5  # k
STARTUP_BUDGET_MS = 20000  # S
KILL_AFTER_MS = 35000  # K
OUTSTANDING_LIMIT = 8  # nonces remembered since the last answer


@dataclass(frozen=True, slots=True)
class ProbeTiming:
    period_ms: int = PROBE_PERIOD_MS
    miss_limit: int = MISS_LIMIT
    startup_ms: int = STARTUP_BUDGET_MS
    kill_after_ms: int = KILL_AFTER_MS

    def __post_init__(self) -> None:
        for value in (self.period_ms, self.miss_limit, self.startup_ms, self.kill_after_ms):
            if type(value) is not int or value < 1:
                raise ValueError("probe_timing")


SHIPPED_TIMING = ProbeTiming()


@dataclass(frozen=True, slots=True)
class AppRunKey:
    """One app run: its systemd invocation, kernel process birth and app epoch."""

    invocation_id: UUID
    pid: int
    start_ticks: int
    app_epoch: int

    @classmethod
    def of(cls, running) -> AppRunKey:
        """From a `RunningApp` (attribute access; this module imports no broker)."""
        process = running.process
        return cls(process.invocation_id, process.pid, process.start_ticks, running.app_epoch)

    def document(self) -> dict:
        return {"invocation_id": str(self.invocation_id), "pid": self.pid,
                "start_ticks": self.start_ticks, "app_epoch": self.app_epoch}


@dataclass(frozen=True, slots=True)
class ProbeFact:
    kind: str  # probe_unanswered | probe_kill_due
    value: dict


class ProbeClock:
    def __init__(self, run: AppRunKey, started_ms: int, *, timing: ProbeTiming = SHIPPED_TIMING):
        self.run, self.started_ms, self.timing = run, started_ms, timing
        self.last_rtt_ms: int | None = None
        self._mark = started_ms  # start of the interval the next turn judges
        self._counted_ms = 0  # unanswered time since the reference, late intervals excluded
        self._misses = 0
        self._answered = False  # an answer arrived since the previous turn
        self._kill_reported = False
        self._outstanding: dict[str, int] = {}  # nonce -> sent_ms, oldest first

    def sent(self, nonce: str, now_ms: int) -> None:
        self._outstanding[nonce] = now_ms
        while len(self._outstanding) > OUTSTANDING_LIMIT:
            del self._outstanding[next(iter(self._outstanding))]

    def answered(self, nonce: str, now_ms: int) -> bool:
        """True only for a nonce sent on this run and newer than the last answered one."""
        if nonce not in self._outstanding:
            return False
        for sent in list(self._outstanding):  # drop it and every older nonce
            sent_ms = self._outstanding.pop(sent)
            if sent == nonce:
                break
        self.last_rtt_ms = max(0, now_ms - sent_ms)
        self._mark, self._counted_ms, self._misses = now_ms, 0, 0
        self._answered, self._kill_reported = True, False
        return True

    def turn(self, now_ms: int, *, late: bool) -> tuple[ProbeFact, ...]:
        """Judge the interval since the previous turn (or answer); `late`: not counted."""
        interval = max(0, now_ms - self._mark)
        self._mark = now_ms
        answered, self._answered = self._answered, False
        if not late:
            self._counted_ms += interval
            if not answered:
                self._misses += 1
        if now_ms < self.started_ms + self.timing.startup_ms:
            return ()
        run = self.run.document()
        facts = []
        if self._misses >= self.timing.miss_limit:
            facts.append(ProbeFact("probe_unanswered", {
                "run": run, "unanswered_ms": self._counted_ms, "misses": self._misses}))
        if self._counted_ms >= self.timing.kill_after_ms and not self._kill_reported:
            self._kill_reported = True
            facts.append(ProbeFact("probe_kill_due", {"run": run, "unanswered_ms": self._counted_ms}))
        return tuple(facts)


__all__ = [
    "KILL_AFTER_MS", "MISS_LIMIT", "PROBE_PERIOD_MS", "SHIPPED_TIMING", "STARTUP_BUDGET_MS",
    "AppRunKey", "ProbeClock", "ProbeFact", "ProbeTiming",
]
