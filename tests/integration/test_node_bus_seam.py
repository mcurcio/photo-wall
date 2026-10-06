"""The Node bus seam on real servers (E3a): the WebSocket leaf, a method across it, same-domain
isolation, the wall-wide mirror (E3a-1); a conditional write into a Node bucket, Central's durable
read with ack after commit and a counted gap, hub reload and hub store loss (E3a-2). Every stream,
bucket and mirror is a buffer (E-W1-BUF-1): full, it drops its oldest and takes the write.

The hub runs Fleet's generated configuration, each Node the shipped `node-bus.conf`; raw nats-py
clients play Central and Node components. Every service, stream and subject here is the test's.
"""
from __future__ import annotations

import asyncio
import json
import time

import nats.errors
import nats.micro
import pytest
from integration.bus_servers import (
    BusServer,
    Recorder,
    bucket,
    buffer,
    central,
    central_put,
    declare_bucket,
    declare_wall,
    declare_wall_mirror,
    hub_server,
    leaf_connections,
    local,
    node_server,
    reload_hub,
    wall_value,
    wall_writer,
)
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy, DiscardPolicy, RetentionPolicy
from nats.js.errors import APIError, NoStreamResponseError, NotFoundError

from contracts.node_link import (
    NODE_DOMAIN,
    WALL_MESSAGE_BYTES,
    WALL_STREAM,
    WALL_STREAM_BYTES,
    account_id,
)

SERVICE = "probe"
ENDPOINT = "probe.echo"


def _leaf_accounts(hub: BusServer) -> list[str]:
    return list(leaf_connections(hub))


async def _until(check, seconds: float, what: str):
    deadline = time.monotonic() + seconds
    while True:
        value = await check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"not within {seconds}s: {what}")
        await asyncio.sleep(.05)


async def _linked(hub: BusServer, count: int) -> None:
    async def check():
        return len(_leaf_accounts(hub)) == count
    await _until(check, 10, f"{count} leaf links at the hub")


async def _gather(client, subject: str, seconds: float = .5) -> list[dict]:
    """Every reply to one request within `seconds` ($SRV discovery answers once per instance)."""
    inbox = client.new_inbox()
    subscription = await client.subscribe(inbox)
    await client.publish(subject, b"", reply=inbox)
    replies = []
    deadline = time.monotonic() + seconds
    while (left := deadline - time.monotonic()) > 0:
        try:
            message = await subscription.next_msg(timeout=left)
        except nats.errors.TimeoutError:
            break
        replies.append(json.loads(message.data))
    await subscription.unsubscribe()
    return replies


async def _serve(node_client, reply: bytes):
    async def echo(request):
        await request.respond(reply + b":" + request.data)
    service = await nats.micro.add_service(node_client, name=SERVICE, version="0.1.0")
    await service.add_endpoint(name="echo", subject=ENDPOINT, handler=echo)
    return service


async def _await_interest(client, subject: str) -> None:
    """Interest crosses the leaf asynchronously: wait until a request is answered."""
    async def check():
        try:
            return await client.request(subject, b"ready", timeout=.5)
        except (nats.errors.NoRespondersError, nats.errors.TimeoutError):
            return None
    await _until(check, 10, f"a responder on {subject}")


def test_central_calls_a_node_service_across_the_websocket_leaf(tmp_path):
    hub = hub_server(tmp_path, ["serial-a"])
    node = node_server(tmp_path, "serial-a", hub, prefix="photo-wall/bus")
    assert node.environment["PHOTO_WALL_BUS_LEAF_URL"].startswith("ws://node-")
    hub.start()
    node.start()

    async def run():
        await _linked(hub, 1)
        assert _leaf_accounts(hub) == [account_id("serial-a")]
        node_client = await local(node)
        central_client = await central(hub, "serial-a")
        service = await _serve(node_client, b"node-a")
        await _await_interest(central_client, ENDPOINT)

        pings = await _gather(central_client, f"$SRV.PING.{SERVICE}")
        assert [ping["id"] for ping in pings] == [service.id]
        infos = await _gather(central_client, f"$SRV.INFO.{SERVICE}")
        assert [(info["id"], info["endpoints"][0]["subject"]) for info in infos] == [(service.id, ENDPOINT)]
        reply = await central_client.request(ENDPOINT, b"hello", timeout=2)
        assert reply.data == b"node-a:hello"

        # The Node's component stays connected: only the server stopping takes the method away.
        node.stop()
        # The hub drops the Node's interest with its leaf; a request then fails fast.
        async def no_responders():
            started = time.monotonic()
            try:
                await central_client.request(ENDPOINT, b"absent", timeout=3)
            except nats.errors.NoRespondersError:
                return time.monotonic() - started
            return None
        assert await _until(no_responders, 5, "no responders once the Node stopped") < 1
        await node_client.close()
        await central_client.close()

    try:
        asyncio.run(run())
    finally:
        node.stop()
        hub.stop()


