"""Central's Output documents and the Output report judge on real servers (roadmap 1b, slice C1).

A real hub on Fleet's generated configuration, a real Node bus, and the display line's session on
the shipped `DISPLAY_SLICE`. Central runs the worker's own composition of the fleet pipe:
`NodeLinks` over `PgLinkStores` with the `OutputReportJudge`, its documents from
`DisplayDocuments`. (1) The Node's desired bucket receives `output-HDMI-A-1` (its enrolled Output)
with standing on. (2) The session's Output report is recorded and judged into `output_displays`
(the Display recognised on the bound Frame, holding the size the report states). (3) A report from an Output Central had no row for
gives that Output a document within 3 s, carried by the judge's wake (`changed()`), not by a
reconcile: the link first settles (its reconcile after draining the session's `birth`), and no
reconcile runs between the report and the document (E-1B-C3-3). (4) Turning the bound Frame
(`Registry.turn_frame`, render redesign S2) puts its Output's document with the turn and the next
`change` on the Node within 3 s, carried by the command's wake, with no reconcile between. (5)
Unbinding the Frame brings that Output's document back to turn 0 within 3 s, likewise (the unbind's
wake). Mutation probes: drop the judge's wake (step 3), or the `notify_nodes_in` call in
`turn_frame` (step 4): the document then waits for a reconcile, or the 3 s pass.
"""
from __future__ import annotations

import asyncio

from integration.bus_servers import hub_server, leaf_connections, node_server, until
from support.frames import frame_create
from test_display_identity import enroll_pi

from appliance.display_host.bus import DISPLAY_COMPONENT, DISPLAY_SLICE, DISPLAY_VERSION
from central.content_catalog.catalog import device_id_for_serial
from central.db import DatabaseTransactionClock
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
from central.registry import Registry
from contracts.node_link import CENTRAL_WRITER, Pipe, account_id
from contracts.node_output import (
    BEST_DETECTED,
    SCHEMA_MAJOR,
    DisplayIdentity,
    DisplayMode,
    OutputReport,
    Power,
    decode_output_document,
    encode_output_report,
    output_document_key,
    output_report_key,
)
from contracts.time import ManualClock
from nodeapi.hub import NodeLink
from nodeapi.node import NodeSession, Release

SERIAL = "serial-display-documents"
SECONDS = 30
IDENTITY = DisplayIdentity("XYM", 5475, "MNN", None)
MODES = (DisplayMode(1920, 1080, 60000, preferred=True),)


_LISTEN_PROBE = "no-such-device"   # only a wake of all moves its count


def _report(output_id: str) -> bytes:
    return encode_output_report(OutputReport(output_id, True, IDENTITY, MODES, (), None, None, None, None,
                                             (950, 540)))


