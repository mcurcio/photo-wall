"""The Player reports on its own line (player/player_line.py): through the running PlayerService's test
rig (tests/test_player_service.py) with the line recorded, every main-loop turn (`tick_main`) gives the
line `frames` when a Frame's report changes (its binding generation, its Output, whether its picture is
on screen, the light on it as last drawn) and `room-lights-drawn` on a change and at least every
ROOM_LIGHTS_DRAWN_REPEAT_SECONDS. The report never rides the control link: no HTTP request carries it.
The same reports on a real Node's bus, read through its door, are
tests/integration/test_node_bus_wall_reader.py's."""
from __future__ import annotations

import asyncio

import httpx
from test_executor import binding
from test_player_service import TRUST, Server, boot_context, close, immediate, rig
from uplink_fakes import finding

import player.service as service_module
from contracts.frame_lighting import AT_FULL, FrameLighting, Paper, WashLight
from contracts.models import Commit
from contracts.node_state import FrameReport, FrameReports
from contracts.room_light import (
    ROOM_LIGHTS_DRAWN_PATH,
    ROOM_LIGHTS_DRAWN_REPEAT_SECONDS,
    Dimmer,
    FixedColour,
    RoomLightsDocument,
    RoomLightsDrawn,
    WhiteEntry,
    room_lights_digest,
)
from contracts.time import ManualClock
from player.identity import load_identity
from player.luts import PlayerLuts
from player.rendering import RecordingRenderer
from player.service import PlayerConfig, PlayerService

LIGHT ="bookshelf-spot"
WASHED = FrameLighting(mat_depth_mm=0, lights=(WashLight(light=LIGHT, lux=150, reference=AT_FULL),),
                       paper=Paper(whiteness=.9, ink_density=2))


def document(change: int, on: bool) -> RoomLightsDocument:
    return RoomLightsDocument(change=change, lights={
        LIGHT: WhiteEntry(colour=FixedColour(kelvin=2700), state=Dimmer(on=on, brightness=1.0))})


class RecordedLine:
    """The Player's line as the service drives it: every put, in order."""

    def __init__(self) -> None:
        self.frames: list[FrameReports] = []
        self.drawn: list[RoomLightsDrawn] = []

    def put_frames(self, reports: FrameReports) -> None:
        self.frames.append(reports)

    def put_drawn(self, drawn: RoomLightsDrawn) -> None:
        self.drawn.append(drawn)


async def _recorded(tmp_path):
    service, server = await rig(tmp_path)
    service.line = RecordedLine()
    hops = []
    service._room_lights_dispatch = hops.append
    return service, server, hops


def test_frames_carry_each_bindings_generation_and_the_light_drawn_on_it(tmp_path):
    """Mutation probes: report the generation as 1 always -> the rebound Frame reads 1; skip the new
    document's light -> light_lux stays at its dark value."""
    async def check():
        service, server, hops = await _recorded(tmp_path)
        line = service.line
        try:
            # The rig's first state was reported on its turn already; unchanged, it is not put again.
            assert service.frame_reports() == FrameReports(frames=(FrameReport(
                frame_id="frame1", generation=1, output_id="hdmi1", showing=False, light_lux=None),))
            service.tick_main()
            assert line.frames == []

            # Bound again: a new generation and the Frame's lights.
            server.offer(bindings=(binding().model_copy(update={"generation": 2, "lighting": WASHED}),),
                         configuration_revision=2, revision=2, layers=())
            await service.poll_state()
            assert [report.generation for report in line.frames[-1].frames] == [2]
            assert line.frames[-1].frames[0].light_lux is None           # nothing drawn yet

            service.room_lights_changed(document(1, False))
            hops.pop()()
            service.tick_main()
            assert line.frames[-1].frames[0].light_lux == 0.0            # the light is off
            service.room_lights_changed(document(2, True))
            hops.pop()()
            service.tick_main()
            assert abs(line.frames[-1].frames[0].light_lux - 150) < 1e-6  # a wash at full: its lux
        finally:
            await close(service)
    asyncio.run(check())


