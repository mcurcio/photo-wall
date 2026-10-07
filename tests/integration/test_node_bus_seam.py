"""The Node bus seam on real servers (E3a): the WebSocket leaf, a method across it, same-domain
isolation, the wall-wide mirror (E3a-1); a conditional write into a Node bucket, Central's durable
read with ack after commit and a counted gap, hub reload and hub store loss (E3a-2); the leaf through
a path-prefix proxy (E-W1-TD-S4) and the upstream reload bug's tripwire (E-W1-TD-S3). Every stream,
bucket and mirror is built by `nodeapi.buffers` (E-W1-TD-S1).

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
    PrefixProxy,
    Recorder,
    central,
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
from nats.js.errors import APIError, NoStreamResponseError

from contracts.node_link import (
    MAX_STORED_MESSAGE,
    NODE_DOMAIN,
    NODE_MAX_PAYLOAD,
    WALL_STREAM,
    WALL_STREAM_BYTES,
    account_id,
)
from nodeapi.buffers import Documents, bucket, buffer, declare, epoch_of, sticky_bucket
from nodeapi.documents import DocumentWriter, Token
from nodeapi.pull import pull

VALUE_TOO_LARGE = 10054   # JSStreamMessageExceedsMaximumErr: past the stream's max_msg_size
LEAF_PREFIX = "photo-wall/bus"   # the test's path prefix; the production route is E3d/E4's

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
    node = node_server(tmp_path, "serial-a", hub, prefix=LEAF_PREFIX)
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
            await declare(jetstream, buffer("PROBE", 64 * 1024, subjects=["probe.events.>"]))
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



def test_a_message_past_the_nodes_max_payload_is_refused_at_the_hub_and_the_leaf_stays(tmp_path):
    # A message past the Node's max_payload that crossed the leaf would close it (a maximum payload
    # violation, the write lost). The hub takes no larger message than the Node, so it never gets
    # there: Central's client refuses it before sending, and the hub refuses one the client let
    # through (headers count too) by closing that client, never the leaf (E-W1-BUF-3).
    hub = hub_server(tmp_path, ["serial-a"])
    node = node_server(tmp_path, "serial-a", hub)
    hub.start()
    node.start()

    async def run():
        await _linked(hub, 1)
        linked = leaf_connections(hub)
        node_client = await local(node)
        await declare(node_client.jetstream(), buffer("REC_player", 1024 * 1024, subjects=["player.record.>"]))
        central_client = await central(hub, "serial-a")
        assert central_client.max_payload == NODE_MAX_PAYLOAD
        jetstream = central_client.jetstream(domain=NODE_DOMAIN)

        async def stored():
            try:
                return await jetstream.publish("player.record.small", b"small", timeout=.5)
            except (nats.errors.NoRespondersError, nats.errors.TimeoutError, NoStreamResponseError):
                return None
        assert (await _until(stored, 10, "the Node's stream interest at the hub")).stream == "REC_player"

        with pytest.raises(nats.errors.MaxPayloadError):
            await jetstream.publish("player.record.big", b"B" * (NODE_MAX_PAYLOAD + 1))
        with pytest.raises(nats.errors.MaxPayloadError):
            await central_client.publish("player.record.big", b"B" * (NODE_MAX_PAYLOAD + 1))

        # The largest message crosses the leaf, and the stream refuses it (a message limit, E-W1-TD-2):
        # the largest it stores is MAX_STORED_MESSAGE, which crosses and is stored.
        with pytest.raises(APIError) as too_large:
            await jetstream.publish("player.record.big", b"L" * NODE_MAX_PAYLOAD)
        assert too_large.value.err_code == VALUE_TOO_LARGE
        acknowledgement = await jetstream.publish("player.record.big", b"L" * MAX_STORED_MESSAGE)
        assert acknowledgement.stream == "REC_player"

        # Past the limit only with its headers: the client sends it, the hub refuses it and closes
        # that client; the Node never sees it.
        before = (await node_client.jetstream().stream_info("REC_player")).state.messages
        await central_client.publish("player.record.big", b"H" * NODE_MAX_PAYLOAD, headers={"Probe": "x" * 64})
        async def closed():
            return central_client.is_closed
        await _until(closed, 5, "the hub closes the client that sent past max_payload")
        await asyncio.sleep(.5)
        assert (await node_client.jetstream().stream_info("REC_player")).state.messages == before

        # The leaf never dropped: the same connection id.
        assert leaf_connections(hub) == linked
        again = await central(hub, "serial-a")
        assert (await again.jetstream(domain=NODE_DOMAIN).publish("player.record.small", b"after")).stream == "REC_player"
        assert leaf_connections(hub) == linked
        await again.close()
        await node_client.close()

    try:
        asyncio.run(run())
    finally:
        node.stop()
        hub.stop()

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


# E3a-2: Central's client role on a Node's objects, and the hub's lifecycle.

BUCKET = "probe_desired"
CONSUMER = ConsumerConfig(durable_name="central", ack_policy=AckPolicy.EXPLICIT, ack_wait=2,
                          deliver_policy=DeliverPolicy.ALL)
IDLE_SECONDS = 3.0   # longer than the ack wait, so an idle fetch means nothing is left to redeliver


class _Crash(Exception):
    """Central's process dies between committing a message and acknowledging it."""


