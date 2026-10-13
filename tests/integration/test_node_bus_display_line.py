"""The display line carries Central's first document (roadmap 1b, slice T1), on real servers.

A Node on the shipped `node-bus.conf` runs the host component's session and a display session on
the shipped `DISPLAY_SLICE`; the hub runs Fleet's generated configuration, and Central's fleet
`NodeLink` reaches the Node across the leaf, its documents from a `Projection` and its records into a
file-backed `LinkStore`. (1) The display line applies beside the host line inside the store's stream
count. (2) Central's Output document reaches the display session's desired view and decodes to the
same document. (3) A changed projection reaches the Node within 3 s through `DocumentSource.changed()`
alone: no birth, no new stream, no reconnect, so no reconcile carried it. (4) The session's Output
report is recorded by Central on the display line's state bucket. (5) A key the display table does
not list is never sent and is logged as `document_refused`. (6) Picture on its own keys (decision
0022): a picture document Central puts on `picture-HDMI-A-1` reaches the picture controller through
the runner's document feed, and the picture report the controller puts appears on Central's
`picture-HDMI-A-1`. (7) The print look ships together (picture operations v5): a picture document
with saturation, highlight rolloff, black floor and a LUT reaches the real picture controller, whose
`picture` op gives the shell v5 operations text that `decode_picture_operations` accepts.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

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

from appliance.display_host.bus import (
    DISPLAY_COMPONENT,
    DISPLAY_SLICE,
    DISPLAY_VERSION,
    BusPictureSink,
)
from appliance.display_host.picture import FileAppliedPictures, PictureController, operations_for
from appliance.display_host.picture_list import FileOfferedCache
from appliance.display_host.runner import document_feed
from appliance.host.bus import HOST_SLICE, host_session
from contracts.lut import LutRef
from contracts.node_frame import LutMix, decode_picture_operations, encode_picture_operations
from contracts.node_link import CENTRAL_WRITER, NODE_MAX_STREAMS, Pipe, account_id
from contracts.node_output import (
    BEST_DETECTED,
    SCHEMA_MAJOR,
    Darkness,
    DisplayIdentity,
    DisplayMode,
    OutputDocument,
    OutputPower,
    OutputReport,
    Power,
    PowerMethod,
    PowerRequest,
    decode_output_document,
    decode_output_report,
    encode_output_document,
    encode_output_report,
    output_document_key,
    output_report_key,
)
from contracts.node_picture import (
    HardwareRange,
    HardwareResult,
    HardwareSetting,
    PictureDocument,
    PictureReport,
    RangeKind,
    SoftwarePicture,
    WriteResult,
    decode_picture_report,
    encode_picture_document,
    encode_picture_report,
    picture_document_key,
    picture_report_key,
)
from nodeapi.hub import NodeLink
from nodeapi.node import NodeSession, Release

SERIAL = "serial-d"
DESIRED = "KV_desired_display"
STATE = "KV_state_display"
KEY = output_document_key("HDMI-A-1")
UNLISTED = "output-HDMI-A-3"
ALWAYS_ON = OutputPower(Power.ON, (), "2099-01-01T00:00:00Z", Darkness.DISPLAY_OFF, None)
FIRST = OutputDocument("HDMI-A-1", 1, ALWAYS_ON, BEST_DETECTED, True, True)
TEST_OFF = OutputDocument("HDMI-A-1", 2, replace(ALWAYS_ON, test=PowerRequest("req-1", Power.OFF, 300)),
                          BEST_DETECTED, True, True)
PICTURE_KEY = picture_document_key("HDMI-A-1")
PICTURE_DOCUMENT = PictureDocument("HDMI-A-1", 8, {"10": 46}, SoftwarePicture(114, 100, 6500, 22))
PICTURE_REPORT = PictureReport("HDMI-A-1", DisplayIdentity("XYM", 5475, "MNN", None),
                               (HardwareSetting("10", HardwareRange(RangeKind.LEVEL, 100, ()), 46),), 8,
                               {"10": HardwareResult(WriteResult.TAKEN, 60, 46)}, 8)
PAPER = LutRef("ab" * 32, "lut33-perceptual-relcol-bpc-1")
PRINT_LOOK = PictureDocument("HDMI-A-1", 9, {"10": 46}, SoftwarePicture(114, 100, 6500, 22, 85, 40, 20, PAPER, 80))
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


class TextShell:
    """The shell's `picture` op as Weston's backend sends it: the operations as their wire text."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def picture(self, operations, *, timeout, brightness):
        self.texts.append(encode_picture_operations(operations))
        return True


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
        pictures: list[tuple[str, PictureDocument]] = []
        sink = BusPictureSink(display)
        shell = TextShell()
        unused = SimpleNamespace()       # no display is connected: no DDC/CI, no ask
        picture = PictureController(unused, unused, FileOfferedCache(tmp_path), shell,
                                    SimpleNamespace(put_picture=lambda report: None),
                                    FileAppliedPictures(tmp_path), time.monotonic)
        picture.start()
        controller = SimpleNamespace(document=lambda output_id, document: (pictures.append((output_id, document)),
                                                                          sink.put_picture(PICTURE_REPORT),
                                                                          picture.document(output_id, document)))
        display.desired.on_change(document_feed(SimpleNamespace(document=lambda output_id, document: None),
                                                controller))

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

            # (6) Picture on its own keys: the document reaches the picture controller, its report Central.
            projection.set(DESIRED, PICTURE_KEY, encode_picture_document(PICTURE_DOCUMENT))

            async def fed():
                return pictures
            assert await until(fed, 15, "the picture document at the picture controller") == [
                ("HDMI-A-1", PICTURE_DOCUMENT)]

            async def pictured():
                return [row["data"] for row in store.records(STATE)
                        if row["subject"].endswith("." + picture_report_key("HDMI-A-1"))]
            [recorded] = await until(pictured, 15, "the picture report recorded")
            assert recorded == encode_picture_report(PICTURE_REPORT)
            assert decode_picture_report(recorded) == PICTURE_REPORT

            # (7) The print look ships together: v5 operations text at the shell's `picture` op.
            projection.set(DESIRED, PICTURE_KEY, encode_picture_document(PRINT_LOOK))

            async def drawn():
                return [operations for operations in map(decode_picture_operations, shell.texts)
                        if operations.picture_change == 9]
            [operations] = (await until(drawn, 15, "the print look's operations at the shell"))[:1]
            assert operations == operations_for("HDMI-A-1", 9, PRINT_LOOK.software, None)
            assert (operations.saturation, operations.lut) == (0.85, LutMix(PAPER, 0.8))
            assert operations.highlight_knee < 1 and operations.black_floor > 0
        finally:
            picture.stop()
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
