"""The Player reports what it drew its Frames with and whether its room-lights reader follows: through
the running PlayerService's test rig (tests/test_player_service.py) with Central's answers recorded,
the report loop PUTs ROOM_LIGHTS_DRAWN_PATH on a change and at least every repeat period."""
from __future__ import annotations

import ast
import asyncio
import inspect
import json

import httpx
import pytest
from test_player_service import TRUST, Server, boot_context, close, immediate, rig
from uplink_fakes import finding

import player.service as service_module
from contracts.room_light import (
    ROOM_LIGHTS_DRAWN_PATH,
    Dimmer,
    FixedColour,
    RoomLightsDocument,
    WhiteEntry,
    room_lights_digest,
)
from contracts.time import ManualClock
from player.identity import load_identity
from player.luts import PlayerLuts
from player.rendering import RecordingRenderer
from player.service import PlayerConfig, PlayerService

TURN = 0.02


class DrawnServer(Server):
    """Central answering the report 204, or `refusals` 503s first."""

    def __init__(self, clock, refusals: int = 0):
        super().__init__(clock)
        self.reports: list[dict] = []
        self.refusals = refusals

    def __call__(self, request):
        if request.url.path != ROOM_LIGHTS_DRAWN_PATH:
            return super().__call__(request)
        self.requests.append(request)
        assert request.method == "PUT"
        if self.refusals:
            self.refusals -= 1
            return httpx.Response(503, json={"error": "fixture_outage"})
        self.reports.append(json.loads(request.content))
        return httpx.Response(204)


def document(on: bool) -> RoomLightsDocument:
    return RoomLightsDocument(change=1, lights={
        "bookshelf-spot": WhiteEntry(colour=FixedColour(kelvin=2700), state=Dimmer(on=on, brightness=1.0))})


async def _until(check, what: str, seconds: float = 3.0) -> None:
    async def wait():
        while not check():
            await asyncio.sleep(TURN / 2)
    try:
        await asyncio.wait_for(wait(), seconds)
    except TimeoutError:
        raise AssertionError(what) from None


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(service_module, "ROOM_LIGHTS_DRAWN_TURN", TURN)


def test_the_player_reports_the_document_it_drew_and_its_readers_health(tmp_path, quick):
    """A document drawn through the GLib hop is reported once with its digest and following; the same
    document drawn again sends nothing before the repeat period; the reader no longer following is
    reported. Mutation probe: `_draw_room_lights` stops recording the digest -> drawn stays None."""
    async def check():
        server = DrawnServer(ManualClock(100))
        service, _ = await rig(tmp_path, server=server)
        hops = []
        service._room_lights_dispatch = hops.append
        following = [True]
        service.watch_room_lights_reader(lambda: following[0])
        task = asyncio.create_task(service._room_lights_drawn_loop())
        try:
            await _until(lambda: server.reports, "the first report")
            assert server.reports == [{"drawn": None, "following": True}]

            lit = document(True)
            service.room_lights_changed(lit)
            assert service.room_lights_drawn().drawn is None          # not drawn until the hop runs
            hops.pop()()
            digest = room_lights_digest(lit)
            await _until(lambda: len(server.reports) == 2, "the drawn document's report")
            assert server.reports[1] == {"drawn": digest, "following": True}

            service.room_lights_changed(lit)                            # drawn again, the same
            hops.pop()()
            await asyncio.sleep(10 * TURN)
            assert len(server.reports) == 2

            following[0] = False
            await _until(lambda: len(server.reports) == 3, "the reader's health")
            assert server.reports[2] == {"drawn": digest, "following": False}
            assert service.renderer.room_lights == [lit, lit]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await close(service)
    asyncio.run(check())


def test_a_refused_report_is_tried_again_and_the_report_repeats(tmp_path, quick, monkeypatch):
    """A report Central refuses is tried at the next turn; one Central accepted is sent again every
    repeat period. Before a reader is watched the report reads not following."""
    monkeypatch.setattr(service_module, "ROOM_LIGHTS_DRAWN_REPEAT_SECONDS", 5 * TURN)

    async def check():
        server = DrawnServer(ManualClock(100), refusals=2)
        service, _ = await rig(tmp_path, server=server)
        task = asyncio.create_task(service._room_lights_drawn_loop())
        try:
            await _until(lambda: len(server.reports) >= 3, "the refused report, then its repeats")
            assert server.refusals == 0
            assert all(report == {"drawn": None, "following": False} for report in server.reports)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await close(service)
    asyncio.run(check())


def test_a_running_session_sends_the_report(tmp_path, quick, monkeypatch):
    """run() starts the report with the session's other loops. Mutation probe: leave it out of the
    session's tasks -> Central never hears it."""
    monkeypatch.setattr(service_module, "BACKOFF", (.001,) * 4)

    async def check():
        directory = tmp_path / "cache"
        directory.mkdir(mode=0o700)
        server = DrawnServer(ManualClock(100))

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
            await _until(lambda: server.reports, "the running session's report")
            assert server.reports[0] == {"drawn": None, "following": False}
        finally:
            service.stop()
            await asyncio.wait_for(task, 3)
            await close(service)
    asyncio.run(check())


def test_main_wires_the_room_lights_through_start_room_lights():
    """main() cannot run off a Pi (GTK, GLib, the Node's bus), so its one wiring call is read from its
    source: the service is the room lights' sink, so the reader's health is reported. Mutation
    probe: main() calls follow_room_lights -> this fails."""
    tree = ast.parse(inspect.getsource(service_module.main))
    called = {node.func.id: [ast.unparse(argument) for argument in node.args]
              for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert called.get("start_room_lights") == ["service", "NODE_BUS_URL"]
    assert "follow_room_lights" not in called