async def _drain(client, stream: str, recorder: Recorder, *, crash_at: int | None = None) -> None:
    """Central's durable read of a Node stream: commit each message, then acknowledge it.

    The cursor is (epoch, seq) (E-W1-TD-5): sequences of another creation of the stream are another
    epoch's rows, and a new creation's cursor starts at its origin. A delivered sequence past the
    last recorded one is a gap only for the part the stream no longer holds (below its first_seq);
    later sequences can still arrive as redeliveries. The gap row is recorded before the message
    that revealed it. `crash_at` is a position from the origin (1 = the creation's first message).
    Returns once a pull longer than the ack wait finds nothing, so every unacknowledged delivery
    has come back. Pulls go through nodeapi's capped pull."""
    jetstream = client.jetstream(domain=NODE_DOMAIN)
    await jetstream.add_consumer(stream, CONSUMER)
    info = await jetstream.stream_info(stream)
    epoch, origin = epoch_of(info), info.config.first_seq
    while messages := await pull(client, stream, CONSUMER.durable_name, 10, timeout=IDLE_SECONDS,
                                 domain=NODE_DOMAIN):
        for message in messages:
            sequence = message.metadata.sequence.stream
            highest = max([origin - 1, *recorder.sequences(epoch),
                           *(first + count - 1 for first, count in recorder.gaps(epoch))])
            if sequence > highest + 1:
                first_held = (await jetstream.stream_info(stream)).state.first_seq
                missing = min(first_held, sequence) - highest - 1
                if missing > 0:
                    recorder.gap(epoch, highest + 1, missing)
            recorder.commit(Token(epoch, sequence), message.data)
            if crash_at is not None and sequence == origin + crash_at - 1:
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
        table = Documents.bucket(BUCKET, {"scene": 64}, history=2)
        kv = await declare_bucket(node_client.jetstream(), sticky_bucket(table))
        config = (await node_client.jetstream().stream_info(f"KV_{BUCKET}")).config
        assert (config.retention, config.discard, config.max_msgs_per_subject) == (
            RetentionPolicy.LIMITS, DiscardPolicy.OLD, 2)
        await kv.put("scene", b"node")
        watcher = await kv.watch("scene")

        # Central reads the revision across the leaf, then writes conditionally on it.
        writer = DocumentWriter.node_bucket(central_client, table)
        value, token = await writer.read("scene")
        assert value == b"node"
        assert token.seq == (await (await central_client.jetstream(domain=NODE_DOMAIN).key_value(BUCKET)).get(
            "scene")).revision
        written = (await writer.put("scene", b"central", token=token)).seq
        assert written > token.seq

        async def watcher_saw_central():
            try:
                entry = await watcher.updates(timeout=.5)
            except nats.errors.TimeoutError:
                return False
            return entry is not None and (entry.value, entry.revision) == (b"central", written)
        await _until(watcher_saw_central, 5, "the Node's watcher sees Central's value")

        # A second write on the stale revision is refused by the Node's server.
        with pytest.raises(APIError) as stale:
            await writer.put("scene", b"stale", token=token)
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
            await declare(jetstream, buffer(stream, 256 * 1024, subjects=["events.>"]))
            for index in range(200):
                await jetstream.publish(f"events.{index % 4}", f"event-{index}".encode())
        else:
            stream = "KV_probe_state"
            kv = await declare_bucket(jetstream, bucket("probe_state", history=1, max_bytes=256 * 1024))
            for index in range(200):
                await kv.put(f"key{index}", f"state-{index}".encode())
        info = await jetstream.stream_info(stream)
        origin = info.config.first_seq
        assert info.state.last_seq == origin + 199

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
        assert sorted(set(recorder.sequences())) == list(range(origin, origin + 200))
        # The committed-but-unacknowledged message came back once: at least once, folded by sequence.
        assert recorder.sequences().count(origin + 77) == 2
        assert recorder.gaps() == [] and len(recorder.epochs()) == 1
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
            await declare(jetstream, buffer(stream, 16 * 1024, subjects=["observed.>"]))

            async def write(index: int) -> None:
                await jetstream.publish("observed.reading", f"reading-{index:04}".encode())
        else:
            stream = "KV_probe_observed"
            kv = await declare_bucket(jetstream, bucket("probe_observed", history=1, max_bytes=16 * 1024))

            async def write(index: int) -> None:
                await kv.put(f"reading{index:04}", f"reading-{index:04}".encode())
        for index in range(10):
            await write(index)
        origin = (await jetstream.stream_info(stream)).config.first_seq
        central_client = await central(hub, "serial-a")
        await _drain(central_client, stream, recorder)
        await central_client.close()
        assert recorder.sequences() == list(range(origin, origin + 10))

        # Central is away: the Node keeps writing, every write accepted, the oldest discarded.
        for index in range(10, 1010):
            await write(index)
        state = (await jetstream.stream_info(stream)).state
        assert state.last_seq == origin + 1009 and state.first_seq > origin + 10

        central_client = await central(hub, "serial-a")
        await _drain(central_client, stream, recorder)
        await central_client.close()
        rows = recorder.rows()
        assert rows[:10] == [("seq", sequence, 0) for sequence in range(origin, origin + 10)]
        assert rows[10] == ("gap", origin + 10, state.first_seq - origin - 10)
        assert recorder.gaps() == [(origin + 10, state.first_seq - origin - 10)]
        assert recorder.sequences()[10:] == list(range(state.first_seq, state.last_seq + 1))
        await node_client.close()

    try:
        asyncio.run(run())
    finally:
        node.stop()
        hub.stop()


