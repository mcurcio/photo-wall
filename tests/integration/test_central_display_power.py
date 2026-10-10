"""A console Test reaches the Node's desired bucket through PostgreSQL's notification (roadmap 1b,
slice C3).

A real hub on Fleet's generated configuration, a real Node bus, and the display line's session on
the shipped `DISPLAY_SLICE`. Central runs the worker's composition of the fleet pipe (`NodeLinks`
over `PgLinkStores`, documents from `DisplayDocuments`, woken by `DisplayWakes.listen`) and, apart
from it, the operator API in its own app. The POST commits in the API's process; the only path to
the worker is the notification. The link first settles (its reconcile after draining the
session's `birth`), and no reconcile runs between the POST and the test on top of standing on
reaching the Node within 3 s: the notification carried it.
"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from integration.bus_servers import hub_server, leaf_connections, node_server, until
from test_display_identity import ADMIN, AUTH, enroll_pi

from appliance.display_host.bus import DISPLAY_COMPONENT, DISPLAY_SLICE, DISPLAY_VERSION
from central.app import create_app
from central.content_catalog.catalog import device_id_for_serial
from central.displays.model import TEST_SECONDS
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
    Power,
    PowerRequest,
    RequestReason,
    decode_output_document,
    output_document_key,
)
from contracts.time import ManualClock
from nodeapi.hub import NodeLink
from nodeapi.node import NodeSession, Release

SERIAL = "serial-display-power"
SECONDS = 30
STANDING = PowerRequest("standing", Power.ON, RequestReason.STANDING)


def test_a_console_test_reaches_the_nodes_desired_bucket_by_notification(database, tmp_path, monkeypatch):
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
            document = await until(held, SECONDS, "output-HDMI-A-1 on the Node")
            assert document.power == (STANDING,)
            # Settled: the link's reconcile after it drained the session's birth is done, and the
            # listener listens (its connect wakes every Node), so only the notification is left.
            async def settled():
                return len(reconciles) >= 2
            await until(settled, SECONDS, "the link's reconcile after the session's birth")
            await asyncio.wait_for(wakes.after(device, before_listening), SECONDS)
            reconciled = len(reconciles)

            response = await asyncio.to_thread(client.post, "/v1/operator/frames/kitchen/power-tests",
                                               headers=AUTH, json={"power": "off"})
            assert response.status_code == 202
            test = PowerRequest(response.json()["request_id"], Power.OFF, RequestReason.CONSOLE_TEST, TEST_SECONDS)

            async def tested():
                found = await held()
                return found if found and found.power == (test, STANDING) else None
            carried = await until(tested, 3, "the test on top of standing on, by notification")
            assert carried.change == document.change + 1
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