def test_two_node_accounts_in_domain_node_stay_isolated(tmp_path):
    hub = hub_server(tmp_path, ["serial-a", "serial-b"])
    node_a = node_server(tmp_path, "serial-a", hub)
    node_b = node_server(tmp_path, "serial-b", hub)
    for server in (hub, node_a, node_b):
        server.start()

    async def run():
        await _linked(hub, 2)
        local_a, local_b = await local(node_a), await local(node_b)
        central_a, central_b = await central(hub, "serial-a"), await central(hub, "serial-b")
        service_a = await _serve(local_a, b"node-a")
        service_b = await _serve(local_b, b"node-b")
        await _await_interest(central_a, ENDPOINT)
        await _await_interest(central_b, ENDPOINT)

        assert [ping["id"] for ping in await _gather(central_a, f"$SRV.PING.{SERVICE}")] == [service_a.id]
        assert [ping["id"] for ping in await _gather(central_b, f"$SRV.PING.{SERVICE}")] == [service_b.id]
        assert (await central_a.request(ENDPOINT, b"x", timeout=2)).data == b"node-a:x"
        assert (await central_b.request(ENDPOINT, b"x", timeout=2)).data == b"node-b:x"

        # The same stream name on both Nodes, both in domain `node`: each Central sees its own.
        for client, count in ((local_a, 3), (local_b, 5)):
            jetstream = client.jetstream()
            await jetstream.add_stream(buffer("PROBE", 64 * 1024, subjects=["probe.events.>"]))
            for index in range(count):
                await jetstream.publish(f"probe.events.{index}", b"event")
        info_a = await central_a.jetstream(domain=NODE_DOMAIN).stream_info("PROBE")
        info_b = await central_b.jetstream(domain=NODE_DOMAIN).stream_info("PROBE")
        assert (info_a.state.messages, info_b.state.messages) == (3, 5)

        # A Node A publish reaches its own Central, and nothing on Node B or as Central B.
        heard = {"central-a": [], "central-b": [], "node-b": []}
        for name, client in (("central-a", central_a), ("central-b", central_b), ("node-b", local_b)):
            async def record(message, name=name):
                heard[name].append(message.data)
            await client.subscribe("probe.isolation", cb=record)
        for client in (central_a, central_b, local_b):
            await client.flush()

        async def central_a_heard():
            await local_a.publish("probe.isolation", b"from-a")
            await local_a.flush()
            await asyncio.sleep(.1)
            return heard["central-a"]
        await _until(central_a_heard, 10, "Central A hears Node A")
        await asyncio.sleep(.5)
        assert heard["central-b"] == [] and heard["node-b"] == []
        assert sorted(_leaf_accounts(hub)) == sorted([account_id("serial-a"), account_id("serial-b")])

        for client in (local_a, local_b, central_a, central_b):
            await client.close()

    try:
        asyncio.run(run())
    finally:
        for server in (node_b, node_a, hub):
            server.stop()


