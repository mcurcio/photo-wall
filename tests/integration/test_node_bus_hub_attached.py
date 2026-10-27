"""Central's hub session tells its listener which hub server it is attached to, once connected and
again after every reconnect (nodeapi.hub `run_hub_session`, ListenedStates.hub_is), on the pinned
nats-server: a hub restart starts it under a new server id, which is how Central judges a door
close it missed while it was away (central/infra/home_assistant/states.py `hub_is`).

Mutation probe: `_hub_attached` telling only once -> the restarted hub's id never arrives."""
from __future__ import annotations

import asyncio

from integration.bus_servers import hub_server, until

from nodeapi.hub import run_hub_session

SECONDS = 30


class _State:
    async def values(self):
        return {}

    async def changed(self) -> None:
        await asyncio.Event().wait()


class _Listened:
    def __init__(self) -> None:
        self.servers: list[str] = []

    async def heard(self, epoch, items) -> None:
        return None

    async def closed(self, connection) -> None:
        return None

    async def hub_is(self, server: str) -> None:
        self.servers.append(server)


def test_the_hub_server_is_told_once_connected_and_after_every_reconnect(tmp_path):
    hub = hub_server(tmp_path, [])
    hub.start()
    listened = _Listened()

    async def run() -> None:
        stop = asyncio.Event()
        session = asyncio.create_task(run_hub_session(hub.client_url, version="0.1.0", state=_State(),
                                                      listened=listened, methods={}, stop=stop))
        try:
            async def told(count: int):
                return len(listened.servers) >= count and listened.servers[-1]
            first = await until(lambda: told(1), SECONDS, "the hub's server id once connected")
            await asyncio.to_thread(hub.stop)
            await asyncio.to_thread(hub.start)
            second = await until(lambda: told(2), SECONDS, "the restarted hub's server id")
            assert first.startswith("N") and second.startswith("N") and first != second
        finally:
            stop.set()
            await asyncio.wait_for(session, SECONDS)

    try:
        asyncio.run(run())
    finally:
        hub.stop()
