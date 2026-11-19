"""Central's scheduler: each step on its own, each with its own health, run when something is due.

One tick runs, in order: the coordinator's three steps ("runtime": the Runtime moves to now;
"planning": plan and offer; "settle": commits due, pin expiry and cleanup,
central/coordination.py `Coordinator.advance`) and the measurement requests of the plan
("measurement"). A step that fails never skips a later one: the Runtime keeps moving when
planning fails. Measurement alone needs an earlier step's answer (the plan): when planning fails
it is "skipped" this tick, so no facet claims a step ran that did not.

The tick answers when to tick next (decision 0034, class F: timers where events exist): the
coordinator's `Advance.wake_at` (the Runtime's next event, the planner's next renewal boundary, the
next due cue), never later than SCHEDULER_IDLE_SECONDS, which bounds a missed wake; a tick with a
failing step tries again after SCHEDULER_MIN_SECONDS. Between ticks the loop (`run_scheduler`)
rests until then or until a wake (a commit's NOTIFY, central/infra/player_state_wakes.py), whichever
comes first, and never ticks twice within SCHEDULER_MIN_SECONDS.

Retention is never inside the tick: `retain` runs each retention in the order wired, one step per
key ("node-retention": node records and events, central/infra/node_record_retention.py), every
RETENTION_EVERY from the loop's start, each in whole bounded batches in their own transactions
outside the coordinator's locks (central/infra/batched_retention.py).

Each step's health is its own facet (`StepHealth`), logged when it changes, not every tick; /healthz
reads them with when the loop means to tick next (central/app.py)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import traceback
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from contracts.time import Clock

if TYPE_CHECKING:
    from central.coordination import Advance
    from central.planner import MeasureRequest

LOG = logging.getLogger("central.scheduler")

SCHEDULER_IDLE_SECONDS: Final = 60.0   # the longest rest between ticks: bounds a missed wake
SCHEDULER_MIN_SECONDS: Final = 1.0     # the shortest: a failing step's retry, and a burst of wakes
RETENTION_EVERY: Final = 300.0         # how often the retention steps run, off the tick

_OWN_PACKAGES = ("central", "media", "contracts", "nodeapi")


def scheduler_failure(exc: BaseException) -> str:
    """A scheduler step's failure as its exception type and the innermost line of this project's
    code it passed (else the innermost line at all), e.g. `ValidationError at
    central/coordination.py:231`. Never the message: a driver's may carry a connection string."""
    frames = traceback.extract_tb(exc.__traceback__)
    own = [frame for frame in frames if "site-packages" not in frame.filename
           and any(f"/{package}/" in frame.filename for package in _OWN_PACKAGES)]
    if not frames:
        return type(exc).__name__
    where = (own or frames)[-1]
    parts = Path(where.filename).parts
    start = max((i for i, part in enumerate(parts) if part in _OWN_PACKAGES), default=len(parts) - 1)
    return f"{type(exc).__name__} at {'/'.join(parts[start:])}:{where.lineno}"


@dataclass(frozen=True)
class StepHealth:
    """One step's facet: "failing" since its first failure in a row (`since`, Central's clock),
    with the sanitized reason of its latest one; "skipped" since the first tick in a row it could
    not run (`reason`: which step it waits on); `last_success` is when it last ran through."""

    status: Literal["ok", "failing", "skipped"]
    since: float | None
    last_success: float | None
    reason: str | None


@dataclass(frozen=True)
class SchedulerHealth:
    """Each step's facet, when the last tick completed and when the loop means to tick next (both
    on the clock's monotonic time; `due_monotonic` None until the loop rests once)."""

    steps: dict[str, StepHealth] = field(default_factory=dict)
    last_tick_monotonic: float | None = None
    due_monotonic: float | None = None