def test_one_wall_write_reaches_every_node_mirror_and_the_mirror_is_read_only(tmp_path):
    hub = hub_server(tmp_path, ["serial-a", "serial-b"])
    node_a = node_server(tmp_path, "serial-a", hub)
    node_b = node_server(tmp_path, "serial-b", hub)
    for server in (hub, node_a, node_b):
        server.start()

    async def holds(node: BusServer, subject: str, value: bytes) -> None:
        client = await local(node)
        try:
            async def check():
                return await wall_value(client, subject) == value
            await _until(check, 10, f"{node.name} mirror holds {value!r} on {subject}")
        finally:
            await client.close()

    async def run():
        await _linked(hub, 2)
        writer = await wall_writer(hub)
        await declare_wall(writer)
        for node in (node_a, node_b):
            client = await local(node)
            await declare_wall_mirror(client)
            await client.close()

        # One Central publish reaches both Nodes' mirrors.
        await writer.jetstream().publish("wall.timing", b"first")
        await holds(node_a, "wall.timing", b"first")
        await holds(node_b, "wall.timing", b"first")

        # Node B misses the second publish while down; its kept store catches up on restart.
        node_b.stop()
        await writer.jetstream().publish("wall.timing", b"second")
        await holds(node_a, "wall.timing", b"second")
        node_b.start()
        await holds(node_b, "wall.timing", b"second")

        # A local publish on a wall subject changes neither the Node's mirror nor the hub stream.
        hub_before = (await writer.jetstream().stream_info(WALL_STREAM)).state
        local_a = await local(node_a)
        mirror_before = (await local_a.jetstream().stream_info(WALL_STREAM)).state
        await local_a.publish("wall.timing", b"forged")
        await local_a.publish("wall.other", b"forged")
        await local_a.flush()
        await asyncio.sleep(1)
        mirror_after = (await local_a.jetstream().stream_info(WALL_STREAM)).state
        assert (mirror_after.messages, mirror_after.last_seq) == (mirror_before.messages, mirror_before.last_seq)
        assert await wall_value(local_a, "wall.timing") == b"second"
        assert await wall_value(local_a, "wall.other") is None
        hub_after = (await writer.jetstream().stream_info(WALL_STREAM)).state
        assert (hub_after.messages, hub_after.last_seq) == (hub_before.messages, hub_before.last_seq)

        # Latest per subject (API6): a subject written once stays in every mirror while another is
        # rewritten past the mirror's byte cap. The writes are paced so each mirror stores them all
        # (a mirror that lags skips what the hub already replaced) (E-W1-E3a-R-1).
        local_b = await local(node_b)
        await writer.jetstream().publish("wall.scene", b"scene-the-only-value")
        value, rewrites = b"t" * 1024, WALL_STREAM_BYTES // 1024 + 64
        for index in range(rewrites):
            acknowledgement = await writer.jetstream().publish("wall.timing", value)
            if index % 16 == 15 or index == rewrites - 1:
                for client in (local_a, local_b):
                    async def caught_up(client=client, seq=acknowledgement.seq):
                        return (await client.jetstream().stream_info(WALL_STREAM)).state.last_seq >= seq
                    await _until(caught_up, 10, f"mirror reaches {acknowledgement.seq}")
        for client in (local_a, local_b):
            assert await wall_value(client, "wall.scene") == b"scene-the-only-value"
            assert await wall_value(client, "wall.timing") == value
            assert (await client.jetstream().stream_info(WALL_STREAM)).state.messages == 2
        await local_b.close()
        await local_a.close()
        await writer.close()

    try:
        asyncio.run(run())
    finally:
        for server in (node_b, node_a, hub):
            server.stop()


def test_a_full_wall_takes_every_write_and_drops_its_oldest_subject(tmp_path):
    """WALL is a buffer (E-W1-BUF-1): full, it takes a new subject and the largest message, each
    dropping the oldest subject's value; the WALL account's store, its cap plus one largest message,
    never refuses first. Only a message past the largest is refused (a message limit, not fullness)."""
    hub = hub_server(tmp_path, [])
    hub.start()

    async def run():
        writer = await wall_writer(hub)
        jetstream = writer.jetstream()
        await declare_wall(writer)
        value, index = b"v" * 1000, 0
        while (full := (await jetstream.stream_info(WALL_STREAM)).state).first_seq == 1:
            acknowledgement = await jetstream.publish(f"wall.s{index:04}", value)
            assert acknowledgement.seq == index + 1
            index += 1
        # Full: the first subject's only value is gone, a hole below first_seq a reader sees.
        assert index > 1 and full.bytes <= WALL_STREAM_BYTES
        with pytest.raises(NotFoundError):
            await jetstream.get_last_msg(WALL_STREAM, "wall.s0000")
        acknowledgement = await jetstream.publish("wall.largest", b"L" * WALL_MESSAGE_BYTES)
        assert acknowledgement.seq == full.last_seq + 1
        after = (await jetstream.stream_info(WALL_STREAM)).state
        assert after.first_seq > full.first_seq and after.bytes <= WALL_STREAM_BYTES
        with pytest.raises(APIError) as too_large:
            await jetstream.publish("wall.too_large", b"x" * (WALL_MESSAGE_BYTES + 1))
        assert too_large.value.err_code == 10054, too_large.value
        await writer.close()

    try:
        asyncio.run(run())
    finally:
        hub.stop()


