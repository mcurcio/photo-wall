"""A picture setting crosses the display line both ways (roadmap 1c, slice C1).

A real hub on Fleet's generated configuration, a real Node bus, and the display line's session on
the shipped `DISPLAY_SLICE`, as tests/integration/test_central_display_power.py runs them. (1) The
Node's Output report with a picture part is recorded and judged: the Display's first probe places
brightness on the display (it answered) and contrast as picture adjustment (it did not). (2) A
brightness edit made through the operator API, in its own process, reaches the Node's
`output-HDMI-A-1` with a new picture change and the same power change within 3 s: carried by the
notification (no reconcile runs between the edit and its arrival). Mutation probe: drop the
session write's NOTIFY -> the edit arrives only at the session's idle deadline, past 3 s.
"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from integration.bus_servers import hub_server, leaf_connections, node_server, until
from test_display_identity import ADMIN, AUTH, MODE, NO_SERIAL, enroll_pi

from appliance.display_host.bus import DISPLAY_COMPONENT, DISPLAY_SLICE, DISPLAY_VERSION
from central.app import create_app
from central.content_catalog.catalog import device_id_for_serial
from central.infra.display_store import (
    REPORT_STREAM,
    DisplayDocuments,
    DisplayWakes,
    OutputReportJudge,
)
from central.infra.node_link_store import PgLinkStores
from central.infra.node_links import NodeLinks
from central.registry import FrameCreate, Registry
from contracts.models import FrameProfile
from contracts.node_link import CENTRAL_WRITER, Pipe, account_id
from contracts.node_output import (
    SCHEMA_MAJOR,
    ActsOn,
    OutputReport,
    PictureReport,
    PictureSetting,
    SettingReport,
    decode_output_document,
    encode_output_report,
    output_document_key,
    output_report_key,
)
from contracts.time import ManualClock
from nodeapi.hub import NodeLink
from nodeapi.node import NodeSession, Release

SERIAL = "serial-display-picture"
SECONDS = 30


def _report(for_change: int) -> bytes:
    """The Pi's report on HDMI-A-1: DDC/CI answered brightness (read 40) but not contrast."""
    picture = PictureReport(True, False, for_change, SettingReport(ActsOn.DISPLAY, 40, None),
                            SettingReport(ActsOn.PICTURE_ADJUSTMENT, None, None), None)
    return encode_output_report(OutputReport("HDMI-A-1", True, NO_SERIAL, MODE, (), None, None, None, None, picture))


def test_a_picture_edit_and_a_probe_cross_the_display_line(database, tmp_path, monkeypatch):
    reconciles: list[float] = []
    reconcile = NodeLink.reconcile

    async def counted(self):
        epochs = await reconcile(self)
        reconciles.append(asyncio.get_running_loop().time())
        return epochs
    monkeypatch.setattr(NodeLink, "reconcile", counted)
    registry = Registry(database, ManualClock(1000))
    player = enroll_pi(registry, SERIAL)
    registry.create_frame(FrameCreate(id="kitchen", width_mm=300, height_mm=500,
                                      profile=FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)))
    registry.bind("kitchen", player, "HDMI-A-1", expected_generation=0)
    hub = hub_server(tmp_path, [SERIAL])
    node = node_server(tmp_path, SERIAL, hub)
    display = NodeSession(DISPLAY_COMPONENT, DISPLAY_SLICE,
                          Release(DISPLAY_VERSION, "sha256:display-test", {DISPLAY_COMPONENT: SCHEMA_MAJOR}),
                          url=node.client_url)
    hub.start()
    node.start()

    def placed() -> tuple:
        with database.transaction() as conn:
            row = conn.execute("SELECT d.brightness_acts_on, d.contrast_acts_on FROM output_displays o "
                               "JOIN displays d ON d.id=o.display_id WHERE o.player_id=%s", (player,)).fetchone()
        return None if row is None else (row["brightness_acts_on"], row["contrast_acts_on"])

    async def run(client: TestClient) -> None:
        display.start()
        assert await asyncio.to_thread(display.wait_attached, 15)

        async def linked():
            return account_id(SERIAL) in leaf_connections(hub)
        await until(linked, 15, "the Node's leaf links")
        wakes = DisplayWakes()
        device = device_id_for_serial(SERIAL)
        before_listening = wakes.count(device)
        listening = asyncio.create_task(wakes.listen(database.dsn))
        stores = PgLinkStores(database, {REPORT_STREAM: OutputReportJudge(wakes)})
        links = NodeLinks(Pipe.FLEET, hub.client_url, stores, DisplayDocuments(stores, wakes))
        stop = asyncio.Event()
        running = asyncio.create_task(links.run(stop))
        try:
            await links.track([SERIAL])

            async def held():
                found = display.desired.get(output_document_key("HDMI-A-1"))
                return (found is not None and found.writer == CENTRAL_WRITER
                        and decode_output_document(found.value))
            first = await until(held, SECONDS, "output-HDMI-A-1 on the Node")

            # (1) The probe is recorded and places the settings; the document follows the placement.
            display.state.put(output_report_key("HDMI-A-1"), _report(first.picture_change))

            async def judged():
                return placed() == ("display", "picture-adjustment")
            await until(judged, SECONDS, "the first probe placing the Display's settings")

            async def placed_document():
                found = await held()
                return found if found and found.picture.brightness.acts_on is ActsOn.DISPLAY else None
            document = await until(placed_document, SECONDS, "the placement in the Node's document")

            async def settled():
                return len(reconciles) >= 2
            await until(settled, SECONDS, "the link's reconcile after the session's birth")
            await asyncio.wait_for(wakes.after(device, before_listening), SECONDS)
            await asyncio.sleep(1.5)   # the judge's wake and any reconcile it started have run

            # (2) An edit in the operator API's process reaches the Node by notification. The session
            # begins first and its own wake settles, so only the edit's notification is left (the
            # session's idle deadline, 5 s after its begin, is past the 3 s this allows).
            begun = await asyncio.to_thread(client.post, "/v1/operator/frames/kitchen/picture-sessions",
                                            headers=AUTH)
            assert begun.status_code == 201, begun.text
            await asyncio.sleep(1.0)
            reconciled = len(reconciles)
            edit = await asyncio.to_thread(
                client.post, f"/v1/operator/picture-sessions/{begun.json()['session_id']}/edits", headers=AUTH,
                json={"setting": "brightness", "value": 60, "expected_sequence": 1})
            assert edit.status_code == 200, edit.text

            async def edited():
                found = await held()
                return found if found and found.picture.brightness == PictureSetting(ActsOn.DISPLAY, 60) else None
            carried = await until(edited, 3, "the brightness edit on the Node, by notification")
            assert (carried.change, carried.picture_change) == (document.change, document.picture_change + 1)
            assert len(reconciles) == reconciled   # no reconcile carried it
            assert not running.done()
        finally:
            stop.set()
            await asyncio.wait_for(running, SECONDS)
            listening.cancel()

    try:
        with TestClient(create_app(database, registry.clock, ADMIN, run_scheduler=False)) as client:
            asyncio.run(run(client))
    finally:
        display.stop()
        node.stop()
        hub.stop()
