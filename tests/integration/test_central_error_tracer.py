"""The Frame shows Central's error when Central's doubt blacks it, end to end (DESIGN-r8 §9's test twin;
BUILD.md, owner 2026-10-27: "1&2&3 all have the same answer: when the issue induces blackness on the
frame").

Through each side's own composition: Central's app for House > Lights; the worker as `media.worker`
composes it, its hub session reading Home Assistant's states on a hub on free ports; Home Assistant
at the hub's door publishing Galleria v0.40.1's recorded light state; a Node on the shipped
configuration; the Player started on that Node (its line, its room-lights reader, its judge), drawing
on a recording renderer; and Display Host's controller as its runner wires it (`BusImpededReports`
on its own reader of the Player's line, `CentralCards` composing the card per Output).

C1. The spot is stated off by a current Galleria: the Frame it alone lights is black, but nothing is
    in doubt, so no card.
C2. v0.40.1's recorded state for the same spot: Central can't read it, the last word (off) stands,
    the Player draws exactly that (0 lx) and reports the Frame impeded under the grant it drew with,
    and the controller composes Central's card for HDMI-A-1: Central's two lines, verbatim.
C3. One ended Player run: with no run admitted (the slate) there is no Central card, and a new run's
    admission under another grant ignores the ended run's report until the new run restates it.
    (Which card the overlay draws while the Pi's own card shows, the Pi's, is the overlay client's
    rule: tests/node/display/test_display_central_card.py and the display harness.)
C4. A current Galleria states the spot on: the doubt clears, the Frame is lit, the report is empty
    and the card goes.

Why not the real-systemd PID1 leg DESIGN-r8 §9 asked for: in that fixture the Player has no GPU (its
renderer needs /dev/dri/renderD128; Weston runs headless on pixman), so it never presents a Frame and
can never be judged impeded, and the fixture's one Output, Virtual-1, is not an Output Central's card
may name (contracts/node_output.py OUTPUT_IDS). The card's pixels inside the Frame's area are the
display harness's (tests/native_display_smoke.py, real Weston capture).
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi.testclient import TestClient
from integration.bus_servers import hub_server, node_server, until
from integration.room_light_rig import HomeAssistant, Worker
from integration.test_central_room_light_tracer import V0_40_1_LIGHT
from test_executor import binding
from test_player_service import close, rig
from test_registry import ADMIN

from appliance.display_host.bus import BusImpededReports
from appliance.display_host.central_card import CentralCards
from central.app import create_app
from central.content_catalog.catalog import device_id_for_serial
from central.infra.home_assistant.references import light_reporter
from central.node_bus_wiring import HUB_CONFIG_ENV, HUB_URL_ENV, WALL_RECHECK_SECONDS
from central.room_light.model import ReportedLight, TypedColour
from central.room_light_routes import HOUSE_LIGHTS
from contracts.frame_lighting import AT_FULL, FrameLighting, Paper, WashLight
from contracts.home_assistant import HomeAssistantLight
from contracts.node_impeded import ImpededFrames
from contracts.node_output import CentralCard
from contracts.room_light import Dimmer, ReportedEntry, RoomLightsDocument

SERIAL = "serial-central-error"
ENTITY = "light.bookshelf_spot"
NAME = "Bookshelf spot"          # as v0.40.1's recorded state names it
LIGHT = "bookshelf-spot"
OUTPUT = "HDMI-A-1"
FRAME = "my-frame"
ON = HomeAssistantLight(name=NAME, on=True, brightness=255, colour=None)
OFF = HomeAssistantLight(name=NAME, on=False, brightness=None, colour=None)
# The Frame is lit by the spot alone: off, it is drawn at 0 lx (black).
LIGHTING = FrameLighting(mat_depth_mm=0, lights=(WashLight(light=LIGHT, lux=150, reference=AT_FULL),),
                         paper=Paper(whiteness=.9, ink_density=2))
FIRST_SECONDS = 30   # the worker reaches the hub, the Node links and its WALL mirror first syncs
SECONDS = WALL_RECHECK_SECONDS / 2   # every later step: a wake, never the WALL writer's own recheck


def _spot(document: RoomLightsDocument | None) -> ReportedEntry | None:
    entry = None if document is None else document.lights.get(LIGHT)
    return entry if isinstance(entry, ReportedEntry) else None


class _Shell:
    """The controller's card sink (the runner's ShellCards): every card sent, in order."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, CentralCard | None]] = []

    def send(self, output_id: str, card: CentralCard | None) -> None:
        self.sent.append((output_id, card))


