"""Sticky documents on real servers (errata E-W1-TD-4, E-W1-TD-5): a desired bucket and WALL keep
every document whatever else is written, because their one writer refuses its own over-budget
write; a reader finds a document that went missing; a token read before a Node store loss is stale
and never applied.

The hub runs Fleet's generated configuration, each Node the shipped `node-bus.conf`; Central's writer
is `nodeapi.documents.DocumentWriter`. Every bucket, subject and document is the test's.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from integration.bus_servers import (
    BusServer,
    central,
    declare_bucket,
    declare_wall,
    declare_wall_mirror,
    desired_documents,
    hub_server,
    leaf_connections,
    local,
    node_server,
    wall_writer,
)
from nats.js.api import Header
from nats.js.errors import APIError

from contracts.node_link import MAX_STORED_MESSAGE, NODE_DOMAIN, WALL_STREAM
from nodeapi.buffers import HEADER_ALLOWANCE, Documents, sticky_bucket
from nodeapi.documents import DocumentRefused, DocumentWriter, StaleToken, missing_documents

WRONG_LAST_SEQUENCE = 10071   # JSStreamWrongLastSequenceErr: the key moved on since the token


async def _until(check, seconds: float, what: str):
    deadline = time.monotonic() + seconds
    while not (value := await check()):
        assert time.monotonic() < deadline, f"not within {seconds}s: {what}"
        await asyncio.sleep(.05)
    return value


async def _linked(hub: BusServer, count: int) -> None:
    async def check():
        return len(leaf_connections(hub)) == count
    await _until(check, 10, f"{count} leaf links at the hub")


def _servers(tmp_path, serials=("serial-a",)) -> tuple[BusServer, list[BusServer]]:
    hub = hub_server(tmp_path, list(serials))
    return hub, [node_server(tmp_path, serial, hub) for serial in serials]


def _run(hub: BusServer, nodes: list[BusServer], body) -> None:
    for server in (hub, *nodes):
        server.start()
    try:
        asyncio.run(body())
    finally:
        for server in (*nodes, hub):
            server.stop()


def test_a_desired_document_written_once_survives_every_other_write(tmp_path):
    # The teardown's case: "show" written once, then seventy other documents. Every listed document
    # is written past the bucket's budget many times over; "show" stays. Seventy unlisted ones are
    # refused by Central's own writer before sending, so the Node sees none (E-W1-TD-4).
    hub, [node] = _servers(tmp_path)

    async def body():
        await _linked(hub, 1)
        client = await local(node)
        jetstream = client.jetstream()
        table = desired_documents("player")
        kv = await declare_bucket(jetstream, sticky_bucket(table))
        writer = DocumentWriter.node_bucket(await central(hub, "serial-a"), table)

        await writer.put("show", b"frame-1: run 42")
        others = sorted(set(table.sizes) - {"show"})
        for round_ in range(3 * table.history):
            for key in others:
                await writer.put(key, bytes([65 + round_]) * table.sizes[key])
        state = (await jetstream.stream_info(table.stream)).state
        assert state.bytes <= table.budget
        for index in range(70):
            with pytest.raises(DocumentRefused, match="document_unlisted"):
                await writer.put(f"frame{index + 20}", b"d" * 3800)
        with pytest.raises(DocumentRefused, match="document_too_large"):
            await writer.put("layout", b"x" * (table.sizes["layout"] + 1))
        assert (await jetstream.stream_info(table.stream)).state.last_seq == state.last_seq

        assert (await kv.get("show")).value == b"frame-1: run 42"
        assert await missing_documents(jetstream, table.stream, table.subject_prefix) == set()

        # A stray writer that bypasses nodeapi (raw puts of unlisted keys) can still fill the bucket,
        # and the server drops its oldest; the reader finds the listed document that went. (Small
        # strays, so each drops one message: a large one also takes the manifest, which the reader
        # then reports as missing instead.)
        index = 0
        while not (missing := await missing_documents(jetstream, table.stream, table.subject_prefix)):
            await kv.put(f"stray{index:04}", b"s")
            index += 1
        assert missing == {"show"}
        await client.close()

    _run(hub, [node], body)


def test_wall_documents_survive_the_largest_messages_in_the_hub_and_every_mirror(tmp_path):
    # The teardown's WALL case: five small subjects, then two of the largest messages. The wall table's
    # budget fits WALL_STREAM_BYTES, so the hub stream and the Node's mirror keep all seven however
    # often each is rewritten; a write past the table is refused by Central's writer (E-W1-TD-4).
    hub, [node] = _servers(tmp_path)
    small = ("scene", "timing", "palette", "brightness", "layout")
    largest = MAX_STORED_MESSAGE - HEADER_ALLOWANCE

    async def body():
        await _linked(hub, 1)
        client = await local(node)
        await declare_wall_mirror(client)
        writer_client = await wall_writer(hub)
        await declare_wall(writer_client)
        table = Documents.wall({**{key: 64 for key in small}, "big1": largest, "big2": largest})
        writer = DocumentWriter(writer_client.jetstream(), table)
        for key in small:
            await writer.put(key, f"small-{key}".encode())
        for round_ in range(3):
            for key in ("big1", "big2"):
                last = await writer.put(key, bytes([65 + round_]) * largest)
        for refused, value in (("big3", b"C" * largest), ("scene", b"x" * 65)):
            with pytest.raises(DocumentRefused):
                await writer.put(refused, value)
        hub_wall = writer_client.jetstream()
        assert (await hub_wall.stream_info(WALL_STREAM)).state.last_seq == last.seq

        mirror = client.jetstream()

        async def caught_up():
            return (await mirror.stream_info(WALL_STREAM)).state.last_seq >= last.seq
        await _until(caught_up, 10, "the mirror reaches the last wall write")
        for jetstream in (hub_wall, mirror):
            assert await missing_documents(jetstream, WALL_STREAM, "wall.") == set()
            for key in small:
                assert (await jetstream.get_last_msg(WALL_STREAM, f"wall.{key}")).data == f"small-{key}".encode()
            for key in ("big1", "big2"):
                assert (await jetstream.get_last_msg(WALL_STREAM, f"wall.{key}")).data == b"C" * largest
        await writer_client.close()
        await client.close()

    _run(hub, [node], body)


def test_a_token_from_a_lost_store_is_stale_and_never_applied(tmp_path):
    # The teardown's case: the Node's store is lost (tmpfs, every reboot) and a Node component writes
    # its default before Central returns; Central's conditional write on its old revision must not
    # land. Its token carries the old epoch, so its writer refuses it as stale, and even a raw
    # conditional write on the old revision misses: the new creation's sequences start elsewhere
    # (E-W1-TD-5). Central re-reads and writes on the new token.
    hub, [node] = _servers(tmp_path)

    async def body():
        await _linked(hub, 1)
        table = desired_documents("player")
        client = await local(node)
        await declare_bucket(client.jetstream(), sticky_bucket(table))
        await client.close()
        central_client = await central(hub, "serial-a")
        writer = DocumentWriter.node_bucket(central_client, table)
        boot_one = await writer.put("show", b"central: run 42")

        node.wipe()
        node.start()
        await _linked(hub, 1)
        client = await local(node)
        kv = await declare_bucket(client.jetstream(), sticky_bucket(table))
        node_revision = await kv.put("show", b"node: local default")

        with pytest.raises(StaleToken):
            await writer.put("show", b"central: run 43", token=boot_one)
        with pytest.raises(APIError) as raw:
            await central_client.jetstream(domain=NODE_DOMAIN).publish(
                f"$JS.{NODE_DOMAIN}.API.$KV.{table.stream.removeprefix('KV_')}.show", b"central: raw",
                headers={Header.EXPECTED_LAST_SUBJECT_SEQUENCE.value: str(boot_one.seq)})
        assert raw.value.err_code == WRONG_LAST_SEQUENCE
        entry = await kv.get("show")
        assert (entry.value, entry.revision) == (b"node: local default", node_revision)

        value, boot_two = await writer.read("show")
        assert value == b"node: local default" and boot_two.epoch != boot_one.epoch
        assert boot_two.seq == node_revision
        written = await writer.put("show", b"central: run 43", token=boot_two)
        assert (await kv.get("show")).value == b"central: run 43" and written.epoch == boot_two.epoch
        await central_client.close()
        await client.close()

    _run(hub, [node], body)