# E3a-2: Central's client role on a Node's objects, and the hub's lifecycle.

BUCKET = "probe_desired"
CONSUMER = ConsumerConfig(durable_name="central", ack_policy=AckPolicy.EXPLICIT, ack_wait=2,
                          deliver_policy=DeliverPolicy.ALL)
IDLE_SECONDS = 3.0   # longer than the ack wait, so an idle fetch means nothing is left to redeliver


class _Crash(Exception):
    """Central's process dies between committing a message and acknowledging it."""


async def _drain(client, stream: str, recorder: Recorder, *, crash_at: int | None = None) -> None:
    """Central's durable read of a Node stream: commit each message, then acknowledge it.

    A delivered sequence past the last recorded one is a gap only for the part the stream no
    longer holds (below its first_seq); later sequences can still arrive as redeliveries. The gap
    row is recorded before the message that revealed it. Returns once a fetch longer than the ack
    wait finds nothing, so every unacknowledged delivery has come back."""
    jetstream = client.jetstream(domain=NODE_DOMAIN)
    await jetstream.add_consumer(stream, CONSUMER)
    subscription = await jetstream.pull_subscribe_bind(CONSUMER.durable_name, stream)
    while True:
        try:
            messages = await subscription.fetch(10, timeout=IDLE_SECONDS)
        except nats.errors.TimeoutError:
            return
        for message in messages:
            sequence = message.metadata.sequence.stream
            highest = max([0, *recorder.sequences(), *(first + count - 1 for first, count in recorder.gaps())])
            if sequence > highest + 1:
                first_held = (await jetstream.stream_info(stream)).state.first_seq
                missing = min(first_held, sequence) - highest - 1
                if missing > 0:
                    recorder.gap(highest + 1, missing)
            recorder.commit(sequence, message.data)
            if sequence == crash_at:
                raise _Crash
            await message.ack_sync()


def test_central_conditionally_updates_a_node_bucket_across_the_leaf(tmp_path):
    hub = hub_server(tmp_path, ["serial-a"])
    node = node_server(tmp_path, "serial-a", hub)
    hub.start()
    node.start()

    async def run():
        await _linked(hub, 1)
        node_client = await local(node)
        central_client = await central(hub, "serial-a")
        kv = await declare_bucket(node_client.jetstream(), bucket(BUCKET, history=2, max_bytes=64 * 1024))
        config = (await node_client.jetstream().stream_info(f"KV_{BUCKET}")).config
        assert (config.discard, config.max_msgs_per_subject) == (DiscardPolicy.OLD, 2)
        await kv.put("scene", b"node")
        watcher = await kv.watch("scene")

        # Central reads the revision across the leaf, then writes conditionally on it.
        revision = (await (await central_client.jetstream(domain=NODE_DOMAIN).key_value(BUCKET)).get("scene")).revision
        written = await central_put(central_client, BUCKET, "scene", b"central", expected_revision=revision)
        assert written > revision

        async def watcher_saw_central():
            try:
                entry = await watcher.updates(timeout=.5)
            except nats.errors.TimeoutError:
                return False
            return entry is not None and (entry.value, entry.revision) == (b"central", written)
        await _until(watcher_saw_central, 5, "the Node's watcher sees Central's value")

        # A second write on the stale revision is refused by the Node's server.
        with pytest.raises(APIError) as stale:
            await central_put(central_client, BUCKET, "scene", b"stale", expected_revision=revision)
        assert stale.value.err_code == 10071
        entry = await kv.get("scene")
        assert (entry.value, entry.revision) == (b"central", written)

        # A plain $KV publish from Central never reaches the Node: the leaf denies $KV.> (W9).
        with pytest.raises(NoStreamResponseError):
            await central_client.jetstream().publish(f"$KV.{BUCKET}.scene", b"plain", timeout=1)
        await central_client.publish(f"$KV.{BUCKET}.scene", b"plain")
        await central_client.flush()
        await asyncio.sleep(.5)
        entry = await kv.get("scene")
        assert (entry.value, entry.revision) == (b"central", written)

        await watcher.stop()
        await node_client.close()
        await central_client.close()

    try:
        asyncio.run(run())
    finally:
        node.stop()
        hub.stop()