def test_a_drain_cursor_from_a_lost_node_store_starts_the_new_creation_fresh(tmp_path):
    # The Node's store is lost (tmpfs, every reboot) and its stream re-created. Central's cursor is
    # (epoch, seq): the new creation's messages are new rows from its own origin, neither folded
    # into the old creation's sequences nor counted as a gap after them (E-W1-TD-5).
    hub = hub_server(tmp_path, ["serial-a"])
    node = node_server(tmp_path, "serial-a", hub)
    hub.start()
    node.start()
    recorder = Recorder(tmp_path / "record")

    async def creation(count: int) -> int:
        node_client = await local(node)
        jetstream = node_client.jetstream()
        await declare(jetstream, buffer("EVENTS", 64 * 1024, subjects=["events.>"]))
        for index in range(count):
            await jetstream.publish("events.reading", f"reading-{index}".encode())
        origin = (await jetstream.stream_info("EVENTS")).config.first_seq
        await node_client.close()
        return origin

    async def drain() -> None:
        central_client = await central(hub, "serial-a")
        await _drain(central_client, "EVENTS", recorder)
        await central_client.close()

    async def run():
        await _linked(hub, 1)
        first_origin = await creation(10)
        await drain()
        node.wipe()
        node.start()
        await _linked(hub, 1)
        second_origin = await creation(5)
        await drain()
        first, second = recorder.epochs()
        assert recorder.sequences(first) == list(range(first_origin, first_origin + 10))
        assert recorder.sequences(second) == list(range(second_origin, second_origin + 5))
        assert recorder.gaps() == []

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


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "nats-server 2.15.0 wires a reload-added account's service imports only on the next reload "
    "(ns:server/server.go:1413, configureAccounts while reloading); reload_hub sends two requests. "
    "Strict: an upstream fix turns this red, and then one request is enough (erratum E-W1-TD-S3)."))
