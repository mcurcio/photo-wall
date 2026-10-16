"""The Player's room lights (decision 0028, design rule 2): a light switching redraws the Frames and
never rides the control State. Through the running PlayerService's test rig (tests/test_player_service.py)
and player/room_lights.py, the WallView callback the WallReader calls."""
from __future__ import annotations

import asyncio

from test_player_service import close, rig

from contracts.room_light import (
    ROOM_LIGHTS_KEY,
    Dimmer,
    FixedColour,
    RoomLightEntry,
    RoomLightsDocument,
    encode_room_lights,
)
from player.room_lights import RoomLights


def document(change: int, on: bool) -> RoomLightsDocument:
    return RoomLightsDocument(change=change, lights={
        "bookshelf-spot": RoomLightEntry(FixedColour(2700), Dimmer(on=on, brightness=1.0))})


def test_ten_switches_redraw_ten_times_and_leave_the_delivery_alone(tmp_path):
    """Acceptance 4: ten documents offered in a row to a running Player redraw ten times; no
    configuration or plan is applied, the executor's revisions and the delivery sequence stay, and
    nothing (no Readiness, no Commit) is sent to Central."""
    async def check():
        service, server = await rig(tmp_path)
        try:
            executor = service.executor
            before = (executor._last_revision, executor._last_configuration_revision, executor._plan,
                      service._highest_control_sequence, service._highest_control_delivery_id,
                      service._configuration, service._plan, len(server.requests))
            lights = RoomLights(service.room_lights_changed)
            for change in range(1, 11):
                lights.offer(ROOM_LIGHTS_KEY, encode_room_lights(document(change, change % 2 == 1)))
            assert [drawn.change for drawn in service.renderer.room_lights] == list(range(1, 11))
            assert lights.current() == document(10, False)
            after = (executor._last_revision, executor._last_configuration_revision, executor._plan,
                     service._highest_control_sequence, service._highest_control_delivery_id,
                     service._configuration, service._plan, len(server.requests))
            assert after == before
        finally:
            await close(service)
    asyncio.run(check())


def test_the_last_good_document_stays():
    drawn = []
    lights = RoomLights(drawn.append)
    lights.offer(ROOM_LIGHTS_KEY, encode_room_lights(document(5, True)))
    lights.offer(ROOM_LIGHTS_KEY, b'{"change":6,"lights":{"Bad Id":{}}}')     # refused: dropped
    lights.offer(ROOM_LIGHTS_KEY, encode_room_lights(document(5, False)))     # not newer: ignored
    lights.offer(ROOM_LIGHTS_KEY, encode_room_lights(document(4, False)))
    lights.offer("timing", encode_room_lights(document(9, False)))           # another WALL key
    assert [document_.change for document_ in drawn] == [5]
    assert lights.current() == document(5, True)


def test_several_documents_before_the_loop_runs_draw_once_newest(tmp_path):
    """The hop onto the GLib thread coalesces: the newest pending document wins."""
    async def check():
        service, _ = await rig(tmp_path)
        queued = []
        service._room_lights_dispatch = queued.append
        try:
            for change in (1, 2, 3):
                service.room_lights_changed(document(change, True))
            assert len(queued) == 1
            queued.pop()()
            assert [drawn.change for drawn in service.renderer.room_lights] == [3]
            service.room_lights_changed(document(4, False))
            assert len(queued) == 1
        finally:
            await close(service)
    asyncio.run(check())
