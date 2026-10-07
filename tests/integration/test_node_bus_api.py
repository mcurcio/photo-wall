"""The Node API tracer on real servers (E3b design §16.1): "start empty, attach, reconcile".

One component (`display`, fleet pipe) runs on a real `NodeSession` on a Node with the shipped
`node-bus.conf`; Central's fleet `NodeLink` reaches it across the leaf, through a path-prefix proxy
that can drop bytes and cut the link, from the hub on Fleet's generated configuration, and records
into a file-backed `LinkStore`. Step 1: the method answers, every event and state change is recorded
once and in order, a Node-local watch sees the latest state; Central's documents reach the session's
desired view and a wall value its wall view; a second writer's document is adopted, and Central's own
write whose acknowledgement it lost is taken back without one. Step 2: consumers deleted behind the
readers' backs, leaf drops mid-drain and a Central crash mid-commit lose nothing, repeat nothing and
invent no gap. Step 4: the hub restarts empty; WALL comes back past every mirror and the drain resumes.
Later slices extend the same test.
"""
from __future__ import annotations

import asyncio
import contextlib
import json

import nats.errors
import pytest
from integration.bus_servers import (
    BusServer,
    FileLinkStore,
    LinkStoreCrash,
    PrefixProxy,
    Projection,
    central,
    hub_server,
    leaf_connections,
    local,
    node_server,
    until,
    wall_writer,
)
from nats.js.api import AckPolicy, ConsumerConfig

from contracts.node_link import (
    CENTRAL_WRITER,
    NODE_DOMAIN,
    STORE_LINES,
    WALL_STREAM,
    Pipe,
    account_id,
)
from nodeapi.buffers import KeyTable, Slice, apply, desired_bucket, event_buffer, state_bucket
from nodeapi.documents import DocumentWriter
from nodeapi.hub import WALL_MARGIN, NodeLink, WallWriter
from nodeapi.node import MethodCall, NodeSession, Release
from nodeapi.pull import ConsumerLost, CursorReader, Pulled, Read, StartAt, pull

SERIAL = "serial-a"
LEAF_PREFIX = "photo-wall/bus"
KIB = 1024
RECORDS = "RECORD_display"
STATE = "KV_state_display"
DESIRED = "KV_desired_display"
TICK = "display.record.tick"
RELEASE = Release("1.0.0", "sha256:display-release", {"display": 1})
WALL_TABLE = KeyTable({"timing": 256})


def _display_slice() -> Slice:
    """The display line filled exactly: records take what state and desired leave (3 streams, 1.5 MiB)."""
    state = state_bucket("display", KeyTable({"mode": 256, "panel": 256}))
    desired = desired_bucket("display", KeyTable({"show": 256, "layout": 256}))
    records = event_buffer("display", "record", STORE_LINES["display"].max_bytes - state.max_bytes - desired.max_bytes)
    return Slice("display", (records, state, desired))


async def _relinked(hub: BusServer, previous: int | None) -> int:
    """The Node's leaf link at the hub, once it is not `previous`; its connection id."""
    async def check():
        return leaf_connections(hub).get(account_id(SERIAL)) not in (None, previous)
    await until(check, 15, "the Node's leaf links")
    return leaf_connections(hub)[account_id(SERIAL)]


async def _recorded(store: FileLinkStore, emitted: list[bytes], seconds: float = 30) -> None:
    """Every emitted tick is recorded once, in order, each with its own message id."""
    async def check():
        return len([row for row in store.records(RECORDS) if row["subject"] == TICK]) >= len(emitted)
    await until(check, seconds, f"{len(emitted)} ticks recorded")
    ticks = [row for row in store.records(RECORDS) if row["subject"] == TICK]
    assert [row["data"] for row in ticks] == emitted
    sequences = [row["seq"] for row in ticks]
    assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences)
    ids = [row["message_id"] for row in ticks]
    assert None not in ids and len(set(ids)) == len(ids)


async def _watched(watch: CursorReader, latest: dict[str, bytes], expected: dict[str, bytes]) -> None:
    """Read the watch until each expected key's latest value is `expected`'s."""
    async def check():
        batch = await watch.read(16, timeout=.5)
        for item in batch.items:
            assert isinstance(item, Read), item   # a watch never reports a gap
            latest[item.subject.rsplit(".", 1)[-1]] = item.data
        return all(latest.get(key) == value for key, value in expected.items())
    await until(check, 20, f"the local watch sees {expected}")


