"""One supervised PostgreSQL LISTEN: the one home of listening on a channel, reconnecting after a
lost connection, and saying so, for every process that wakes on another's NOTIFY."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import psycopg
from psycopg import sql

log = logging.getLogger(__name__)


async def listen(dsn: str, channel: str, *, application: str, connected: Callable[[], None],
                 notified: Callable[[str], None], retry_seconds: float) -> None:
    """Until cancelled: LISTEN `channel` on an autocommit connection of its own (named
    `application`); `connected()` after every (re)connect, since notifications sent while none
    listened are lost; `notified(payload)` for each notification. A lost connection is logged and
    dialled again after `retry_seconds`."""
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(dsn, autocommit=True, application_name=application) as conn:
                await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))
                connected()
                async for notify in conn.notifies():
                    notified(notify.payload)
        except Exception as error:   # the caller's own recheck keeps it correct meanwhile
            log.warning("%s LISTEN connection lost: %s", channel, error)
        await asyncio.sleep(retry_seconds)


__all__ = ["listen"]
