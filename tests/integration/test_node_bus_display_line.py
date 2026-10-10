"""The display line carries Central's first document (roadmap 1b, slice T1), on real servers.

A Node on the shipped `node-bus.conf` runs the host component's session and a display session on
the shipped `DISPLAY_SLICE`; the hub runs Fleet's generated configuration, and Central's fleet
`NodeLink` reaches the Node across the leaf, its documents from a `Projection` and its records into a
file-backed `LinkStore`. (1) The display line applies beside the host line inside the store's stream
count. (2) Central's Output document reaches the display session's desired view and decodes to the
same document. (3) A changed projection reaches the Node within 3 s through `DocumentSource.changed()`
alone: no birth, no new stream, no reconnect, so no reconcile carried it. (4) The session's Output
report is recorded by Central on the display line's state bucket. (5) A key the display table does
not list is never sent and is logged as `document_refused`.
"""
from __future__ import annotations

import asyncio

from integration.bus_servers import (
    FileLinkStore,
    Projection,
    central,
    hub_server,
    leaf_connections,
    local,
    node_server,
    until,
)
from nats.js.errors import NotFoundError

from appliance.display_host.bus import DISPLAY_COMPONENT, DISPLAY_SLICE, DISPLAY_VERSION
from appliance.host.bus import HOST_SLICE, host_session
from contracts.node_link import CENTRAL_WRITER, NODE_MAX_STREAMS, Pipe, account_id
from contracts.node_output import (
    BEST_DETECTED,
    SCHEMA_MAJOR,
    DisplayIdentity,
    DisplayMode,
    OutputDocument,
    OutputReport,
    Power,
    PowerMethod,
    PowerRequest,
    RequestReason,
    decode_output_document,
    decode_output_report,
    encode_output_document,
    encode_output_report,
    output_document_key,
    output_report_key,
)
from nodeapi.hub import NodeLink
from nodeapi.node import NodeSession, Release

SERIAL = "serial-d"
DESIRED = "KV_desired_display"
STATE = "KV_state_display"
KEY = output_document_key("HDMI-A-1")
UNLISTED = "output-HDMI-A-3"
STANDING = PowerRequest("standing", Power.ON, RequestReason.STANDING)
FIRST = OutputDocument("HDMI-A-1", 1, (STANDING,), BEST_DETECTED, True, True)
TEST_OFF = OutputDocument("HDMI-A-1", 2, (PowerRequest("req-1", Power.OFF, RequestReason.CONSOLE_TEST, 300), STANDING),
                          BEST_DETECTED, True, True)
REPORT = OutputReport("HDMI-A-1", True, DisplayIdentity("XYM", 5475, "MNN", None),
                      (DisplayMode(1920, 1080, 60000, preferred=True),), (PowerMethod.DDC_CI, PowerMethod.SIGNAL_OFF),
                      PowerMethod.DDC_CI, 2, None, None)


async def _names(node) -> frozenset[str]:
    client = await local(node)
    try:
        names = await client.jetstream().streams_info()
        return frozenset(info.config.name for info in names)
    finally:
        await client.close()


class CountingStore(FileLinkStore):
    """The file store, counting reconciles: NodeLink.reconcile reads `cursors()` once each time."""

    reconciles = 0

    async def cursors(self):
        self.reconciles += 1
        return await super().cursors()


async def _settled(store: CountingStore, seconds: float = 2.5) -> int:
    """The reconcile count once it has held still for `seconds` (longer than a drain's read and the
    link's stream-name look), so no reconcile is in flight."""
    async def still():
        count = store.reconciles
        await asyncio.sleep(seconds)
        return store.reconciles == count and count
    return await until(still, 20, "the link settles")


def _births(store: FileLinkStore) -> int:
    return sum(row["subject"].endswith(".birth") for stream in (STATE, "KV_state_host")
               for row in store.records(stream))


def test_the_display_line_carries_centrals_output_document(tmp_path):
    hub = hub_server(tmp_path, [SERIAL])
    node = node_server(tmp_path, SERIAL, hub)
    host = host_session("base-test", url=node.client_url)
    display = NodeSession(DISPLAY_COMPONENT, DISPLAY_SLICE,
                          Release(DISPLAY_VERSION, "sha256:display-test", {DISPLAY_COMPONENT: SCHEMA_MAJOR}),
                          url=node.client_url)
    hub.start()
    node.start()

    async def run():
        host.start()
        display.start()
        assert await asyncio.to_thread(host.wait_attached, 15)
        assert await asyncio.to_thread(display.wait_attached, 15)

        # (1) The display line applies beside the host line, inside the store's stream count.
        names = await _names(node)
        assert {config.name for config in (*HOST_SLICE.buffers, *DISPLAY_SLICE.buffers)} <= names
        assert len(names) <= NODE_MAX_STREAMS

        async def linked():
            return leaf_connections(hub).get(account_id(SERIAL))
        leaf = await until(linked, 15, "the Node's leaf links")
        client = await central(hub, SERIAL)
        store = CountingStore(tmp_path / "link.jsonl")
        projection = Projection()
        projection.streams[DESIRED] = {KEY: encode_output_document(FIRST)}
        stop = asyncio.Event()
        running = asyncio.create_task(NodeLink(client, Pipe.FLEET, store, projection).run(stop))
        try:
            # (2) Central asserts the Output document; the display session's view decodes the same one.
            async def held(document: OutputDocument):
                found = display.desired.get(KEY)
                return (found is not None and found.writer == CENTRAL_WRITER
                        and decode_output_document(found.value) == document)
            await until(lambda: held(FIRST), 15, "the first Output document in the desired view")

            async def born():
                return _births(store) >= 2
            await until(born, 15, "both births recorded")
            reconciles = await _settled(store)   # the births' reconciles are done
            births = _births(store)

            # (3) A projection change reaches the Node through changed() alone, within 3 s.
            projection.set(DESIRED, KEY, encode_output_document(TEST_OFF))
            await until(lambda: held(TEST_OFF), 3, "the changed Output document in the desired view")
            assert store.reconciles == reconciles
            assert await _names(node) == names
            assert leaf_connections(hub).get(account_id(SERIAL)) == leaf
            assert client.stats["reconnects"] == 0
            assert _births(store) == births

            # (4) The session's Output report is recorded on the display line's state bucket.
            encoded = encode_output_report(REPORT)
            display.state.put(output_report_key("HDMI-A-1"), encoded)

            async def reported():
                return [row["data"] for row in store.records(STATE) if row["subject"].endswith(".output-HDMI-A-1")]
            [recorded] = await until(reported, 15, "the Output report recorded")
            assert recorded == encoded and decode_output_report(recorded) == REPORT

            # (5) A key the display table does not list is refused before sending, and logged.
            projection.set(DESIRED, UNLISTED, encode_output_document(FIRST))

            async def refused():
                return [action for action in store.actions("document_refused") if action["key"] == UNLISTED]
            [action] = (await until(refused, 3, "the unlisted key refused"))[:1]
            assert action["stream"] == DESIRED
            watcher = await local(node)
            try:
                try:
                    await watcher.jetstream().get_last_msg(DESIRED, f"$KV.desired_display.{UNLISTED}")
                    raise AssertionError("an unlisted key reached the Node")
                except NotFoundError:
                    pass
            finally:
                await watcher.close()
            assert display.desired.get(UNLISTED) is None
        finally:
            stop.set()
            await asyncio.wait_for(running, 10)
            await client.close()

    try:
        asyncio.run(run())
    finally:
        display.stop()
        host.stop()
        node.stop()
        hub.stop()