def test_the_frame_shows_centrals_card_while_its_doubt_blacks_it(registry, tmp_path, monkeypatch):
    """Mutation probes: the controller matching an entry without its grant -> C3's new run shows the
    ended run's card before restating; `CentralCards.reported` composing nothing -> C2's card never
    comes."""
    monkeypatch.setenv("PHOTO_WALL_CACHE_ROOT", str(tmp_path / "worker-cache"))
    database = registry.db
    with database.transaction() as conn:   # the Node's device, as its boot offer records it
        conn.execute("INSERT INTO devices(device_id, serial, first_seen, last_seen) VALUES (%s, %s, 1, 1)",
                     (device_id_for_serial(SERIAL), SERIAL))
    hub = hub_server(tmp_path, [SERIAL])
    node = node_server(tmp_path, SERIAL, hub)
    house = HomeAssistant(f"ws://127.0.0.1:{hub.websocket_port}")
    env = {HUB_URL_ENV: hub.client_url, HUB_CONFIG_ENV: str(tmp_path / "worker-hub.json")}
    shell = _Shell()
    reports = BusImpededReports(url=node.client_url)     # Display Host's reader, as runner.main wires it
    cards = CentralCards(reports, shell.send)
    reports.on_change(cards.reported)
    stoppers: list[Callable] = []

    with TestClient(create_app(database, registry.clock, ADMIN)) as client:
        answer = client.put(f"{HOUSE_LIGHTS}/{LIGHT}", headers={"Authorization": "Bearer " + ADMIN},
                            json=ReportedLight(name=NAME, reporter=light_reporter(ENTITY),
                                               colour=TypedColour(kelvin=2700)).model_dump(mode="json"))
        assert answer.status_code < 300, answer.text

        async def player_run(directory: str, grant: str):
            """A Player run on the Node, its one Frame on OUTPUT lit by the spot alone, drawing under
            `grant`; returns (service, turn) where `turn()` runs one main-loop turn (the room-lights
            hop, then the turn that judges and reports)."""
            folder = tmp_path / directory
            folder.mkdir()
            service, server = await rig(folder, node_bus_url=node.client_url)
            service.renderer.grants[OUTPUT] = grant
            hops: list[Callable[[], None]] = []
            service._room_lights_dispatch = hops.append   # the GLib hop, run here on the "main loop"
            server.offer(bindings=(binding(OUTPUT, FRAME, lighting=LIGHTING),),
                         configuration_revision=2, revision=2, layers=())
            await service.poll_state()

            def turn() -> None:
                while hops:
                    hops.pop(0)()
                service.tick_main()

            async def stop() -> None:
                service.stop()
                await close(service)
            stoppers.append(stop)
            return service, turn

        def drawn(service) -> RoomLightsDocument | None:
            documents = service.renderer.room_lights
            return documents[-1] if documents else None

        def settles(turn, check):
            async def poll():
                turn()
                return check()
            return poll

        async def run() -> None:
            await asyncio.to_thread(hub.start)
            await asyncio.to_thread(node.start)
            await house.__aenter__()
            stoppers.append(house.__aexit__)
            worker = Worker(database.dsn, env)
            worker.start()
            stoppers.append(worker.stop)
            reports.start()

            # C1: stated off by a current Galleria: black, nothing in doubt, no card.
            await house.publish(ENTITY, OFF)
            first = "6f1c2a4e-1b2d-4c3e-8f40-5a6b7c8d9e0f"
            service, turn = await player_run("run-1", first)
            service.start_node_bus()
            cards.admitted(OUTPUT, first, FRAME, trial=False)   # the shell admitted this run's surface

            def off_and_trusted():
                spot = _spot(drawn(service))
                return spot is not None and spot.state == Dimmer(on=False, brightness=0.0) and spot.doubt is None
            await until(settles(turn, off_and_trusted), FIRST_SECONDS, "the Player draws the spot off")
            await until(settles(turn, lambda: reports.latest() == ImpededFrames(frames=())), SECONDS,
                        "the Player says its black Frame is not impeded")
            assert cards.card(OUTPUT) is None
            (black,) = service.frame_reports().frames
            assert black.light_lux == 0

            # C2: v0.40.1's recorded state: Central can't read it, the last word (off) stands.
            await house.publish(ENTITY, V0_40_1_LIGHT)

            def in_doubt():
                spot = _spot(drawn(service))
                return (spot is not None and spot.doubt is not None
                        and spot.doubt.line1.startswith(f"Photo Wall can't read {NAME} · since"))
            await until(settles(turn, in_doubt), SECONDS, "the Player's document says Central can't read the spot")
            spot = _spot(drawn(service))
            assert spot.state == Dimmer(on=False, brightness=0.0)        # drawn as stated: black

            async def card_up():
                turn()
                return cards.card(OUTPUT)
            card = await until(card_up, SECONDS, "the controller composes Central's card")
            assert card == CentralCard(OUTPUT, spot.doubt.line1, spot.doubt.line2)
            (stopped,) = reports.latest().frames
            assert (stopped.frame_id, stopped.output_id, stopped.grant_id, stopped.lights) == (
                FRAME, OUTPUT, first, (LIGHT,))
            assert shell.sent[-1] == (OUTPUT, card)

            # C3: the run ends. The slate: no Central card, the ended run's report still on the bus.
            await stoppers.pop()()
            cards.admitted(OUTPUT, None, None, trial=False)
            assert cards.card(OUTPUT) is None and reports.latest().frames[0].grant_id == first
            # A new run admitted under another grant: the ended run's report counts for nothing ...
            second = "0b8f7c4e-3a1d-4c2b-9e6f-5d4c3b2a1f0e"
            service, turn = await player_run("run-2", second)
            cards.admitted(OUTPUT, second, FRAME, trial=False)
            assert cards.card(OUTPUT) is None
            # ... until the new run restates it.
            service.start_node_bus()
            again = await until(card_up, FIRST_SECONDS, "the new run restates the report and the card returns")
            assert again == card and reports.latest().frames[0].grant_id == second

            # C4: a current Galleria states the spot on: the doubt clears, the Frame is lit, no card.
            await house.publish(ENTITY, ON)

            async def card_gone():
                turn()
                return cards.card(OUTPUT) is None and reports.latest() == ImpededFrames(frames=())
            await until(card_gone, SECONDS, "the stated spot clears the card")
            assert _spot(drawn(service)).doubt is None
            (lit,) = service.frame_reports().frames
            assert lit.light_lux > 0
            assert shell.sent[-1] == (OUTPUT, None)

        async def main() -> None:
            try:
                await run()
            finally:
                while stoppers:
                    await stoppers.pop()()

        try:
            asyncio.run(main())
        finally:
            reports.stop()
            node.stop()
            hub.stop()
