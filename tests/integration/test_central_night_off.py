"""Night off keeps on the Pi's own clock while Central is away (Power and the week, A7), on real
servers.

A real hub on Fleet's generated configuration, a real Node bus, and the display line's session on
the shipped `DISPLAY_SLICE`. Central runs the worker's fleet pipe (`NodeLinks` over `PgLinkStores`,
documents from `DisplayDocuments` on a fixed clock, reports judged by `OutputReportJudge`); the Pi
runs its power controller (the real `OutputPowerController` over the fake `ddcutil` harness, a
fake clock with both readings) fed by the display host's own `document_feed` and reporting through
`BusReportSink`. The house is Europe/London; Night off is 23:00 to 07:00 on every Frame.

At 16:00 Central writes the document and its links stop. The Pi's clock reaches 23:00 and it turns
the display off by itself; the links start again (Central at 23:01), the judge records the Pi's
report for Central's new document, and the Frame's Why says wanted off, because Night off, the Pi
carrying out the timetable's off, Resting. The links stop again, and the Pi turns the display on at
07:00.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi.testclient import TestClient
from integration.bus_servers import hub_server, leaf_connections, node_server, until
from node.display.test_display_power_methods import Clock, Curtain, Display, ddc
from support.house_time import LONDON, at, set_house
from test_display_identity import ADMIN, AUTH, enroll_pi

from appliance.display_host.bus import BusReportSink, display_session
from appliance.display_host.output_power import OutputPowerController
from appliance.display_host.runner import document_feed
from central.app import create_app
from central.frame_lighting.paper_profiles import LUT_RECIPE_ID
from central.infra.display_store import (
    REPORT_STREAM,
    DisplayDocuments,
    DisplayWakes,
    OutputReportJudge,
)
from central.infra.frame_lighting_store import PgFrameLighting
from central.infra.node_link_store import PgLinkStores
from central.infra.node_links import NodeLinks
from central.registry import FrameCreate, Registry
from contracts.models import FrameProfile
from contracts.node_link import Pipe, account_id
from contracts.node_output import InForceKind, Power, PowerResult, decode_output_report
from contracts.time import ManualClock

SERIAL = "serial-night-off"
OUTPUT = "HDMI-A-1"
SECONDS = 30
FOUR_PM = at(LONDON, "2026-10-12T16:00")
ELEVEN_PM = at(LONDON, "2026-10-12T23:00")
SEVEN_AM = at(LONDON, "2026-10-13T07:00")


class CentralClock:
    """Central's TransactionClock in this test: fixed where the test puts it."""

    def __init__(self, now: float) -> None:
        self.now = now

    def now_in(self, conn: Any) -> float:
        return self.now


class NotUnderTest:
    """The picture controller and the screen turns of the display host's document feed: not under
    test here."""

    def document(self, output_id, document) -> None:
        return None


def _stored_report(database) -> Any:
    with database.transaction() as conn:
        row = conn.execute("SELECT report FROM output_displays WHERE output_id=%s", (OUTPUT,)).fetchone()
    if row is None or row["report"] is None:
        return None
    return decode_output_report(json.dumps(row["report"]).encode())


def _latest_change(database) -> int:
    with database.transaction() as conn:
        return conn.execute("SELECT change FROM output_documents WHERE output_id=%s", (OUTPUT,)).fetchone()["change"]