async def _desired(session: NodeSession, expected: dict[str, tuple[bytes, str]]) -> None:
    """The session's desired view holds each key's (value, writer)."""
    async def check():
        return all((document := session.desired.get(key)) is not None and (document.value, document.writer) == held
                   for key, held in expected.items())
    await until(check, 15, f"the desired view holds {expected}")


async def _walled(session: NodeSession, value: bytes, seconds: float = 15) -> None:
    async def check():
        return session.wall.get("timing") == value
    await until(check, seconds, f"the wall view holds {value!r}")


def test_the_node_api_tracer(tmp_path):
    hub = hub_server(tmp_path, [SERIAL])
    proxy = PrefixProxy(hub.websocket_port, LEAF_PREFIX)
    node = node_server(tmp_path, SERIAL, hub, prefix=LEAF_PREFIX, leaf_port=proxy.port)
    slice_ = _display_slice()
    callers: list[str] = []

    async def identify(call: MethodCall) -> bytes:
        callers.append(call.caller)
        return b"display:" + call.payload

    assert sum(config.max_bytes for config in slice_.buffers) == STORE_LINES["display"].max_bytes
    assert len(slice_.buffers) == STORE_LINES["display"].streams
    session = NodeSession("display", slice_, RELEASE, url=node.client_url, methods={"identify": identify},
                          reads_wall=True)
    emitted: list[bytes] = []

    def emit(count: int) -> None:
        for _ in range(count):
            body = f"tick-{len(emitted):05}".encode()
            session.events.emit("record.tick", body, schema_major=1)
            emitted.append(body)

    hub.start()

    async def run():
        await proxy.start()
        node.start()
        clients = []
        try:
            leaf = await _relinked(hub, None)
            session.state.put("mode", b"normal")   # held before attach, put at attach
            session.start()
            assert await asyncio.to_thread(session.wait_attached, 15)
            client = await central(hub, SERIAL)
            watcher = await local(node)
            clients += [client, watcher]
            store = FileLinkStore(tmp_path / "link.jsonl")
            projection = Projection()
            projection.streams[DESIRED] = {"show": b"show-1", "layout": b"layout-1"}
            stop = asyncio.Event()
            link = NodeLink(client, Pipe.FLEET, store, projection)
            running = asyncio.create_task(link.run(stop))

            # Step 1: the method answers across the leaf; events and state are recorded once, in order.
            async def answered():
                with contextlib.suppress(nats.errors.NoRespondersError, nats.errors.TimeoutError):
                    return await link.call("display", "identify", b"hello", timeout=1)
            assert await until(answered, 15, "the display method answers across the leaf") == b"display:hello"
            assert callers[-1] == CENTRAL_WRITER
            assert {"component": "display", "method": "identify", "outcome": "ok"} in store.actions("call")
            [service] = [info for info in await link.discover() if info["name"] == "display"]
            assert [endpoint["subject"] for endpoint in service["endpoints"]] == ["display.method.identify"]
            assert store.actions("discover")
            with pytest.raises(ValueError, match="call_outside_pipe"):
                await link.call("content", "identify", b"", timeout=1)
            emit(200)
            session.state.put("panel", b"lit")
            await _recorded(store, emitted)

            async def call_recorded():
                return [json.loads(row["data"]) for row in store.records(RECORDS)
                        if row["subject"] == "display.record.call"]
            assert {"method": "identify", "caller": CENTRAL_WRITER, "outcome": "ok"} in await until(
                call_recorded, 10, "the call event")

            async def state_recorded():
                held = {row["subject"].rsplit(".", 1)[-1]: row["data"] for row in store.records(STATE)}
                return held if held.get("panel") == b"lit" and "birth" in held else None
            held = await until(state_recorded, 10, "state and birth recorded")
            assert held["mode"] == b"normal"
            birth = json.loads(held["birth"])
            assert birth == {"component": "display", "version": "1.0.0", "pipe": "fleet",
                             "release_digest": RELEASE.digest, "schema_majors": {"display": 1},
                             "slice_digest": slice_.digest}
            watch = CursorReader(watcher, STATE, None, start=StartAt.LAST_PER_SUBJECT)
            latest: dict[str, bytes] = {}
            await _watched(watch, latest, {"mode": b"normal", "panel": b"lit", "birth": held["birth"]})

            # Central's two documents reach the session's desired view, written by Central.
            await _desired(session, {"show": (b"show-1", CENTRAL_WRITER), "layout": (b"layout-1", CENTRAL_WRITER)})
            seen: list[tuple[str, bytes, str | None]] = []
            session.desired.on_change(lambda key, document: seen.append((key, document.value, document.writer)))

            # WallWriter creates WALL past the margin and its value reaches the session's wall view.
            walls = await wall_writer(hub)
            clients.append(walls)
            wall = WallWriter(walls, WALL_TABLE, store)
            assert await wall.ensure() is True
            assert (await walls.jetstream().stream_info(WALL_STREAM)).config.first_seq == 1 + WALL_MARGIN
            store.hold_wall("timing", b"timing-1")
            await wall.put("timing", b"timing-1")
            await _walled(session, b"timing-1")

            # A second writer (a local UI) changes one document; Central's next assert of a changed
            # projection conflicts, reads, adopts the Node's value and flags it, and leaves it there.
            local_ui = await DocumentWriter.bind(watcher, DESIRED, writer="local-ui")
            await local_ui.put("show", b"show-local", expect=(await local_ui.read("show")).token)
            await _desired(session, {"show": (b"show-local", "local-ui")})
            assert ("show", b"show-local", "local-ui") in seen
            projection.streams[DESIRED]["show"] = b"show-2"
            await link.assert_documents(DESIRED)
            await link.assert_documents(DESIRED)
            assert store.actions("document_adopted") == [{"stream": DESIRED, "key": "show", "writer": "local-ui"}]
            assert (await local_ui.read("show")).value == b"show-local"

            # Central's write lands but its record does not (the acknowledgement lost): the next
            # assert conflicts on Central's stale token, finds its own value and takes its token,
            # with no adoption and no second write.
            projection.streams[DESIRED]["layout"] = b"layout-2"
            store.crash_next = "wrote"
            with pytest.raises(LinkStoreCrash):
                await link.assert_documents(DESIRED)
            jetstream = watcher.jetstream()
            written = (await jetstream.stream_info(DESIRED)).state.last_seq
            await link.assert_documents(DESIRED)
            assert (await jetstream.stream_info(DESIRED)).state.last_seq == written
            layout = await local_ui.read("layout")
            assert (layout.value, layout.writer) == (b"layout-2", CENTRAL_WRITER)
            assert (await store.own_tokens(DESIRED))["layout"][1] == layout.token
            assert len(store.actions("document_adopted")) == 1
            await _desired(session, {"layout": (b"layout-2", CENTRAL_WRITER)})

            # Step 2: both readers' consumers deleted behind their backs; both recover from their cursors.
            for stream in (RECORDS, STATE):
                consumers = await jetstream.consumers_info(stream)
                assert consumers, stream
                for consumer in consumers:
                    await jetstream.delete_consumer(stream, consumer.name)
            emit(50)
            session.state.put("mode", b"dim")
            await _watched(watch, latest, {"mode": b"dim"})
            await _recorded(store, emitted)

            # Deliveries a reader never saw, as a leaf drop loses them in transit (X13), made
            # deterministic: another client takes three from a local drain's consumer. The reader sees
            # its consumer sequence jump, recreates from its cursor and reports no gap for events the
            # stream still holds (erratum E-E3B-S1-1).
            drain = CursorReader(watcher, RECORDS, None)
            others = {consumer.name for consumer in await jetstream.consumers_info(RECORDS)}
            while (await drain.read(64, timeout=.2)).items:
                pass
            [own] = {consumer.name for consumer in await jetstream.consumers_info(RECORDS)} - others
            last = (await jetstream.stream_info(RECORDS)).state.last_seq
            emit(10)

            async def held():
                return (await jetstream.stream_info(RECORDS)).state.last_seq >= last + 10
            await until(held, 10, "ten more events in the stream")
            assert len((await pull(watcher, RECORDS, own, 3, timeout=1)).messages) == 3
            items, recreated = [], []

            async def read_up():
                batch = await drain.read(64, timeout=.2)
                items.extend(batch.items)
                recreated.append(batch.recreated)
                return drain.cursor.seq >= last + 10
            await until(read_up, 10, "the local drain reads the ten events")
            assert all(isinstance(item, Read) for item in items), items
            assert any(recreated) and [item.token.seq for item in items] == list(range(last + 1, last + 11))
            await drain.close()
            await _recorded(store, emitted)

            # The leaf drops bytes, then the link is cut, three times while the session emits.
            emitting = True

            async def keep_emitting():
                while emitting:
                    emit(2)
                    await asyncio.sleep(.02)
            emitter = asyncio.create_task(keep_emitting())
            for _ in range(3):
                await asyncio.sleep(.5)
                await proxy.drop(.3)
                proxy.sever()
                leaf = await _relinked(hub, leaf)
            await asyncio.sleep(.5)
            emitting = False
            await emitter
            await _recorded(store, emitted)
            assert store.rows("gap") == [], "a gap row for events the stream still holds"
            assert store.repeats == []

            # Central crashes mid-commit; a new NodeLink on the same store resumes from its cursors.
            store.crash_next = "commit"
            emit(50)
            with pytest.raises(LinkStoreCrash):
                await asyncio.wait_for(running, 30)
            store = FileLinkStore(store.path)
            running = asyncio.create_task(NodeLink(client, Pipe.FLEET, store, projection).run(stop))
            emit(20)
            await _recorded(store, emitted)
            assert store.rows("gap") == [] and store.repeats == []

            # Step 4: the hub restarts empty. A wall write whose mark Central lost (a crash between the
            # hub's ack and its record) leaves every mirror past Central's mark, and the wall value
            # changes while the hub is away.
            wall = WallWriter(walls, WALL_TABLE, store)   # Central's restarted process
            assert await wall.ensure() is False
            store.hold_wall("timing", b"timing-2")
            store.crash_next = "record_mark"
            with pytest.raises(LinkStoreCrash):
                await wall.put("timing", b"timing-2")
            await _walled(session, b"timing-2")
            mark = await store.mark()
            assert (await jetstream.stream_info(WALL_STREAM)).state.last_seq == mark + 1
            emit(30)
            hub.wipe()
            store.hold_wall("timing", b"timing-outage")
            emit(30)
            await asyncio.wait_for(running, 15)   # its client closed with the hub
            hub.start()
            leaf = await _relinked(hub, leaf)

            # Central reconnects (E3d's supervisors): WallWriter re-creates WALL at mark + 1 + K and
            # re-puts; every mirror and the wall view are current; the drain resumes from its cursors.
            client, walls = await central(hub, SERIAL), await wall_writer(hub)
            clients += [client, walls]
            running = asyncio.create_task(NodeLink(client, Pipe.FLEET, store, projection).run(stop))
            assert await WallWriter(walls, WALL_TABLE, store).ensure() is True
            assert (await walls.jetstream().stream_info(WALL_STREAM)).config.first_seq == mark + 1 + WALL_MARGIN
            # The mirror resumes on the server's own retry of its source: 12-19 s measured after
            # ensure() (erratum E-E3B-S2-1); 30 s as the wave-1 hub-loss test allowed.
            await _walled(session, b"timing-outage", 30)
            emit(20)
            await _recorded(store, emitted)
            assert store.rows("gap") == [] and store.repeats == []
            assert len(store.actions("document_adopted")) == 1
            stop.set()
            await asyncio.wait_for(running, 15)
            await watch.close()
        finally:
            session.stop()
            for client in clients:
                await client.close()
            node.stop()
            await proxy.close()

    try:
        asyncio.run(run())
    finally:
        session.stop()
        node.stop()
        hub.stop()


