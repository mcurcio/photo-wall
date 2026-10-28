"""Central's scheduler tick: each step on its own, each with its own health.

One tick runs, in order: the Node reconciler ("reconcile"), the coordinator's three steps
("runtime": the Runtime moves to now; "planning": plan and offer; "settle": commits due, pin
expiry and cleanup, central/coordination.py `Coordinator.advance`), the measurement requests
of the plan ("measurement") and execution events' retention ("retention", bounded batches in
their own transactions outside the coordinator's locks,
central/infra/execution_event_retention.py). A step that fails never skips a later one: the Runtime keeps moving
when planning fails, and planning still runs when the reconciler fails. Measurement alone needs
an earlier step's answer (the plan): when planning fails it is "skipped" this tick, so no facet
claims a step ran that did not. Each step's health is its
own facet (`StepHealth`), logged when it changes, not every tick; /healthz reads them
(central/app.py)."""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from contracts.time import Clock

if TYPE_CHECKING:
    from central.coordination import Advance
    from central.planner import MeasureRequest

LOG = logging.getLogger("central.scheduler")

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
    steps: dict[str, StepHealth] = field(default_factory=dict)
    last_tick_monotonic: float | None = None


class Scheduler:
    """`reconciler.advance()`, `coordinator.advance() -> Advance`, `measurements(requests)` and
    `retention()` are the steps; `clock` stamps their facets and the completed tick."""

    def __init__(self, reconciler, coordinator,
                 measurements: Callable[[Sequence[MeasureRequest]], object], clock: Clock, *,
                 retention: Callable[[], object]) -> None:
        self.reconciler, self.coordinator = reconciler, coordinator
        self.measurements, self.clock, self.retention = measurements, clock, retention
        self._steps: dict[str, StepHealth] = {}
        self._last_tick_monotonic: float | None = None

    def tick(self) -> None:
        self._run("reconcile", self.reconciler.advance)
        advance = self._coordinate()
        if advance is not None and advance.projection is not None:
            projection = advance.projection
            self._run("measurement", lambda: self.measurements(projection.measurements))
        else:
            self._skipped("measurement", "planning failed")
        self._run("retention", self.retention)
        self._last_tick_monotonic = self.clock.monotonic()

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
                               last_tick_monotonic=self._last_tick_monotonic)

    def _run(self, step: str, call: Callable[[], object]) -> None:
        """Run one step; its facet is failing while it raises, ok once it runs through."""
        try:
            call()
        except Exception as exc:
            self._failed(step, scheduler_failure(exc))
        else:
            self._succeeded(step)

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
