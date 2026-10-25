"""The light tracer (the Home Assistant system design, draft 4, Q1-Q3 A): a light Home Assistant
publishes at Central's door reaches the Frame it lights, read back through the Pi's read-only door.

Real composition: Central's app under uvicorn (its door relayed to the hub's WebSocket listener), the
worker as `media.worker` runs it with its Node bus from `build_node_bus` (Central's hub session
inside), a hub on Fleet's configuration, one Node on the shipped node-bus.conf with its door, and
room_light_rig's Player started on that Node and bound to the Frame Central binds (its control State
carries Central's bindings, lighting included). A new Frame's lighting views the house's fixed light;
House › Lights then makes that light the one `light.test` reports.

1. As Home Assistant (nats-py over WebSocket at Central's door, as HOME_ASSISTANT_USER; the Galleria
   integration's own tests prove the integration publishes as this does): wait for `states-epoch`,
   then publish light.test off, then on at brightness 255.
2. Through the Pi's read-only door (no credentials, its server name checked): the Frame's FrameReport
   carries the generation Central's `installation` names (read at Central's door) and its light_lux
   rises above the value it had while the light was off.
3. Through the Pi's door a publish to the Player's `frames` is a permissions violation and the value
   is unchanged; through Central's door a publish to Central's state is refused.

The Frame is bound twice in setup, so its generation is 2: a Player reporting any other generation
never matches.
"""
from __future__ import annotations

import asyncio

from integration.bus_servers import hub_server, node_server, until
from integration.home_assistant_rig import (
    BoundPlayer,
    CentralOrigin,
    PiReader,
    central_bindings,
    central_state,
    refused,
    worker,
)
from integration.room_light_rig import HomeAssistant
from test_registry import enroll, frame, hello

from central.content_catalog.catalog import device_id_for_serial
from central.infra.home_assistant.references import light_reporter
from central.room_light.model import ReportedLight, TypedColour
from central.room_light_routes import HOUSE_LIGHTS
from contracts.central_api import CENTRAL_STATE_PREFIX, INSTALLATION_KEY, STATES_EPOCH_KEY, DrivenBy
from contracts.home_assistant import HomeAssistantLight
from contracts.node_state import (
    FRAMES_KEY,
    PLAYER_LINE,
    FrameReport,
    FrameReports,
    decode_frame_reports,
    encode_frame_reports,
)
from nodeapi.buffers import state_prefix, state_stream

SERIAL = "serial-home-assistant"
FRAME = "hall"
ENTITY = "light.test"
FRAMES_STREAM, FRAMES_SUBJECT = state_stream(PLAYER_LINE), state_prefix(PLAYER_LINE) + FRAMES_KEY
FIRST_SECONDS = 30   # the worker reaches the hub, the Node links, its WALL mirror first syncs
SECONDS = 10         # every later step


def _light(on: bool) -> HomeAssistantLight:
    return HomeAssistantLight(name="Test light", on=on, brightness=255 if on else None, colour=None)


def test_a_home_assistant_light_reaches_its_frame_read_through_the_pis_door(registry, tmp_path):
    database = registry.db
    device_id = device_id_for_serial(SERIAL)
    with database.transaction() as conn:   # the Node's device, as its boot offer records it
        conn.execute("INSERT INTO devices(device_id, serial, first_seen, last_seen) VALUES (%s, %s, 1, 1)",
                     (device_id, SERIAL))
    player, _, _ = enroll(registry, device_id=device_id)
    hello(registry, player)

    hub = hub_server(tmp_path, [SERIAL])
    node = node_server(tmp_path, SERIAL, hub)
    central = CentralOrigin(database, registry.clock, hub)
    stoppers: list = []

    async def run() -> None:
        # The Frame, bound twice (generation 2); the light its lighting views becomes light.test's.
        frame(registry, FRAME)
        registry.bind(FRAME, player["player_id"], "HDMI-A-2", expected_generation=0)
        registry.bind(FRAME, player["player_id"], "HDMI-A-1", expected_generation=1)
        bindings = await asyncio.to_thread(central_bindings, central, player)
        [(frame_id, generation, [light])] = [
            (b.frame_id, b.generation, [frame_light.light for frame_light in b.lighting.lights]) for b in bindings]
        assert (frame_id, generation) == (FRAME, 2)
        await asyncio.to_thread(central.call, "PUT", f"{HOUSE_LIGHTS}/{light}", json=ReportedLight(
            name="Test light", reporter=light_reporter(ENTITY), colour=TypedColour(kelvin=3000)).model_dump(mode="json"))

        running = worker(database.dsn, hub, tmp_path)
        running.start()
        stoppers.append(running.stop)
        followed = BoundPlayer(tmp_path / "player", node.client_url, bindings)
        followed.start()
        stoppers.append(followed.stop)

        # 1. Home Assistant at Central's door: the states epoch, then light.test off.
        async with HomeAssistant(central.door_url) as house:
            await until(lambda: central_state(house, STATES_EPOCH_KEY), FIRST_SECONDS,
                        "Central's hub session names the states epoch")
            await house.publish(ENTITY, _light(False))

            async def driven_by() -> DrivenBy | None:
                installation = await central_state(house, INSTALLATION_KEY)
                listed = [f.driven_by for f in installation.frames if f.id == FRAME] if installation else []
                return listed[0] if listed else None
            named = await until(driven_by, FIRST_SECONDS, "Central's installation names who drives the Frame")
            assert (named.serial, named.output_id, named.generation) == (SERIAL, "HDMI-A-1", 2)

            # 2. Through the Pi's read-only door: the Frame's report at that generation, dark, then lit.
            reader = await PiReader.at(node, SERIAL, SECONDS)
            stoppers.append(reader.close)

            def report(check):
                async def read() -> FrameReport | None:
                    raw = await reader.get(FRAMES_STREAM, FRAMES_SUBJECT)
                    ours = [r for r in decode_frame_reports(raw).frames
                            if (r.frame_id, r.generation, r.output_id) == (FRAME, named.generation, named.output_id)
                            ] if raw is not None else []
                    return ours[0] if ours and check(ours[0]) else None
                return read
            dark = await until(report(lambda r: r.light_lux is not None), FIRST_SECONDS,
                               "the Pi reports the Frame drawn at Central's generation")
            await house.publish(ENTITY, _light(True))
            lit = await until(report(lambda r: r.light_lux > dark.light_lux), SECONDS,
                              "the light on the Frame rises once light.test is on")

            # 3. Both doors' permissions are the servers': a write through either is refused, nothing changes.
            before = await reader.get(FRAMES_STREAM, FRAMES_SUBJECT)
            forged = encode_frame_reports(FrameReports(frames=(lit.model_copy(update={"light_lux": 99999.0}),)))
            await refused(reader.client, reader.violations, FRAMES_SUBJECT, forged)
            assert await reader.get(FRAMES_STREAM, FRAMES_SUBJECT) == before
            await refused(house.client, house.violations, CENTRAL_STATE_PREFIX + INSTALLATION_KEY,
                          b'{"frames":[],"players":[]}')
            assert (await central_state(house, INSTALLATION_KEY)).frames
        assert running.running()

    async def main() -> None:
        try:
            await run()
        finally:
            for stop in reversed(stoppers):
                await stop()

    try:
        hub.start()
        node.start()
        central.start()
        asyncio.run(main())
    finally:
        central.stop()
        node.stop()
        hub.stop()
