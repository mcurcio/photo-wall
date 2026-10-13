"""Picture crosses the display line both ways on its own keys (decision 0022, the picture split,
slice S2; roadmap 1c's Live model).

A real hub on Fleet's generated configuration, a real Node bus, and the display line's session on
the shipped `DISPLAY_SLICE`, as tests/integration/test_central_display_power.py runs them. (1)
Central writes `picture-HDMI-A-1` on the Node's desired bucket beside `output-HDMI-A-1`. (2) The
Node's picture report on `picture-HDMI-A-1` is recorded (`picture_reports`), so the Picture tab
knows what the display offers. (3) A hardware edit made through the operator API, in its own
process, reaches the Node's `picture-HDMI-A-1` with a new picture change within 3 s while
`output-HDMI-A-1` keeps its change: carried by the notification (no reconcile runs between the edit
and its arrival). Mutation probe: drop the session write's NOTIFY -> the edit arrives only at the
session's idle deadline, past 3 s. (Print look) the document Central writes decodes with the print
look's fields (the Display's neutral ones); the v4 software shape would not decode
(contracts.node_picture's strict decoder). (Display tuning, decision 0024) the display line is
schema major 6: (4) the session's signal range and test pattern reach the Node's document, and the
Node's signal answer and the pattern it shows come back to the Picture tab.
"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from integration.bus_servers import hub_server, leaf_connections, node_server, until
from test_display_identity import ADMIN, AUTH, MODE, NO_SERIAL, enroll_pi

from appliance.display_host.bus import DISPLAY_COMPONENT, DISPLAY_SLICE, DISPLAY_VERSION
from central.app import create_app
from central.content_catalog.catalog import device_id_for_serial
from central.db import DatabaseTransactionClock
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
    OutputReport,
    decode_output_document,
    encode_output_report,
    output_document_key,
    output_report_key,
)
from contracts.node_picture import (
    NEUTRAL_SOFTWARE,
    HardwareRange,
    HardwareSetting,
    PictureReport,
    RangeKind,
    SignalRange,
    SignalReport,
    SignalResult,
    TestPatternAsk,
    decode_picture_document,
    encode_picture_report,
    picture_document_key,
    picture_report_key,
)
from contracts.time import ManualClock
from contracts.tuning import TestPattern
from nodeapi.hub import NodeLink
from nodeapi.node import NodeSession, Release

SERIAL = "serial-display-picture"
SECONDS = 30


def _report() -> bytes:
    """The Pi's Output report on HDMI-A-1: the test monitor connected."""
    return encode_output_report(OutputReport("HDMI-A-1", True, NO_SERIAL, MODE, (), None, None, None, None))


def _picture_report(signal: SignalReport | None = None, shown: TestPatternAsk | None = None) -> bytes:
    """The Pi's picture report on HDMI-A-1: the monitor offers brightness as a level of 100, read at 40;
    with `signal`, it answers that change (presented), showing `shown`."""
    change = None if signal is None else signal.for_change
    return encode_picture_report(PictureReport(
        "HDMI-A-1", NO_SERIAL, (HardwareSetting("10", HardwareRange(RangeKind.LEVEL, 100, ()), 40),), change, {},
        change, signal, shown))


