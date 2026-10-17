"""The one supervised LISTEN (central/infra/listen.py) on the test database: each notification is
handed over, and a connection the database ends is dialled again and says so (`connected`)."""
from __future__ import annotations

import asyncio
import uuid

from central.infra.listen import listen


def test_a_lost_listen_connection_reconnects_and_says_so(registry):
    """A9. Mutation probe: `connected()` only on the first connect -> the reconnect is never seen."""
    application = f"photo-wall-test-listen-{uuid.uuid4().hex[:8]}"
    channel = f"test_listen_{uuid.uuid4().hex[:8]}"

    def sql(statement: str, params=()) -> None:
        with registry.db.transaction() as conn:
            conn.execute(statement, params)

    async def run() -> tuple[int, list[str]]:
        connects: asyncio.Queue[int] = asyncio.Queue()
        notes: asyncio.Queue[str] = asyncio.Queue()
        count = 0

        def connected() -> None:
            nonlocal count
            count += 1
            connects.put_nowait(count)

        task = asyncio.create_task(listen(registry.db.dsn, channel, application=application, connected=connected,
                                          notified=notes.put_nowait, retry_seconds=0.01))
        try:
            assert await asyncio.wait_for(connects.get(), 10) == 1
            await asyncio.to_thread(sql, "SELECT pg_notify(%s, 'one')", (channel,))
            heard = [await asyncio.wait_for(notes.get(), 10)]
            await asyncio.to_thread(sql, "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                                         "WHERE datname = current_database() AND application_name=%s", (application,))
            second = await asyncio.wait_for(connects.get(), 10)
            await asyncio.to_thread(sql, "SELECT pg_notify(%s, 'two')", (channel,))
            heard.append(await asyncio.wait_for(notes.get(), 10))
            return second, heard
        finally:
            task.cancel()

    assert asyncio.run(run()) == (2, ["one", "two"])
