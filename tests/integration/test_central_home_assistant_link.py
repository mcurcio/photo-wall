"""The Home Assistant link (central/infra/home_assistant/link.py) against the pinned nats-server's MQTT
listener (the house's broker, room_light_rig.py) and the real room-light store.

L1. It survives a database failure while recording: it disconnects, says "connecting" again,
    reconnects, is handed the retained state again and records it, and its task never ends (the
    worker's TaskGroup would end every loop with it). Mutation: `except Exception` in `_connecting`
    narrowed (to `ZeroDivisionError`) -> the link's task ends with the database's error.
L2. A room light or Sensor saved with an entity (in another process: House › Lights) has that
    entity's retained state read again at once, with no new message from Home Assistant. Mutation:
    `_named_in` not called on save (or `reread` returning at once) -> the word never comes back.
"""
from __future__ import annotations

import asyncio

from integration.bus_servers import until
from integration.room_light_rig import Broker

from central.infra import room_light_store
from central.infra.frame_lighting_store import PgHouseLock
from central.infra.home_assistant.link import HomeAssistantLink, broker_from
from central.infra.home_assistant.topics import (
    ILLUMINANCE_TOPIC_PREFIX,
    LIGHT_TOPIC_PREFIX,
    illuminance_sensor,
    light_reporter,
)
from central.infra.room_light_store import PgNamedReferences, PgRoomLights
from central.room_light.model import ReportedLight, Sensor
from contracts.room_light import FixedColour

ENTITY = "light.bookshelf_spot"
SENSOR = "sensor.hall_illuminance"


class _Beats:
    def __init__(self) -> None:
        self.said: list[str] = []

    def beat(self, beat: str) -> None:
        self.said.append(beat)


def test_a_recording_that_fails_reconnects_and_the_retained_state_is_recorded(registry, tmp_path, monkeypatch):
    tried: list[str] = []
    word_in = room_light_store._word_in

    def fails_once(conn, house, table, *rest):
        tried.append(table)
        if len(tried) == 1:
            conn.execute("SELECT 1/0")
        return word_in(conn, house, table, *rest)

    monkeypatch.setattr(room_light_store, "_word_in", fails_once)
    broker, beats = Broker(tmp_path), _Beats()
    broker.start()

    def kept() -> list[dict]:
        with registry.db.transaction() as conn:
            return conn.execute("SELECT address, word FROM light_words").fetchall()

    async def run() -> None:
        await broker.publish(LIGHT_TOPIC_PREFIX + ENTITY, b'{"state":"on","brightness":255}')
        stop = asyncio.Event()
        link = HomeAssistantLink(broker_from(broker.url), _lights(registry), beats,  # type: ignore[arg-type]
                                 PgNamedReferences(registry.db.dsn), close=lambda: None)
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


def _lights(registry) -> PgRoomLights:
    return PgRoomLights(registry.db, PgHouseLock())


def test_a_light_or_sensor_saved_has_its_entity_read_again(registry, tmp_path):
    broker, beats = Broker(tmp_path), _Beats()
    broker.start()
    lights = _lights(registry)

    def words() -> set[tuple[str, str]]:
        with registry.db.transaction() as conn:
            return {(row["kind"], row["address"]) for row in conn.execute(
                "SELECT 'light' AS kind, address FROM light_words UNION ALL "
                "SELECT 'reading', address FROM reading_words").fetchall()}

    def forget() -> None:
        """The words gone (as when a source's oldest are trimmed): only a re-read brings them back."""
        with registry.db.transaction() as conn:
            conn.execute("DELETE FROM light_words")
            conn.execute("DELETE FROM reading_words")

    async def run() -> None:
        await broker.publish(LIGHT_TOPIC_PREFIX + ENTITY, b'{"state":"on","brightness":255}')
        await broker.publish(ILLUMINANCE_TOPIC_PREFIX + SENSOR, b'{"lux":120}')
        stop = asyncio.Event()
        link = HomeAssistantLink(broker_from(broker.url), lights, beats,  # type: ignore[arg-type]
                                 PgNamedReferences(registry.db.dsn), close=lambda: None)
        running = asyncio.create_task(link.run(stop))
        try:
            async def both_heard():
                return await asyncio.to_thread(words) == {("light", ENTITY), ("reading", SENSOR)}
            await until(both_heard, 10, "the retained states are recorded at connect")

            async def following():   # the link LISTENs for the references House > Lights saves
                return await asyncio.to_thread(_listening, registry)
            await until(following, 10, "the link follows the saved references")
            await asyncio.to_thread(forget)
            await asyncio.to_thread(lights.save_room_light, "bookshelf-spot", ReportedLight(
                name="Bookshelf spot", reporter=light_reporter(ENTITY), colour=FixedColour(kelvin=2700)))

            async def light_again():
                return await asyncio.to_thread(words) == {("light", ENTITY)}
            await until(light_again, 5, "the saved light's entity is read again")
            await asyncio.to_thread(lights.save_sensor, "hall", Sensor(name="Hall", at=illuminance_sensor(SENSOR)))

            async def sensor_again():
                return await asyncio.to_thread(words) == {("light", ENTITY), ("reading", SENSOR)}
            await until(sensor_again, 5, "the saved Sensor's entity is read again")
            assert not running.done()
        finally:
            stop.set()
            await asyncio.wait_for(running, 10)

    try:
        asyncio.run(run())
    finally:
        broker.stop()


def _listening(registry) -> bool:
    with registry.db.transaction() as conn:
        return conn.execute("SELECT 1 FROM pg_stat_activity WHERE datname = current_database() "
                            "AND application_name=%s AND state='idle'",
                            (room_light_store._NAMED_APPLICATION,)).fetchone() is not None