@pytest.mark.parametrize("source", ["stream", "bucket"])
def test_central_durable_consumer_acks_after_commit_and_loses_nothing(tmp_path, source):
    hub = hub_server(tmp_path, ["serial-a"])
    node = node_server(tmp_path, "serial-a", hub)
    hub.start()
    node.start()
    recorder = Recorder(tmp_path / "record")

    async def run():
        await _linked(hub, 1)
        node_client = await local(node)
        jetstream = node_client.jetstream()
        if source == "stream":
            stream = "EVENTS"
            await jetstream.add_stream(buffer(stream, 256 * 1024, subjects=["events.>"]))
            for index in range(200):
                await jetstream.publish(f"events.{index % 4}", f"event-{index}".encode())
        else:
            stream = "KV_probe_state"
            kv = await declare_bucket(jetstream, bucket("probe_state", history=1, max_bytes=256 * 1024))
            for index in range(200):
                await kv.put(f"key{index}", f"state-{index}".encode())
        assert (await jetstream.stream_info(stream)).state.last_seq == 200

        # Central dies mid-batch: the message it committed and the rest of its batch go unacknowledged.
        first = await central(hub, "serial-a")
        with pytest.raises(_Crash):
            await _drain(first, stream, recorder, crash_at=78)
        await first.close()
        assert 60 < len(recorder.sequences()) < 200

        # A new client binds the same durable and drains what the server still owes it.
        second = await central(hub, "serial-a")
        await _drain(second, stream, recorder)
        await second.close()
        assert sorted(set(recorder.sequences())) == list(range(1, 201))
        # The committed-but-unacknowledged message came back once: at least once, folded by sequence.
        assert recorder.sequences().count(78) == 2
        assert recorder.gaps() == []
        await node_client.close()

    try:
        asyncio.run(run())
    finally:
        node.stop()
        hub.stop()


@pytest.mark.parametrize("source", ["stream", "bucket"])
def test_a_buffer_that_overflows_while_central_is_away_reaches_central_as_a_counted_gap(tmp_path, source):
    hub = hub_server(tmp_path, ["serial-a"])
    node = node_server(tmp_path, "serial-a", hub)
    hub.start()
    node.start()
    recorder = Recorder(tmp_path / "record")

    async def run():
        await _linked(hub, 1)
        node_client = await local(node)
        jetstream = node_client.jetstream()
        if source == "stream":
            stream = "OBSERVED"
            await jetstream.add_stream(buffer(
                stream, 16 * 1024, subjects=["observed.>"], retention=RetentionPolicy.LIMITS))

            async def write(index: int) -> None:
                await jetstream.publish("observed.reading", f"reading-{index:04}".encode())
        else:
            stream = "KV_probe_observed"
            kv = await declare_bucket(jetstream, bucket("probe_observed", history=1, max_bytes=16 * 1024))

            async def write(index: int) -> None:
                await kv.put(f"reading{index:04}", f"reading-{index:04}".encode())
        for index in range(10):
            await write(index)
        central_client = await central(hub, "serial-a")
        await _drain(central_client, stream, recorder)
        await central_client.close()
        assert recorder.sequences() == list(range(1, 11))

        # Central is away: the Node keeps writing, every write accepted, the oldest discarded.
        for index in range(10, 1010):
            await write(index)
        state = (await jetstream.stream_info(stream)).state
        assert state.last_seq == 1010 and state.first_seq > 11

        central_client = await central(hub, "serial-a")
        await _drain(central_client, stream, recorder)
        await central_client.close()
        rows = recorder.rows()
        assert rows[:10] == [("seq", sequence, 0) for sequence in range(1, 11)]
        assert rows[10] == ("gap", 11, state.first_seq - 10 - 1)
        assert recorder.gaps() == [(11, state.first_seq - 11)]
        assert recorder.sequences()[10:] == list(range(state.first_seq, state.last_seq + 1))
        await node_client.close()

    try:
        asyncio.run(run())
    finally:
        node.stop()
        hub.stop()


async def _holds(node: BusServer, values: dict[str, bytes], seconds: float) -> None:
    client = await local(node)
    try:
        async def check():
            return all([await wall_value(client, subject) == value for subject, value in values.items()])
        await _until(check, seconds, f"{node.name} mirror holds {values!r}")
    finally:
        await client.close()