def test_the_pi_keeps_night_off_while_central_is_away(database, tmp_path):
    """Mutation probe (P-central-flips): make the Pi act only on `now` (ignore `changes`) -> no off
    while Central is stopped."""
    registry = Registry(database, ManualClock(FOUR_PM), lighting=PgFrameLighting(LUT_RECIPE_ID))
    set_house(database, LONDON)
    player = enroll_pi(registry, SERIAL)
    registry.create_frame(FrameCreate(id="bedroom", width_mm=300, height_mm=500,
                                      profile=FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)))
    registry.bind("bedroom", player, OUTPUT, expected_generation=0)
    with database.transaction() as conn:   # Night off on every Frame (the seed chose the Frames there were)
        conn.execute("""UPDATE night_off SET settings = jsonb_set(settings, '{frames}', '{"kind": "all-frames"}')""")

    display = Display(tmp_path)
    pi_clock = Clock()
    pi_clock.wall = FOUR_PM
    hub = hub_server(tmp_path, [SERIAL])
    node = node_server(tmp_path, SERIAL, hub)
    session = display_session("sha256:night-off-test", url=node.client_url)
    power = OutputPowerController({adapter.method: adapter for adapter in (ddc(display),)},
                                  BusReportSink(session), pi_clock, Curtain())
    central_clock = CentralClock(FOUR_PM)
    hub.start()
    node.start()

    async def run(client: TestClient) -> None:
        session.start()
        assert await asyncio.to_thread(session.wait_attached, 15)
        session.desired.on_change(document_feed(power, NotUnderTest(), NotUnderTest()))
        power.start()
        power.output(OUTPUT, connected=True, identity=None, modes=())

        async def linked():
            return account_id(SERIAL) in leaf_connections(hub)
        await until(linked, 15, "the Node's leaf links")
        wakes = DisplayWakes()
        stores = PgLinkStores(database, {REPORT_STREAM: OutputReportJudge(wakes)})

        async def central_runs(wanted_change):
            """Central's links run until the judge has recorded the Pi's report for the document
            Central holds; then they stop (Central goes away)."""
            links = NodeLinks(Pipe.FLEET, hub.client_url, stores, DisplayDocuments(stores, wakes, times=central_clock))
            stop = asyncio.Event()
            running = asyncio.create_task(links.run(stop))
            try:
                await links.track([SERIAL])

                async def answered():
                    report = _stored_report(database)
                    return report if report is not None and wanted_change(report) else None
                return await until(answered, SECONDS, "the Pi's report for Central's document")
            finally:
                stop.set()
                await asyncio.wait_for(running, SECONDS)

        # 16:00: Central writes the timetable (on now, off at 23:00, on at 07:00, ...) and goes away.
        first = await central_runs(lambda report: report.in_force is not None)
        assert (first.in_force.kind, first.in_force.power) == (InForceKind.TIMETABLE, Power.ON)
        written = _latest_change(database)

        # 23:00 on the Pi's own clock, Central away: the Pi turns the display off by itself.
        pi_clock.wall = ELEVEN_PM

        async def off():
            report = power.report(OUTPUT)
            return report if report.in_force is not None and report.in_force.power is Power.OFF else None
        carried = await until(off, SECONDS, "the Pi's off at 23:00")
        assert (carried.in_force.kind, carried.result) == (InForceKind.TIMETABLE, PowerResult.CONFIRMED)
        assert display.power == "standby"
        assert _latest_change(database) == written           # nothing new came from Central

        # 23:01: Central is back; its document rises (off now) and the Pi answers it without a write.
        central_clock.now = ELEVEN_PM + 60
        registry.clock.advance(central_clock.now - registry.clock.utc())
        await central_runs(lambda report: report.for_change > written and report.in_force is not None)
        why = (await asyncio.to_thread(client.get, "/v1/operator/frames/bedroom/why", headers=AUTH)).json()
        assert why["power"]["wanted"] == "off" and why["power"]["because"] == "night-off"
        assert why["power"]["until"] == "2026-10-13T07:00"
        pi_says = why["power"]["pi_says"]
        assert (pi_says["kind"], pi_says["power"], pi_says["since"]) == ("timetable", "off", "2026-10-12T23:00")
        assert why["power"]["resting"] is True

        # Central away again: 07:00 on the Pi's clock turns the display on.
        pi_clock.wall = SEVEN_AM

        async def on():
            report = power.report(OUTPUT)
            return report if report.in_force.power is Power.ON else None
        await until(on, SECONDS, "the Pi's on at 07:00")
        assert display.power == "on"

    try:
        with TestClient(create_app(database, registry.clock, ADMIN, run_scheduler=False)) as client:
            asyncio.run(run(client))
    finally:
        power.stop()
        session.stop()
        node.stop()
        hub.stop()