def test_one_reload_wires_a_new_accounts_service_imports(tmp_path):
    hub = hub_server(tmp_path, ["serial-a"])
    node_c = node_server(tmp_path, "serial-c", hub)
    hub.start()

    async def run():
        writer = await wall_writer(hub)
        await declare_wall(writer)
        await writer.jetstream().publish("wall.timing", b"timing-1")
        await writer.close()
        await reload_hub(hub, ["serial-a", "serial-c"], requests=1)
        node_c.start()
        await _linked(hub, 1)
        client = await local(node_c)
        await declare_wall_mirror(client)
        await client.close()
        await _holds(node_c, {"wall.timing": b"timing-1"}, 10)

    try:
        asyncio.run(run())
    finally:
        node_c.stop()
        hub.stop()


def test_the_leaf_links_through_a_path_prefix_proxy(tmp_path):
    # The Node dials the origin's ingress route, never the hub's port (W11): a proxy that forwards
    # only its path prefix stands in for it. A method, a Central read of the largest stored message
    # and a wall mirror all cross it (E-W1-TD-S4).
    hub = hub_server(tmp_path, ["serial-a"])
    proxy = PrefixProxy(hub.websocket_port, LEAF_PREFIX)
    node = node_server(tmp_path, "serial-a", hub, prefix=LEAF_PREFIX, leaf_port=proxy.port)
    assert f":{proxy.port}/{LEAF_PREFIX}" in node.environment["PHOTO_WALL_BUS_LEAF_URL"]
    hub.start()

    async def run():
        await proxy.start()
        node.start()
        try:
            await _linked(hub, 1)
            assert proxy.paths == [f"/{LEAF_PREFIX}/leafnode"]
            node_client = await local(node)
            central_client = await central(hub, "serial-a")
            await _serve(node_client, b"node-a")
            await _await_interest(central_client, ENDPOINT)
            assert (await central_client.request(ENDPOINT, b"via", timeout=2)).data == b"node-a:via"

            jetstream = node_client.jetstream()
            await declare(jetstream, buffer("REC_player", 1024 * 1024, subjects=["player.record.>"]))
            await jetstream.publish("player.record.asrun", b"P" * MAX_STORED_MESSAGE)
            message = await central_client.jetstream(domain=NODE_DOMAIN).get_last_msg(
                "REC_player", "player.record.asrun")
            assert len(message.data) == MAX_STORED_MESSAGE

            writer = await wall_writer(hub)
            await declare_wall(writer)
            await declare_wall_mirror(node_client)
            await writer.jetstream().publish("wall.timing", b"through-the-proxy")
            await _holds(node, {"wall.timing": b"through-the-proxy"}, 10)
            assert len(leaf_connections(hub)) == 1 and proxy.paths == [f"/{LEAF_PREFIX}/leafnode"]
            for client in (writer, central_client, node_client):
                await client.close()
        finally:
            node.stop()
            await proxy.close()

    try:
        asyncio.run(run())
    finally:
        node.stop()
        hub.stop()


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
