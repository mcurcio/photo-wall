from __future__ import annotations

from datetime import timedelta

import pytest

from central.kernel.assets import AssetReady
from central.kernel.handling import (
    OriginRejected,
    OriginUnavailable,
    TerminalFailure,
    TransientFailure,
    handler_job_type,
)
from central.kernel.job_types import FetchOsImage, SyncReleases
from central.kernel.jobs import Job

# Handler modules import job types at runtime (never under TYPE_CHECKING): handler_job_type
# resolves the `handle` hints with typing.get_type_hints.


Unregistered = Job[None]  # a parametrized intermediate: a Job subclass that is never registered


class FetchOsImageHandler:
    async def handle(self, job: FetchOsImage) -> AssetReady:
        raise NotImplementedError


class SyncHandler:
    async def handle(self, job: SyncReleases) -> None:
        return None


class WrongReturn:
    async def handle(self, job: FetchOsImage) -> None:
        return None


class MissingReturn:
    async def handle(self, job: SyncReleases):
        return None


class MissingJobHint:
    async def handle(self, job) -> None:
        return None


class NotAJob:
    async def handle(self, job: int) -> None:
        return None


def test_handler_job_type_returns_the_job_type():
    assert handler_job_type(FetchOsImageHandler()) is FetchOsImage
    assert handler_job_type(SyncHandler()) is SyncReleases


@pytest.mark.parametrize("handler", [WrongReturn(), MissingReturn(), MissingJobHint(), NotAJob(),
                                     object()])
def test_handler_job_type_refuses_bad_hints(handler):
    with pytest.raises(TypeError):
        handler_job_type(handler)


def test_handler_job_type_refuses_unregistered_job_type():
    class Intermediate:
        async def handle(self, job: Unregistered) -> None:
            return None

    class BareJob:
        async def handle(self, job: Job) -> None:  # type: ignore[type-arg]
            return None

    for handler in (Intermediate(), BareJob()):
        with pytest.raises(TypeError, match="not a registered job type"):
            handler_job_type(handler)


def test_failures_validate_reason_and_retry_after():
    failure = OriginUnavailable("rate_limited", retry_after=timedelta(seconds=30))
    assert isinstance(failure, TransientFailure)
    assert (failure.reason, failure.retry_after) == ("rate_limited", timedelta(seconds=30))
    assert TransientFailure("x").retry_after is None
    rejected = OriginRejected("not_found")
    assert isinstance(rejected, TerminalFailure) and rejected.reason == "not_found"
    with pytest.raises(ValueError):
        TransientFailure("x", retry_after=timedelta(seconds=-1))
    with pytest.raises(ValueError):
        TerminalFailure("Bad Reason")
