"""The Node bus seam on real servers (E3a): the WebSocket leaf, a method across it, same-domain
isolation, and the wall-wide mirror.

The hub runs Fleet's generated configuration, each Node the shipped `node-bus.conf`; raw nats-py
clients play Central and Node components. Every service, stream and subject here is the test's.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request

import nats.errors
import nats.micro
from integration.bus_servers import (
    BusServer,
    central,
    declare_wall,
    declare_wall_mirror,
    hub_server,
    local,
    node_server,
    wall_value,
    wall_writer,
)
from nats.js.api import StorageType, StreamConfig

from contracts.node_link import NODE_DOMAIN, WALL_STREAM, account_id

SERVICE = "probe"
ENDPOINT = "probe.echo"


def _leaf_accounts(hub: BusServer) -> list[str]:
    with urllib.request.urlopen(f"{hub.monitor_url}/leafz", timeout=2) as response:
        return [leaf["account"] for leaf in json.load(response).get("leafs") or []]


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
            await jetstream.add_stream(StreamConfig(
                name="PROBE", subjects=["probe.events.>"], max_bytes=64 * 1024,
                storage=StorageType.FILE))
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
        await local_a.close()
        await writer.close()

    try:
        asyncio.run(run())
    finally:
        for server in (node_b, node_a, hub):
            server.stop()
