"""Publishing a job and awaiting its outcome.

Publisher contract (PB1-PB9; `RecordingPublisher` and the procrastinate adapter share one
conformance suite):
- PB1 An unregistered job type raises `TypeError`; `within.state != "open"` raises
  `RuntimeError("transaction_not_open")`.
- PB2 A latest `transient` outcome with `retry_not_before > now`: insert nothing, return
  `SettledHandle(Failed(False, reason, retry_not_before - now))`.
- PB3 A latest `terminal` outcome without `retry_terminal`: insert nothing, return
  `SettledHandle(Failed(True, reason, None))`.
- PB4 Otherwise read `since` (the key's current outcome sequence, 0 if none), then defer. A merge
  into a pending copy is success. With a copy running, a pending copy is inserted: an asset job's
  waits for the running one's lock; any other may run alongside it. Either way the handle
  resolves on the first outcome newer than `since` (PB7), the running copy's included.
- PB5 `publish` runs in a SAVEPOINT of `within`, so a merge never aborts the caller's work.
- PB6 `wait` while `within` is open raises `RuntimeError("await_after_commit")`; after a rollback
  it returns `NOT_PUBLISHED`.
- PB7 `wait` resolves on the first outcome with a sequence > `since`: `ok` -> `Ready`;
  `transient` -> `Failed(False, reason, max(0, retry_not_before - now))`; `terminal` ->
  `Failed(True, reason, None)`; timeout -> `Pending`; cancellation -> unregister and re-raise.
  Neither a timeout nor a cancellation cancels the job. An asset job's `ok` whose Asset record
  is absent (or has no produced facts) is `Failed(False, ASSET_NOT_RECORDED, 0)`: there is
  nothing to serve, and a later reference can re-create the record.
- PB8 `publish_now` uses its own transaction and commits it before returning.
- PB9 Periodic job types may be published on demand; they merge into the pending tick.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Final, Generic, Protocol, TypeVar

from central.kernel.jobs import Job, R
from central.kernel.transactions import Transaction
from central.kernel.types import require_reason

R_co = TypeVar("R_co", covariant=True)


@dataclass(frozen=True, slots=True)
class Ready(Generic[R]):
    result: R  # asset job: AssetReady read from the Asset record; else None


@dataclass(frozen=True, slots=True)
class Failed:
    terminal: bool
    reason: str  # require_reason
    retry_after: timedelta | None  # terminal => None; else a timedelta >= 0

    def __post_init__(self) -> None:
        if type(self.terminal) is not bool:
            raise ValueError("invalid_terminal")
        require_reason(self.reason)
        if self.terminal:
            if self.retry_after is not None:
                raise ValueError("invalid_retry_after")
        elif not isinstance(self.retry_after, timedelta) or self.retry_after < timedelta(0):
            raise ValueError("invalid_retry_after")


@dataclass(frozen=True, slots=True)
class Pending:
    pass


NOT_PUBLISHED: Final[Failed] = Failed(terminal=True, reason="not_published", retry_after=None)
# PB7, and an asset handler that finds no Asset record: one condition, one transient reason.
ASSET_NOT_RECORDED: Final = "asset_not_recorded"


class JobHandle(Protocol[R_co]):
    async def wait(self, *, timeout: timedelta) -> Ready[R_co] | Failed | Pending: ...


class SettledHandle(Generic[R]):
    """A handle whose outcome was decided at publish time; shared by every Publisher."""

    __slots__ = ("_outcome",)

    def __init__(self, outcome: Ready[R] | Failed) -> None:
        self._outcome = outcome

    async def wait(self, *, timeout: timedelta) -> Ready[R] | Failed | Pending:
        return self._outcome


class Publisher(Protocol):
    def publish(self, job: Job[R], *, within: Transaction,
                retry_terminal: bool = False) -> JobHandle[R]: ...

    async def publish_now(self, job: Job[R], *, retry_terminal: bool = False) -> JobHandle[R]: ...
