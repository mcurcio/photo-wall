"""The Node API tracer on real servers (E3b design §16.1): "start empty, attach, reconcile".

One component (`display`, fleet pipe) runs on a real `NodeSession` on a Node with the shipped
`node-bus.conf`; Central's fleet `NodeLink` reaches it across the leaf, through a path-prefix proxy
that can drop bytes and cut the link, from the hub on Fleet's generated configuration, and records
into a file-backed `LinkStore`. Steps 1 and 2 are slice S1's: the method answers, every event and
state change is recorded once and in order, a Node-local watch sees the latest state; consumers
deleted behind the readers' backs, leaf drops mid-drain and a Central crash mid-commit lose nothing,
repeat nothing and invent no gap. Later slices extend the same test.
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
    central,
    hub_server,
    leaf_connections,
    local,
    node_server,
    until,
)
from nats.js.api import AckPolicy, ConsumerConfig

from contracts.node_link import CENTRAL_WRITER, NODE_DOMAIN, Pipe, account_id
from nodeapi.buffers import KeyTable, Slice, apply, event_buffer, state_bucket
from nodeapi.hub import NodeLink
from nodeapi.node import MethodCall, NodeSession, Release
from nodeapi.pull import ConsumerLost, CursorReader, Pulled, Read, StartAt, pull

SERIAL = "serial-a"
LEAF_PREFIX = "photo-wall/bus"
KIB = 1024
RECORDS = "RECORD_display"
STATE = "KV_state_display"
TICK = "display.record.tick"
RELEASE = Release("1.0.0", "sha256:display-release", {"display": 1})


def _display_slice() -> Slice:
    return Slice("display", (event_buffer("display", "record", 512 * KIB),
                             state_bucket("display", KeyTable({"mode": 256, "panel": 256}))))


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


def test_the_node_api_tracer(tmp_path):
    hub = hub_server(tmp_path, [SERIAL])
    proxy = PrefixProxy(hub.websocket_port, LEAF_PREFIX)
    node = node_server(tmp_path, SERIAL, hub, prefix=LEAF_PREFIX, leaf_port=proxy.port)
    slice_ = _display_slice()
    callers: list[str] = []

    async def identify(call: MethodCall) -> bytes:
        callers.append(call.caller)
        return b"display:" + call.payload

    session = NodeSession("display", slice_, RELEASE, url=node.client_url, methods={"identify": identify})
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
            stop = asyncio.Event()
            link = NodeLink(client, Pipe.FLEET, store)
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

            # Step 2: both readers' consumers deleted behind their backs; both recover from their cursors.
            jetstream = watcher.jetstream()
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
            store.crash_next_commit = True
            emit(50)
            with pytest.raises(LinkStoreCrash):
                await asyncio.wait_for(running, 30)
            restarted = FileLinkStore(store.path)
            running = asyncio.create_task(NodeLink(client, Pipe.FLEET, restarted).run(stop))
            emit(20)
            await _recorded(restarted, emitted)
            assert restarted.rows("gap") == [] and restarted.repeats == []
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
