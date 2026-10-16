"""The Home Assistant link survives a database failure while recording (central/infra/home_assistant/
link.py): it disconnects, says "connecting" again, reconnects, is handed the retained state again and
records it, and its task never ends (the worker's TaskGroup would end every loop with it).

The link runs against the pinned nats-server's MQTT listener (the house's broker, room_light_rig.py)
and the real room-light store, whose first word fails in the database.

Mutation: `except Exception` in `_connecting` narrowed (to `ZeroDivisionError`) -> the link's task
ends with the database's error.
"""
from __future__ import annotations

import asyncio

from integration.bus_servers import until
from integration.room_light_rig import Broker

from central.infra import room_light_store
from central.infra.home_assistant.link import HomeAssistantLink, broker_from
from central.infra.home_assistant.topics import LIGHT_TOPIC_PREFIX
from central.infra.room_light_store import PgRoomLights

ENTITY = "light.bookshelf_spot"


class _Beats:
    def __init__(self) -> None:
        self.said: list[str] = []

    def beat(self, beat: str) -> None:
        self.said.append(beat)


def test_a_recording_that_fails_reconnects_and_the_retained_state_is_recorded(registry, tmp_path, monkeypatch):
    tried: list[str] = []
    word_in = room_light_store._word_in

    def fails_once(conn, table, *rest):
        tried.append(table)
        if len(tried) == 1:
            conn.execute("SELECT 1/0")
        return word_in(conn, table, *rest)

    monkeypatch.setattr(room_light_store, "_word_in", fails_once)
    broker, beats = Broker(tmp_path), _Beats()
    broker.start()

    def kept() -> list[dict]:
        with registry.db.transaction() as conn:
            return conn.execute("SELECT address, word FROM light_words").fetchall()

    async def run() -> None:
        await broker.publish(LIGHT_TOPIC_PREFIX + ENTITY, b'{"state":"on","brightness":255}')
        stop = asyncio.Event()
        link = HomeAssistantLink(broker_from(broker.url), PgRoomLights(registry.db), beats,  # type: ignore[arg-type]
                                 close=lambda: None)
        running = asyncio.create_task(link.run(stop))
        try:
            async def recorded():
                return await asyncio.to_thread(kept)
            [word] = await until(recorded, 10, "the retained state is recorded after the failure")
            assert word["address"] == ENTITY and len(tried) == 2
            assert not running.done()
            said = [beat for index, beat in enumerate(beats.said) if index == 0 or beats.said[index - 1] != beat]
            assert said[:4] == ["connecting", "connected", "connecting", "connected"], beats.said
        finally:
            stop.set()
            await asyncio.wait_for(running, 10)

    try:
        asyncio.run(run())
    finally:
        broker.stop()
