from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from central.kernel.assets import AssetReady
from central.kernel.publishing import NOT_PUBLISHED, Failed, Ready, SettledHandle


def test_failed_invariants():
    with pytest.raises(ValueError):
        Failed(terminal=True, reason="boom", retry_after=timedelta(seconds=1))
    with pytest.raises(ValueError):
        Failed(terminal=False, reason="boom", retry_after=timedelta(seconds=-1))
    with pytest.raises(ValueError):
        Failed(terminal=False, reason="boom", retry_after=None)
    with pytest.raises(ValueError):
        Failed(terminal=True, reason="Not A Reason", retry_after=None)
    assert Failed(terminal=False, reason="busy", retry_after=timedelta(0)).retry_after == timedelta(0)
    assert NOT_PUBLISHED == Failed(True, "not_published", None)


@pytest.mark.parametrize("outcome", [
    Ready(AssetReady(size=1, sha256="a" * 64)),
    Ready(None),
    Failed(False, "origin_down", timedelta(seconds=3)),
    NOT_PUBLISHED,
])
@pytest.mark.parametrize("timeout", [timedelta(0), timedelta(days=1)])
def test_settled_handle_returns_its_outcome_immediately(outcome, timeout):
    async def wait():
        return await asyncio.wait_for(SettledHandle(outcome).wait(timeout=timeout), 1)

    assert asyncio.run(wait()) is outcome