def test_a_frame_reads_showing_once_its_picture_is_drawn(tmp_path):
    async def check():
        service, server, _ = await _recorded(tmp_path)
        try:
            assert service.frame_reports().frames[0].showing is False
            readiness, _ = service._feedback()
            assert await service.download(service._jobs[0])
            service.tick_main()
            readiness, _ = service._feedback()
            server.offer(commits=(Commit(plan_id="offered", revision=1, authority_epoch=1,
                                         readiness_sequence=readiness.sequence, assignment_ids=("picture",),
                                         committed_at=100),))
            await service.poll_state()
            assert service.line.frames[-1].frames[0].showing is True
        finally:
            await close(service)
    asyncio.run(check())


def test_the_drawn_report_is_put_on_a_change_and_again_every_repeat_period(tmp_path):
    """Mutation probe: stop re-putting an unchanged report -> no second put after the period."""
    async def check():
        service, _, hops = await _recorded(tmp_path)
        line = service.line
        following = [True]
        try:
            # Put on the rig's first turn already (no reader watched yet: not following); unchanged,
            # it is not put again before the period.
            assert service.room_lights_drawn() == RoomLightsDrawn(drawn=None, following=False)
            service.tick_main()
            assert line.drawn == []
            service.watch_room_lights_reader(lambda: following[0])
            service.tick_main()
            assert line.drawn[-1] == RoomLightsDrawn(drawn=None, following=True)

            lit = document(1, True)
            service.room_lights_changed(lit)
            service.tick_main()
            assert line.drawn[-1].drawn is None                     # not drawn until the hop runs
            hops.pop()()
            service.tick_main()
            assert line.drawn[-1] == RoomLightsDrawn(drawn=room_lights_digest(lit), following=True)

            count = len(line.drawn)
            service.clock.advance(ROOM_LIGHTS_DRAWN_REPEAT_SECONDS - 1)
            service.tick_main()
            assert len(line.drawn) == count                         # unchanged, not yet due
            service.clock.advance(1)
            service.tick_main()
            assert len(line.drawn) == count + 1 and line.drawn[-1] == line.drawn[-2]   # due: put again

            following[0] = False
            service.tick_main()
            assert line.drawn[-1] == RoomLightsDrawn(drawn=room_lights_digest(lit), following=False)
            assert service.renderer.room_lights == [lit]
        finally:
            await close(service)
    asyncio.run(check())


def test_a_running_session_sends_no_drawn_report_over_http(tmp_path, monkeypatch):
    """The report left the control link (P4): a running session polls its state, and no request it
    makes carries the drawn report."""
    monkeypatch.setattr(service_module, "BACKOFF", (.001,) * 4)

    async def check():
        directory = tmp_path / "cache"
        directory.mkdir(mode=0o700)
        server = Server(ManualClock(100))

        def handle(request):
            answer = server(request)
            if request.url.path == "/v1/enrollment/register":
                server.offer()
            return answer

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False)
        service = PlayerService(
            PlayerConfig(central_origin="http://central", cache_dir=str(directory), cache_bytes=1024**2),
            load_identity(), (), RecordingRenderer(), immediate, find_central=finding("http://central"),
            trust=TRUST, clock=server.clock, client=client, time_client=client, websocket_connect=False,
            boot_context=boot_context(), luts=PlayerLuts())
        task = asyncio.create_task(service.run())
        try:
            async def polled():
                while sum(request.url.path == "/v1/player/state" for request in server.requests) < 3:
                    await asyncio.sleep(.01)
            await asyncio.wait_for(polled(), 5)
            assert ROOM_LIGHTS_DRAWN_PATH not in {request.url.path for request in server.requests}
        finally:
            service.stop()
            await asyncio.wait_for(task, 3)
            await close(service)
    asyncio.run(check())