class Scheduler:
    """`coordinator.advance() -> Advance`, `measurements(requests)` and
    each of `retentions`' calls are the steps; `clock` stamps their facets and the completed
    tick. Each retention is its own step and `/healthz` facet, named by its key, so a failing
    trim never hides behind another (decision 0033)."""

    def __init__(self, coordinator, measurements: Callable[[Sequence[MeasureRequest]], object],
                 clock: Clock, *, retentions: Mapping[str, Callable[[], object]]) -> None:
        self.coordinator = coordinator
        self.measurements, self.clock = measurements, clock
        self.retentions = dict(retentions)
        self._steps: dict[str, StepHealth] = {}
        self._last_tick_monotonic: float | None = None
        self._due_monotonic: float | None = None

    def tick(self) -> float:
        """Run the steps (retention excepted) and answer the next instant to tick at, on the
        clock's UTC: the coordinator's `wake_at`, at most SCHEDULER_IDLE_SECONDS on, and
        SCHEDULER_MIN_SECONDS on when a step failed."""
        failing = False

        def run(step: str, call: Callable[[], object]) -> None:
            nonlocal failing
            failing = not self._run(step, call) or failing

        advance = self._coordinate()
        if advance is not None and advance.projection is not None:
            projection = advance.projection
            run("measurement", lambda: self.measurements(projection.measurements))
        else:
            self._skipped("measurement", "planning failed")
        self._last_tick_monotonic = self.clock.monotonic()
        now = self.clock.utc()
        if failing or advance is None or advance.failures:
            return now + SCHEDULER_MIN_SECONDS
        wake_at = advance.wake_at if advance.wake_at is not None else math.inf
        return min(wake_at, now + SCHEDULER_IDLE_SECONDS)

    def retain(self) -> None:
        """The retention steps, each its own facet; called every RETENTION_EVERY by the loop."""
        for step, retention in self.retentions.items():
            self._run(step, retention)

    def resting_until(self, due_monotonic: float) -> None:
        """The loop rests until `due_monotonic` (a wake may end it sooner): /healthz reads the
        loop as stale only once that has passed by more than its allowance."""
        self._due_monotonic = due_monotonic

    def _coordinate(self) -> Advance | None:
        """The coordinator's tick, each of its steps its own facet; one that raises past its
        own step handling (it should not) fails all three."""
        steps = ("runtime", "planning", "settle")
        try:
            advance = self.coordinator.advance()
        except Exception as exc:
            reason = scheduler_failure(exc)
            for step in steps:
                self._failed(step, reason)
            return None
        failed = {failure.step: failure.reason for failure in advance.failures}
        for step in steps:
            if step in failed:
                self._failed(step, failed[step])
            else:
                self._succeeded(step)
        return advance

    def health(self) -> SchedulerHealth:
        return SchedulerHealth(steps=dict(self._steps),
                               last_tick_monotonic=self._last_tick_monotonic,
                               due_monotonic=self._due_monotonic)

    def _run(self, step: str, call: Callable[[], object]) -> bool:
        """Run one step; its facet is failing while it raises, ok once it runs through. True when
        it ran through."""
        try:
            call()
        except Exception as exc:
            self._failed(step, scheduler_failure(exc))
            return False
        self._succeeded(step)
        return True

    def _failed(self, step: str, reason: str) -> None:
        previous = self._steps.get(step)
        failing = previous is not None and previous.status == "failing"
        if not failing or previous.reason != reason:
            LOG.error("scheduler step %s failing: %s", step, reason)
        self._steps[step] = StepHealth(
            status="failing", since=previous.since if failing else self.clock.utc(),
            last_success=previous.last_success if previous else None, reason=reason)

    def _skipped(self, step: str, reason: str) -> None:
        previous = self._steps.get(step)
        skipped = previous is not None and previous.status == "skipped"
        self._steps[step] = StepHealth(
            status="skipped", since=previous.since if skipped else self.clock.utc(),
            last_success=previous.last_success if previous else None, reason=reason)

    def _succeeded(self, step: str) -> None:
        previous = self._steps.get(step)
        if previous is not None and previous.status == "failing":
            LOG.warning("scheduler step %s recovered", step)
        self._steps[step] = StepHealth(status="ok", since=None, last_success=self.clock.utc(),
                                       reason=None)


class Wakes(Protocol):
    """A count of wakes (central/infra/player_state_wakes.py `PlayerStateWakes`)."""

    def count(self) -> int: ...

    def after(self, seen: int) -> Coroutine[Any, Any, int]: ...


async def rest(woken: Coroutine[Any, Any, object], seconds: float) -> None:
    """Until `woken` completes or `seconds` pass, whichever is first."""
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(woken, max(0.0, seconds))


async def _never() -> None:
    await asyncio.Event().wait()


async def run_scheduler(scheduler: Scheduler, wakes: Wakes, *,
                        ticked: Callable[[], None] = lambda: None,
                        resting: Callable[[Coroutine[Any, Any, object], float], Awaitable[None]] = rest,
                        ) -> None:
    """Until cancelled: tick (in a thread), then the retention steps when RETENTION_EVERY has passed
    since they last ran (at once on the first pass), then rest until the tick's answer or the next
    retention, or until a wake heard since the tick began; never two ticks within
    SCHEDULER_MIN_SECONDS. `ticked` runs after each tick; `resting` is the rest itself (a test's
    stands in for time)."""
    clock = scheduler.clock
    retain_at = clock.monotonic()
    while True:
        seen = wakes.count()
        started = clock.monotonic()
        wake_at = await asyncio.to_thread(scheduler.tick)
        ticked()
        if clock.monotonic() >= retain_at:
            await asyncio.to_thread(scheduler.retain)
            retain_at = clock.monotonic() + RETENTION_EVERY
        seconds = max(0.0, min(wake_at - clock.utc(), retain_at - clock.monotonic()))
        scheduler.resting_until(clock.monotonic() + max(seconds, SCHEDULER_MIN_SECONDS))
        await resting(wakes.after(seen), seconds)
        left = started + SCHEDULER_MIN_SECONDS - clock.monotonic()
        if left > 0:
            await resting(_never(), left)
