"""Ending a cycle's tasks: cancelled until each has ended, never cancelled once and trusted.

One `Task.cancel()` is not always delivered. httpx reaches the network through httpcore and anyio,
and anyio's cancel scopes cancel the task they run in with the same native `CancelledError`; when
a caller's cancel lands while such a scope is cancelling itself (anyio.connect_tcp's happy eyeballs
cancels its task group as soon as one address connects), asyncio raises one `CancelledError` for
both, and the scope's exit takes it as its own and swallows it. The task then runs on with
`cancelling() > 0` and nothing left to stop it, so a caller that cancels once and awaits it waits
for good (the cycle that never ended, `PlayerService.run`). Asked again after a grace, it ends at
its next await; the grace leaves a cleanup that awaits on its way out (a pool closing a
connection) to finish uncut.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

CANCEL_AGAIN = 1.0
"""Seconds a cancelled task has to end before it is cancelled again."""


async def cancel_and_wait(tasks: Iterable[asyncio.Future]) -> None:
    """Cancel every task and return once each has ended, its outcome retrieved. A task still
    running CANCEL_AGAIN after its cancel is cancelled again, until it ends. A cancel of the
    caller meanwhile is held until then, and raised after: the tasks never outlive this call."""
    tasks = list(tasks)
    pending = {task for task in tasks if not task.done()}
    interrupted = None
    while pending:
        for task in pending:
            task.cancel()
        try:
            _, pending = await asyncio.wait(pending, timeout=CANCEL_AGAIN)
        except asyncio.CancelledError as error:
            interrupted = error
            pending = {task for task in pending if not task.done()}
    for task in tasks:
        if not task.cancelled():
            task.exception()
    if interrupted is not None:
        raise interrupted
