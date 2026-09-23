"""Handlers and the failures they raise.

A handler's job and result types come from its `handle` annotations, read at runtime with
`typing.get_type_hints`. Handler modules must therefore import job types at runtime, never under
`TYPE_CHECKING`.
"""

from __future__ import annotations

import typing
from datetime import timedelta
from typing import Any, Protocol, TypeVar

from central.kernel.jobs import Job, registered_job_type
from central.kernel.publishing import R_co
from central.kernel.types import require_reason

J_contra = TypeVar("J_contra", bound=Job[Any], contravariant=True)


class Handler(Protocol[J_contra, R_co]):
    async def handle(self, job: J_contra) -> R_co: ...


class TransientFailure(Exception):
    """Retry later; `retry_after` (>= 0) raises the next delay above the delivery's backoff."""

    def __init__(self, reason: str, *, retry_after: timedelta | None = None) -> None:
        require_reason(reason)
        if retry_after is not None and (
            not isinstance(retry_after, timedelta) or retry_after < timedelta(0)
        ):
            raise ValueError("invalid_retry_after")
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


class TerminalFailure(Exception):
    """Never retried unless a publisher sets `retry_terminal`."""

    def __init__(self, reason: str) -> None:
        require_reason(reason)
        super().__init__(reason)
        self.reason = reason


class OriginUnavailable(TransientFailure):
    """Network, 5xx, 403/429 rate limit (+retry_after), corrupt or truncated stream."""


class OriginRejected(TerminalFailure):
    """404/410, oversize, schema or manifest errors."""


class ProducedFactsConflict(Exception):
    """`record_produced` was given facts that differ from the recorded facts."""


def handler_job_type(handler: object) -> type[Job[Any]]:
    """The registered job type a handler handles; `TypeError` unless its hints are exact."""
    handle = getattr(type(handler), "handle", None)
    if handle is None:
        raise TypeError(f"{type(handler).__name__} has no handle method")
    try:
        hints = typing.get_type_hints(handle)
    except Exception as error:  # an unresolvable hint (e.g. a TYPE_CHECKING-only import)
        raise TypeError(f"{type(handler).__name__}.handle hints do not resolve: {error}") from None
    job_type = hints.get("job")
    if not isinstance(job_type, type) or not issubclass(job_type, Job):
        raise TypeError(f"{type(handler).__name__}.handle(job) is not annotated with a job type")
    name = getattr(job_type, "job_name", None)
    try:
        registered = registered_job_type(name) if isinstance(name, str) else None
    except KeyError:
        registered = None
    if registered is not job_type:
        raise TypeError(f"{job_type.__name__} is not a registered job type")
    expected = type(None) if job_type.result_type is None else job_type.result_type
    if "return" not in hints or hints["return"] is not expected:
        raise TypeError(
            f"{type(handler).__name__}.handle must return {expected!r} for {job_type.__name__}"
        )
    return job_type
