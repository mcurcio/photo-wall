"""The Player's judge of "impedes" and its `impeded` report (player/impediment.py, player/service.py
`impeded_frames`; owner 2026-10-27: "1&2&3 all have the same answer: when the issue induces blackness
on the frame").

Through the running PlayerService's test rig (tests/test_player_service.py) with its line recorded:
a Frame drawn black (0 lx at its centre) whose lighting names a light Central doubts is reported
impeded on every main-loop turn that changes it, under the grant its Output's last presented commit
was drawn under; a dim but lit Frame, a black Frame with every light trusted, and a Frame with no
presented commit are not. The report is put whole, empty too. The drawing never changes: the renderer
draws the very document Central sent. The same report on a real Node's bus, read through its door, is
tests/integration/test_node_bus_wall_reader.py's.
"""
from __future__ import annotations

import asyncio

from test_executor import binding
from test_player_service import close, rig

from contracts.frame_lighting import AT_FULL, AtReading, FrameLighting, Paper, WashLight
from contracts.node_impeded import Impeded, ImpededFrames
from contracts.room_light import (
    CentralWords,
    Dimmer,
    FixedEntry,
    KelvinColour,
    Measured,
    MeasuredEntry,
    ReportedEntry,
    RoomLightsDocument,
)
from player.impediment import impeded

GRANT = "0b8f7c4e-3a1d-4c2b-9e6f-5d4c3b2a1f0e"
FAMILY = CentralWords(line1="Photo Wall can't read Family Room Lights · since 29 Oct 18:05",
                      line2="Its last word: off (26 Oct 15:35) · see Needs attention in Photo Wall")
DAYLIGHT = CentralWords(line1="Home Assistant can't tell Daylight · since 29 Oct 17:00",
                        line2="Its last reading: 0 lx · see Needs attention in Photo Wall")
PAPER = Paper(whiteness=.9, ink_density=2)


def lighting(*lights: str) -> FrameLighting:
    return FrameLighting(mat_depth_mm=0, lights=tuple(WashLight(light=light, lux=150, reference=AT_FULL)
                                                      for light in lights), paper=PAPER)


def reported(on: bool, doubt: CentralWords | None = None, brightness: float = 1.0) -> ReportedEntry:
    return ReportedEntry(colour=KelvinColour(kelvin=2700), state=Dimmer(on=on, brightness=brightness), doubt=doubt)


def document(change: int = 1, **lights) -> RoomLightsDocument:
    return RoomLightsDocument(change=change, lights=lights)


# ---- The judge, pure ----

def test_a_black_frame_with_a_doubted_light_is_impeded_naming_every_doubted_light_in_id_order():
    """Mutation probes: drop the id ordering -> lights come out as the Frame names them; take the
    words of the Frame's first light -> the daylight words; judge `!= 0` as `> 1` -> a 0.5 lx Frame
    is impeded."""
    drawn = document(**{"family-room": reported(False, FAMILY),
                        "daylight": MeasuredEntry(colour=KelvinColour(kelvin=6500), state=Measured(lux=0),
                                                  doubt=DAYLIGHT),
                        "lamp": reported(False)})
    frame = FrameLighting(mat_depth_mm=0, paper=PAPER, lights=(
        WashLight(light="family-room", lux=150, reference=AT_FULL),
        WashLight(light="daylight", lux=150, reference=AtReading(lux=1000)),
        WashLight(light="lamp", lux=80, reference=AT_FULL),
        WashLight(light="family-room", lux=40, reference=AT_FULL)))         # named twice: counted once
    assert impeded("my-frame", "HDMI-A-1", GRANT, frame, drawn, 0) == Impeded(
        frame_id="my-frame", output_id="HDMI-A-1", grant_id=GRANT, lights=("daylight", "family-room"),
        words=DAYLIGHT)
    assert impeded("my-frame", "HDMI-A-1", GRANT, frame, drawn, 0.5) is None   # dim, not black


def test_a_black_frame_whose_lights_are_trusted_or_unnamed_is_not_impeded():
    """Only a light the Frame's lighting names counts; a fixed light carries no doubt."""
    drawn = document(**{"family-room": reported(False), "hall": reported(False, FAMILY),
                        "window": FixedEntry(colour=KelvinColour(kelvin=6500))})
    assert impeded("my-frame", "HDMI-A-1", GRANT, lighting("family-room", "window", "gone"), drawn, 0) is None
    assert impeded("my-frame", "HDMI-A-1", GRANT, lighting(), drawn, 0) is None