def test_picture_documents_and_reports_cross_the_display_line_on_their_own_keys(database, tmp_path, monkeypatch):
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

    def offered() -> bool:
        with database.transaction() as conn:
            return conn.execute("SELECT 1 FROM picture_reports WHERE player_id=%s AND output_id='HDMI-A-1' "
                                "AND report->'offered' IS NOT NULL AND report->'offered' <> 'null'::jsonb",
                                (player,)).fetchone() is not None

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
        links = NodeLinks(Pipe.FLEET, hub.client_url, stores, DisplayDocuments(stores, wakes, times=DatabaseTransactionClock()))
        stop = asyncio.Event()
        running = asyncio.create_task(links.run(stop))
        try:
            await links.track([SERIAL])

            async def held():
                found = display.desired.get(output_document_key("HDMI-A-1"))
                return (found is not None and found.writer == CENTRAL_WRITER
                        and decode_output_document(found.value))

            async def held_picture():
                found = display.desired.get(picture_document_key("HDMI-A-1"))
                return (found is not None and found.writer == CENTRAL_WRITER
                        and decode_picture_document(found.value))
            power = await until(held, SECONDS, "output-HDMI-A-1 on the Node")
            first = await until(held_picture, SECONDS, "(1) picture-HDMI-A-1 on the Node")
            assert (dict(first.hardware), first.software.brightness_percent) == ({}, 100)
            assert SCHEMA_MAJOR == 6 and first.software == NEUTRAL_SOFTWARE   # the print look's fields, neutral
            assert (first.signal_range, first.test_pattern) == (SignalRange.AUTO, None)

            # (2) The Output report records the display; the picture report, what it offers.
            display.state.put(output_report_key("HDMI-A-1"), _report())
            display.state.put(picture_report_key("HDMI-A-1"), _picture_report())

            async def recorded():
                return offered()
            await until(recorded, SECONDS, "(2) the picture report recorded on its Output")
            picture = await asyncio.to_thread(client.get, "/v1/operator/frames/kitchen/picture", headers=AUTH)
            assert picture.json()["offered_known"] is True, picture.text

            async def settled():
                return len(reconciles) >= 2
            await until(settled, SECONDS, "the link's reconcile after the session's birth")
            await asyncio.wait_for(wakes.after(device, before_listening), SECONDS)
            await asyncio.sleep(1.5)   # the judge's wake and any reconcile it started have run

            # (3) An edit in the operator API's process reaches the Node by notification. The session
            # begins first and its own wake settles, so only the edit's notification is left (the
            # session's idle deadline, 5 s after its begin, is past the 3 s this allows).
            begun = await asyncio.to_thread(client.post, "/v1/operator/frames/kitchen/picture-sessions",
                                            headers=AUTH)
            assert begun.status_code == 201, begun.text
            await asyncio.sleep(1.0)
            reconciled = len(reconciles)
            settled_picture = await held_picture()
            edit = await asyncio.to_thread(
                client.post, f"/v1/operator/picture-sessions/{begun.json()['session_id']}/hardware-edits",
                headers=AUTH, json={"code": "10", "value": 46, "expected_sequence": 1})
            assert edit.status_code == 200, edit.text

            async def edited():
                found = await held_picture()
                return found if found and dict(found.hardware) == {"10": 46} else None
            carried = await until(edited, 3, "(3) the hardware edit on the Node, by notification")
            assert carried.picture_change == settled_picture.picture_change + 1
            assert (await held()).change == power.change   # picture never costs power
            assert len(reconciles) == reconciled   # no reconcile carried it

            # (4) The signal range and the test pattern cross the line both ways.
            session = begun.json()["session_id"]
            ranged = await asyncio.to_thread(
                client.post, f"/v1/operator/picture-sessions/{session}/signal-range-edits", headers=AUTH,
                json={"signal_range": "full", "expected_sequence": 2})
            assert ranged.status_code == 200, ranged.text
            asked = await asyncio.to_thread(
                client.post, f"/v1/operator/picture-sessions/{session}/test-pattern-edits", headers=AUTH,
                json={"test_pattern": "white", "expected_sequence": 3})
            assert asked.json() == {"sequence": 4}, asked.text

            async def tuned():
                found = await held_picture()
                return found if found and found.test_pattern is not None else None
            tuning = await until(tuned, 3, "(4) the signal range and test pattern on the Node")
            assert (tuning.signal_range, tuning.test_pattern) == (SignalRange.FULL, TestPatternAsk(TestPattern.WHITE, 4))
            display.state.put(picture_report_key("HDMI-A-1"), _picture_report(
                SignalReport(tuning.picture_change, SignalRange.FULL, SignalResult.APPLIED), tuning.test_pattern))
            kept = await asyncio.to_thread(client.post, f"/v1/operator/picture-sessions/{session}/keepalive",
                                           headers=AUTH)       # the pattern is the live session's
            assert kept.status_code == 200, kept.text

            async def answered():
                view = (await asyncio.to_thread(client.get, "/v1/operator/frames/kitchen/picture",
                                                headers=AUTH)).json()
                return view if view["signal"] is not None else None
            view = await until(answered, 3, "(4) the Node's signal answer on the Picture tab")
            assert view["signal"] == {"in_force": "full", "result": "applied", "answered": True}
            assert view["test_pattern"] == {"pattern": "white", "state": "shown"}
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