def test_a_pull_tells_a_live_idle_consumer_from_a_lost_one(tmp_path):
    # Any reply proves a consumer alive; a missing one answers nothing (X9, X10). Across the leaf, an
    # idle live consumer is live and empty every time; a deleted one is lost, never "empty".
    hub = hub_server(tmp_path, [SERIAL])
    node = node_server(tmp_path, SERIAL, hub)
    hub.start()
    node.start()

    async def run():
        await _relinked(hub, None)
        node_client = await local(node)
        client = await central(hub, SERIAL)
        jetstream = node_client.jetstream()
        await apply(jetstream, Slice("host", (event_buffer("host", "record", 64 * KIB),
                                              state_bucket("host", KeyTable({"uptime": 64})))))
        await jetstream.add_consumer("RECORD_host", ConsumerConfig(
            name="idle", ack_policy=AckPolicy.NONE, inactive_threshold=60))
        for _ in range(3):
            assert await pull(client, "RECORD_host", "idle", 8, timeout=1, domain=NODE_DOMAIN) == Pulled([], True)
        await jetstream.delete_consumer("RECORD_host", "idle")
        with pytest.raises(ConsumerLost):
            await pull(client, "RECORD_host", "idle", 8, timeout=1, domain=NODE_DOMAIN)
        await client.close()
        await node_client.close()

    try:
        asyncio.run(run())
    finally:
        node.stop()
        hub.stop()
