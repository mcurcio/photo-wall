"""Progress-probe timing for one app run: pure rules, published constants.

The broker's probe thread sends a nonce every T and asks this clock, once per turn, what
the run's unanswered time amounts to. Only an answer to a nonce this clock sent counts.
Unanswered time runs from when the run was first seen (its launch, or broker start for a
run already running) or from the last answer; an interval whose turn ran more than T/2
late is the broker's own stall, not the app's, and is not counted. No miss is reported
before the startup budget S has passed, so "no channel since launch" is judged against S.

The kill rule's guard is here too: `recovery_may_be_armed` says whether a host recovery
obligation may still be armed, in which case the broker never kills (Q1: a switch recovery
owns the app until the host acknowledges control). It judges the last obligation the host
accepted (`recovery-armed`, written by the online broker after each successful arm), not the
online record alone: a newer stage replaces that record before the older obligation's control
is acknowledged, and a record that is not settled (`running`/`fallback_running`) is mid-switch.

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
# Boot-store keys the online broker writes: the last obligation the host armed, and the last
# one whose control the host acknowledged. Each holds {"operation_id": str}.
RECOVERY_ARMED = "recovery-armed"
RECOVERY_ACKNOWLEDGED = "recovery-acknowledged"
_SETTLED = ("running", "fallback_running")


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
class KillDue:
    """The probe thread's level latch: `run` has been unanswered for at least K."""

    run: AppRunKey
    unanswered_ms: int


@dataclass(frozen=True, slots=True)
class OwedRelink:
    """The outbox owes `run`'s Player a relink; `episode` names the refusal that owed it, so
    each new refusal of the same run is a new relink (once per channel instance per episode)."""

    run: AppRunKey
    episode: str


def recovery_may_be_armed(record: dict | None, armed: dict | None,
                          acknowledged: dict | None) -> bool:
    """False only when every obligation that may be armed is the one `acknowledged` names.

    The obligations are the last one armed (`armed`, the `recovery-armed` key) and the online
    `record`'s own. A record in any phase but `running`/`fallback_running` (`preparing`, or a
    switch in flight) counts as armed. Fail-closed: anything unreadable counts as armed."""
    if record is not None:
        if not isinstance(record, dict) or record.get("phase") not in _SETTLED:
            return True
    obligations = []
    if armed is not None:
        obligations.append(armed)
    if isinstance(record, dict) and "recovery" in record:
        obligations.append(record["recovery"])
    if not obligations:
        return False  # nothing was ever armed on this boot (cold start)
    acknowledged_id = acknowledged.get("operation_id") if isinstance(acknowledged, dict) else None
    for obligation in obligations:
        operation_id = obligation.get("operation_id") if isinstance(obligation, dict) else None
        if not isinstance(operation_id, str) or not operation_id or operation_id != acknowledged_id:
            return True
    return False


@dataclass(frozen=True, slots=True)
class ProbeFact:
    kind: str  # probe_unanswered | probe_kill_due
    value: dict


class ProbeClock:
    def __init__(self, run: AppRunKey, started_ms: int, *, timing: ProbeTiming = SHIPPED_TIMING):
        self.run, self.started_ms, self.timing = run, started_ms, timing
        self.last_rtt_ms: int | None = None
        self._judged = False  # a turn past the startup budget has run
        self._mark = started_ms  # start of the interval the next turn judges
        self._counted_ms = 0  # unanswered time since the reference, late intervals excluded
        self._misses = 0
        self._answered = False  # an answer arrived since the previous turn
        self._kill_reported = False
        self._outstanding: dict[str, int] = {}  # nonce -> sent_ms, oldest first

    @property
    def unanswered_ms(self) -> int:
        """Counted unanswered time (late intervals excluded) since the reference."""
        return self._counted_ms

    @property
    def overdue(self) -> bool:
        """Level: counted unanswered time has reached K (judged past the startup budget).

        `probe_kill_due` is reported once per episode; this stays true until an answer."""
        return self._judged and self._counted_ms >= self.timing.kill_after_ms

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
        self._judged = True
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
    "KILL_AFTER_MS", "MISS_LIMIT", "PROBE_PERIOD_MS", "RECOVERY_ACKNOWLEDGED", "RECOVERY_ARMED",
    "SHIPPED_TIMING", "STARTUP_BUDGET_MS", "AppRunKey", "KillDue", "OwedRelink", "ProbeClock", "ProbeFact",
    "ProbeTiming", "recovery_may_be_armed",
]