# ---- The Player's report, through the running service ----

class RecordedLine:
    """The Player's line as the service drives it: every put of `impeded`, in order."""

    def __init__(self) -> None:
        self.impeded: list[ImpededFrames] = []

    def put_frames(self, reports) -> None:
        pass

    def put_drawn(self, drawn) -> None:
        pass

    def put_impeded(self, frames: ImpededFrames) -> None:
        self.impeded.append(frames)


async def _bound(tmp_path, lights: FrameLighting):
    service, server = await rig(tmp_path)
    service.line = RecordedLine()
    hops = []
    service._room_lights_dispatch = hops.append
    server.offer(bindings=(binding().model_copy(update={"generation": 2, "lighting": lights}),),
                 configuration_revision=2, revision=2, layers=())
    await service.poll_state()
    return service, hops


def _draw(service, hops, drawn: RoomLightsDocument) -> None:
    service.room_lights_changed(drawn)
    hops.pop()()
    service.tick_main()


def test_the_player_reports_its_frame_impeded_while_a_doubted_light_blacks_it_and_clears_it_after(tmp_path):
    """The Frame's Output was presented (its grant drawn under); Family Room's last word is off and
    Central can't read it: impeded, with Central's words verbatim and the grant. The renderer drew
    exactly that document (no stand-in light). Lit again, the report is put empty. Mutation probes:
    skip the put on change -> the clear never reaches the line; name another grant -> the grant
    differs."""
    async def check():
        service, hops = await _bound(tmp_path, lighting("family-room"))
        line = service.line
        try:
            grant = service.renderer.drawn_grant("hdmi1")
            assert grant is not None                                # the rig's Frame was presented
            service.renderer.grants["hdmi1"] = GRANT
            dark = document(1, **{"family-room": reported(False, FAMILY)})
            _draw(service, hops, dark)
            assert line.impeded[-1] == ImpededFrames(frames=(Impeded(
                frame_id="frame1", output_id="hdmi1", grant_id=GRANT, lights=("family-room",), words=FAMILY),))
            assert service.renderer.room_lights[-1] == dark         # card only: the drawing is the document's
            puts = len(line.impeded)
            service.tick_main()
            assert len(line.impeded) == puts                        # unchanged: not put again

            _draw(service, hops, document(2, **{"family-room": reported(True)}))
            assert line.impeded[-1] == ImpededFrames(frames=())
        finally:
            await close(service)
    asyncio.run(check())


def test_a_dim_frame_or_a_black_frame_lit_by_trusted_lights_is_not_impeded(tmp_path):
    """Dim but not black under a doubt: no card (its doubt shows on Needs attention). Black with every
    light trusted: no card. (The rig's first turn put the empty report already; unchanged, it is not
    put again.)"""
    async def check():
        service, hops = await _bound(tmp_path, lighting("family-room"))
        line = service.line
        try:
            _draw(service, hops, document(1, **{"family-room": reported(True, FAMILY, brightness=0.01)}))
            assert service.frame_reports().frames[0].light_lux > 0
            _draw(service, hops, document(2, **{"family-room": reported(False)}))
            assert service.frame_reports().frames[0].light_lux == 0
            assert line.impeded == [] and service.impeded_frames() == ImpededFrames(frames=())
        finally:
            await close(service)
    asyncio.run(check())


def test_a_frame_with_no_presented_commit_is_not_impeded(tmp_path):
    """No grant drawn under (a candidate's ground, or none yet): nothing Display Host could admit, so
    no report names the Frame. Once a commit is presented it is."""
    async def check():
        service, hops = await _bound(tmp_path, lighting("family-room"))
        line = service.line
        try:
            drawn_grant = service.renderer.drawn_grant
            service.renderer.drawn_grant = lambda output_id: None
            _draw(service, hops, document(1, **{"family-room": reported(False, FAMILY)}))
            assert line.impeded == []
            service.renderer.drawn_grant = drawn_grant
            service.tick_main()
            assert [frame.frame_id for frame in line.impeded[-1].frames] == ["frame1"]
        finally:
            await close(service)
    asyncio.run(check())
