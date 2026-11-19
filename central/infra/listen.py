"""One supervised PostgreSQL LISTEN: the one home of listening on a channel, reconnecting after a
lost connection, and saying so, for every process that wakes on another's NOTIFY."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import psycopg
from psycopg import sql

from central.db import PoolName, with_application_name

log = logging.getLogger(__name__)


async def listen(dsn: str, channel: str | tuple[str, ...], *, application: PoolName,
                 connected: Callable[[], None], notified: Callable[[str], None],
                 retry_seconds: float) -> None:
    """Until cancelled: LISTEN `channel` (or each of several) on an autocommit connection of its own
    (named `application`); `connected()` after every (re)connect, since notifications sent while
    none listened are lost; `notified(payload)` for each notification. A lost connection is logged
    and dialled again after `retry_seconds`."""
    channels = (channel,) if isinstance(channel, str) else channel
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(with_application_name(dsn, application), autocommit=True) as conn:
                for each in channels:
                    await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(each)))
                connected()
                async for notify in conn.notifies():
                    notified(notify.payload)
        except Exception as error:   # the caller's own recheck keeps it correct meanwhile
            log.warning("%s LISTEN connection lost: %s", ",".join(channels), error)
        await asyncio.sleep(retry_seconds)


__all__ = ["listen"]
