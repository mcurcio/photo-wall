"""The room-lights key's one writer (central/node_bus_wiring.py `RoomLightsWall`): a put that fails
while WALL stays (a blip on the worker's client, a put that timed out) is put again by the writer's
own recheck, with no wake from anyone: nothing else would wake it, since WALL was not re-created.

Mutation: the recheck's timeout dropped from `run` -> the document never arrives.
"""
from __future__ import annotations

import asyncio

from central import node_bus_wiring
from central.node_bus_wiring import RoomLightsWall
from contracts.room_light import ROOM_LIGHTS_KEY, RoomLightsDocument, encode_room_lights

DOCUMENT = RoomLightsDocument(change=1, lights={})


class _Documents:
    def current(self) -> RoomLightsDocument:
        return DOCUMENT


class _FailsOnce:
    """A WallWriter whose first put fails, as a client blip does; WALL itself stays."""

    def __init__(self) -> None:
        self.tries = 0
        self.held: dict[str, bytes] = {}
        self.put_once = asyncio.Event()

    async def put(self, key: str, value: bytes) -> None:
        self.tries += 1
        if self.tries == 1:
            raise TimeoutError("put timed out")
        self.held[key] = value
        self.put_once.set()


class _Wall:
    def __init__(self, writer: _FailsOnce) -> None:
        self.wall = writer


def test_a_put_that_fails_while_wall_stays_is_put_again_by_the_recheck(monkeypatch):
    monkeypatch.setattr(node_bus_wiring, "WALL_RECHECK_SECONDS", 0.05)

    async def run() -> None:
        writer, stop = _FailsOnce(), asyncio.Event()
        running = asyncio.create_task(RoomLightsWall(_Documents()).run(stop, _Wall(writer)))
        try:
            await asyncio.wait_for(writer.put_once.wait(), 2)
        finally:
            stop.set()
            await asyncio.wait_for(running, 2)
        assert writer.tries == 2
        assert writer.held == {ROOM_LIGHTS_KEY: encode_room_lights(DOCUMENT)}

    asyncio.run(run())
