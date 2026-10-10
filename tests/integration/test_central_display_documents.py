"""Central's Output documents and the Output report judge on real servers (roadmap 1b, slice C1).

A real hub on Fleet's generated configuration, a real Node bus, and the display line's session on
the shipped `DISPLAY_SLICE`. Central runs the worker's own composition of the fleet pipe:
`NodeLinks` over `PgLinkStores` with the `OutputReportJudge`, its documents from
`DisplayDocuments`. (1) The Node's desired bucket receives `output-HDMI-A-1` (its enrolled Output)
with standing on. (2) The session's Output report is recorded and judged into `output_displays`
(the Display recognised on the bound Frame). (3) A report from an Output Central had no row for
gives that Output a document within 3 s, carried by the judge's wake (`changed()`), not by a
reconcile.
"""
from __future__ import annotations

import asyncio

from integration.bus_servers import hub_server, leaf_connections, node_server, until
from test_display_identity import enroll_pi

from appliance.display_host.bus import DISPLAY_COMPONENT, DISPLAY_SLICE, DISPLAY_VERSION
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
    BEST_DETECTED,
    SCHEMA_MAJOR,
    DisplayIdentity,
    DisplayMode,
    OutputReport,
    Power,
    PowerRequest,
    RequestReason,
    decode_output_document,
    encode_output_report,
    output_document_key,
    output_report_key,
)
from contracts.time import ManualClock
from nodeapi.node import NodeSession, Release

SERIAL = "serial-display-documents"
SECONDS = 30
IDENTITY = DisplayIdentity("XYM", 5475, "MNN", None)
MODES = (DisplayMode(1920, 1080, 60000, preferred=True),)


def _report(output_id: str) -> bytes:
    return encode_output_report(OutputReport(output_id, True, IDENTITY, MODES, (), None, None, None, None))


def test_a_nodes_output_documents_and_reports_cross_the_display_line(database, tmp_path):
    registry = Registry(database, ManualClock(1000))
    player = enroll_pi(registry, SERIAL)
    with database.transaction() as conn:   # one enrolled Output only: HDMI-A-2 has no `outputs` row
        conn.execute("DELETE FROM outputs WHERE player_id=%s AND output_id='HDMI-A-2'", (player,))
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

    def rows(sql: str, params=()) -> list[dict]:
        with database.transaction() as conn:
            return conn.execute(sql, params).fetchall()

    async def run() -> None:
        display.start()
        assert await asyncio.to_thread(display.wait_attached, 15)

        async def linked():
            return account_id(SERIAL) in leaf_connections(hub)
        await until(linked, 15, "the Node's leaf links")
        wakes = DisplayWakes()
        stores = PgLinkStores(database, {REPORT_STREAM: OutputReportJudge(wakes)})
        links = NodeLinks(Pipe.FLEET, hub.client_url, stores, DisplayDocuments(stores, wakes))
        stop = asyncio.Event()
        running = asyncio.create_task(links.run(stop))
        try:
            await links.track([SERIAL])

            # (1) The enrolled Output's document, standing on, reaches the Node's desired view.
            async def held(output_id: str):
                found = display.desired.get(output_document_key(output_id))
                return (found is not None and found.writer == CENTRAL_WRITER
                        and decode_output_document(found.value))
            document = await until(lambda: held("HDMI-A-1"), SECONDS, "output-HDMI-A-1 on the Node")
            assert (document.change, document.power, document.method) == (
                1, (PowerRequest("standing", Power.ON, RequestReason.STANDING),), BEST_DETECTED)
            assert display.desired.get(output_document_key("HDMI-A-2")) is None

            # (2) The Node's report is recorded and judged: the Display on the bound Frame.
            display.state.put(output_report_key("HDMI-A-1"), _report("HDMI-A-1"))

            async def judged():
                found = rows("SELECT o.connected, d.frame_id FROM output_displays o JOIN displays d "
                             "ON d.id=o.display_id WHERE o.player_id=%s AND o.output_id='HDMI-A-1'", (player,))
                return found or None
            [seen] = await until(judged, SECONDS, "the report judged into output_displays")
            assert (seen["connected"], seen["frame_id"]) == (True, "kitchen")
            recorded = rows("SELECT data FROM node_link_records WHERE device_id=%s AND stream=%s "
                            "AND subject LIKE '%%.output-HDMI-A-1'", (device_id_for_serial(SERIAL), REPORT_STREAM))
            assert [bytes(row["data"]) for row in recorded] == [_report("HDMI-A-1")]

            # (3) A report from an Output with no row: its document follows the judge's wake.
            display.state.put(output_report_key("HDMI-A-2"), _report("HDMI-A-2"))
            await until(lambda: held("HDMI-A-2"), 3, "output-HDMI-A-2 on the Node after its report")
            assert not running.done()
        finally:
            stop.set()
            await asyncio.wait_for(running, SECONDS)

    try:
        asyncio.run(run())
    finally:
        display.stop()
        node.stop()
        hub.stop()