def test_a_reload_that_adds_an_account_keeps_existing_leaf_links(tmp_path):
    hub = hub_server(tmp_path, ["serial-a", "serial-b"])
    node_a = node_server(tmp_path, "serial-a", hub)
    node_b = node_server(tmp_path, "serial-b", hub)
    node_c = node_server(tmp_path, "serial-c", hub)
    for server in (hub, node_a, node_b):
        server.start()

    async def run():
        await _linked(hub, 2)
        writer = await wall_writer(hub)
        await declare_wall(writer)
        values = {"wall.timing": b"timing-1", "wall.scene": b"scene-1"}
        for subject, value in values.items():
            await writer.jetstream().publish(subject, value)
        await writer.close()
        before = leaf_connections(hub)

        await reload_hub(hub, ["serial-a", "serial-b", "serial-c"])
        node_c.start()
        await _linked(hub, 3)
        after = leaf_connections(hub)
        assert {account: after[account] for account in before} == before
        assert account_id("serial-c") in after

        client = await local(node_c)
        await declare_wall_mirror(client)
        await client.close()
        await _holds(node_c, values, 10)

    try:
        asyncio.run(run())
    finally:
        for server in (node_c, node_b, node_a, hub):
            server.stop()


class _WallCentral:
    """Central's wall writer: it keeps the latest value per subject and the last sequence WALL
    acknowledged and, on every connect, declares WALL (create-if-absent, continuing past that
    sequence) and re-puts each latest value."""

    def __init__(self, hub: BusServer) -> None:
        self.hub = hub
        self.latest: dict[str, bytes] = {}
        self.last_seq = 0
        self._client = None

    async def connect(self) -> None:
        self._client = await wall_writer(self.hub)
        await declare_wall(self._client, first_seq=self.last_seq + 1)
        for subject, value in self.latest.items():
            await self._publish(subject, value)

    async def put(self, subject: str, value: bytes) -> None:
        self.latest[subject] = value
        await self._publish(subject, value)

    async def _publish(self, subject: str, value: bytes) -> None:
        acknowledgement = await self._client.jetstream().publish(subject, value)
        self.last_seq = max(self.last_seq, acknowledgement.seq)

    async def close(self) -> None:
        await self._client.close()


def test_the_wall_mirror_catches_up_after_the_hub_loses_its_store(tmp_path):
    hub = hub_server(tmp_path, ["serial-a", "serial-b"])
    node_a = node_server(tmp_path, "serial-a", hub)
    node_b = node_server(tmp_path, "serial-b", hub)
    for server in (hub, node_a, node_b):
        server.start()

    async def run():
        await _linked(hub, 2)
        wall = _WallCentral(hub)
        await wall.connect()
        for node in (node_a, node_b):
            client = await local(node)
            await declare_wall_mirror(client)
            await client.close()
        # Several rounds, so the hub's sequence is well past what a fresh store re-puts.
        for round_ in range(4):
            await wall.put("wall.timing", f"timing-{round_}".encode())
            await wall.put("wall.scene", f"scene-{round_}".encode())
        for node in (node_a, node_b):
            await _holds(node, wall.latest, 10)
        await wall.close()

        # The hub loses its store; one value changes in Central while the hub is down.
        acknowledged = wall.last_seq
        hub.wipe()
        wall.latest["wall.scene"] = b"scene-outage"

        # Hub away: a Node component's declare needs nothing from the hub; the mirror still reads.
        client = await local(node_a)
        assert not await declare_wall_mirror(client)
        assert await wall_value(client, "wall.timing") == b"timing-3"
        await client.close()

        # Each mirror waits for its next sequence; Central's new WALL starts there (§6 row 11 needs
        # no Node rule, E-W1-E3a-R-4). Either order of declares: Node A before Central's, Node B after.
        hub.start()
        await _linked(hub, 2)
        client = await local(node_a)
        assert not await declare_wall_mirror(client)
        await client.close()
        await wall.connect()
        assert (await wall._client.jetstream().stream_info(WALL_STREAM)).state.first_seq == acknowledged + 1
        client = await local(node_b)
        assert not await declare_wall_mirror(client)
        await client.close()
        for node in (node_a, node_b):
            await _holds(node, wall.latest, 30)
        await wall.close()

    try:
        asyncio.run(run())
    finally:
        for server in (node_b, node_a, hub):
            server.stop()