def test_a_nodes_output_documents_and_reports_cross_the_display_line(database, tmp_path, monkeypatch):
    reconciles: list[float] = []
    reconcile = NodeLink.reconcile

    async def counted(self):
        epochs = await reconcile(self)
        reconciles.append(asyncio.get_running_loop().time())
        return epochs
    monkeypatch.setattr(NodeLink, "reconcile", counted)
    registry = Registry(database, ManualClock(1000), lighting=PgFrameLighting(LUT_RECIPE_ID))
    player = enroll_pi(registry, SERIAL)
    with database.transaction() as conn:   # one enrolled Output only: HDMI-A-2 has no `outputs` row
        conn.execute("DELETE FROM outputs WHERE player_id=%s AND output_id='HDMI-A-2'", (player,))
    registry.create_frame(frame_create("kitchen"))
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
        # Registry writes (turn_frame, unbind) wake the source by NOTIFY, as the worker hears it:
        # listen here too. A connect wakes all, so a key nobody holds counts the connect alone.
        connects = wakes.count(_LISTEN_PROBE)
        listening = asyncio.create_task(wakes.listen(database.dsn))
        stores = PgLinkStores(database, {REPORT_STREAM: OutputReportJudge(wakes)})
        links = NodeLinks(Pipe.FLEET, hub.client_url, stores, 
                         DisplayDocuments(stores, wakes, times=DatabaseTransactionClock()))
        stop = asyncio.Event()
        running = asyncio.create_task(links.run(stop))
        try:
            await links.track([SERIAL])

            # (1) The enrolled Output's document, its timetable on, reaches the Node's desired view.
            async def held(output_id: str):
                found = display.desired.get(output_document_key(output_id))
                return (found is not None and found.writer == CENTRAL_WRITER
                        and decode_output_document(found.value))
            document = await until(lambda: held("HDMI-A-1"), SECONDS, "output-HDMI-A-1 on the Node")
            assert (document.change, document.power.now, document.power.test, document.method) == (
                1, Power.ON, None, BEST_DETECTED)
            assert display.desired.get(output_document_key("HDMI-A-2")) is None

            # (2) The Node's report is recorded and judged: the Display on the bound Frame.
            display.state.put(output_report_key("HDMI-A-1"), _report("HDMI-A-1"))

            async def judged():
                found = rows("SELECT o.connected, d.frame_id, d.width_mm, d.height_mm FROM output_displays o "
                             "JOIN displays d "
                             "ON d.id=o.display_id WHERE o.player_id=%s AND o.output_id='HDMI-A-1'", (player,))
                return found or None
            [seen] = await until(judged, SECONDS, "the report judged into output_displays")
            assert (seen["connected"], seen["frame_id"]) == (True, "kitchen")
            assert (seen["width_mm"], seen["height_mm"]) == (950, 540)   # the Display's size, from the bus
            recorded = rows("SELECT data FROM node_link_records WHERE device_id=%s AND stream=%s "
                            "AND subject LIKE '%%.output-HDMI-A-1'", (device_id_for_serial(SERIAL), REPORT_STREAM))
            assert [bytes(row["data"]) for row in recorded] == [_report("HDMI-A-1")]

            # (3) A report from an Output with no row: its document follows the judge's wake, once
            # the link settled (its reconcile after draining the session's birth), so no reconcile
            # can carry it.
            async def settled():
                return len(reconciles) >= 2
            await until(settled, SECONDS, "the link's reconcile after the session's birth")
            reconciled = len(reconciles)
            display.state.put(output_report_key("HDMI-A-2"), _report("HDMI-A-2"))
            await until(lambda: held("HDMI-A-2"), 3, "output-HDMI-A-2 on the Node after its report")
            assert len(reconciles) == reconciled   # the judge's wake carried it, not a reconcile

            # (4) A turn reaches the Node through turn_frame's wake, not a reconcile.
            await asyncio.wait_for(wakes.after(_LISTEN_PROBE, connects), SECONDS)   # LISTEN is on
            before = await held("HDMI-A-1")
            assert (before.change, before.turn) == (1, 0)
            [kitchen] = [frame for frame in registry.inventory().frames if frame.id == "kitchen"]
            turned = registry.turn_frame("kitchen", 90, expected_generation=kitchen.generation)
            assert turned["changed"] is True

            async def hanging(turn: int):
                document = await held("HDMI-A-1")
                return document if document and document.turn == turn else None
            document = await until(lambda: hanging(90), 3, "output-HDMI-A-1 turned 90 on the Node")
            assert document.change == 2
            assert len(reconciles) == reconciled   # turn_frame's wake carried it, not a reconcile

            # (5) Unbinding the Frame leaves its Output unturned, through the unbind's wake.
            registry.unbind("kitchen", expected_generation=turned["generation"])
            document = await until(lambda: hanging(0), 3, "output-HDMI-A-1 back to turn 0 on the Node")
            assert document.change == 3
            assert len(reconciles) == reconciled   # the unbind's wake carried it, not a reconcile
            assert not running.done()
        finally:
            stop.set()
            listening.cancel()
            await asyncio.wait_for(running, SECONDS)

    try:
        asyncio.run(run())
    finally:
        display.stop()
        node.stop()
        hub.stop()
