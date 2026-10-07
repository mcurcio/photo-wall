"""Sticky documents on real servers (errata E-W1-TD-4, E-W1-TD-5; E3b design §7.2 documents, §9.2):
a desired bucket and WALL keep every document whatever else is written, because each writer takes
the key table from the stream itself and refuses its own over-budget write; a token read before a
Node store loss raises a conflict and is never applied blind.

The hub runs Fleet's generated configuration, each Node the shipped `node-bus.conf`; Central's writers
are `nodeapi.documents.DocumentWriter` and `nodeapi.hub.WallWriter`. Every bucket, subject and
document is the test's.
"""
from __future__ import annotations

import asyncio

import pytest
from integration.bus_servers import (
    BusServer,
    FileLinkStore,
    central,
    declare_bucket,
    declare_wall_mirror,
    desired_documents,
    hub_server,
    leaf_connections,
    local,
    node_server,
    until,
    wall_value,
    wall_writer,
)
from nats.js.api import Header
from nats.js.errors import APIError

from contracts.node_link import CENTRAL_WRITER, MAX_STORED_MESSAGE, NODE_DOMAIN, WALL_STREAM
from nodeapi.buffers import HEADER_ALLOWANCE, KeyTable, desired_bucket
from nodeapi.documents import ABSENT, Conflict, DocumentRefused, DocumentWriter
from nodeapi.epoch import epoch_of
from nodeapi.hub import WallWriter

WRONG_LAST_SEQUENCE = 10071   # JSStreamWrongLastSequenceErr: the key moved on since the token
PLAYER = "KV_desired_player"


async def _linked(hub: BusServer, count: int) -> None:
    async def check():
        return len(leaf_connections(hub)) == count
    await until(check, 10, f"{count} leaf links at the hub")


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
    # The teardown's case: "show" written once, then every other document past the bucket's budget
    # many times over; "show" stays. Seventy unlisted documents are refused by Central's own writer,
    # which read the table from the bucket, before sending, so the Node sees none (E-W1-TD-4).
    hub, [node] = _servers(tmp_path)

    async def body():
        await _linked(hub, 1)
        client = await local(node)
        jetstream = client.jetstream()
        table = desired_documents("player")
        config = desired_bucket("player", table)
        kv = await declare_bucket(jetstream, config)
        writer = await DocumentWriter.bind(await central(hub, "serial-a"), PLAYER, writer=CENTRAL_WRITER,
                                           domain=NODE_DOMAIN)
        assert writer.table == table

        tokens = {"show": await writer.put("show", b"frame-1: run 42", expect=ABSENT)}
        others = sorted(set(table.sizes) - {"show"})
        for round_ in range(3 * table.history):
            for key in others:
                tokens[key] = await writer.put(key, bytes([65 + round_]) * table.sizes[key],
                                               expect=tokens.get(key, ABSENT))
        state = (await jetstream.stream_info(PLAYER)).state
        assert state.bytes <= config.max_bytes
        for index in range(70):
            with pytest.raises(DocumentRefused, match="document_unlisted"):
                await writer.put(f"frame{index + 20}", b"d" * 3800, expect=ABSENT)
        with pytest.raises(DocumentRefused, match="document_too_large"):
            await writer.put("layout", b"x" * (table.sizes["layout"] + 1), expect=tokens["layout"])
        assert (await jetstream.stream_info(PLAYER)).state.last_seq == state.last_seq

        assert (await kv.get("show")).value == b"frame-1: run 42"
        for key in table.sizes:
            document = await writer.read(key)
            assert document is not None and document.writer == CENTRAL_WRITER, key
            assert document.token == tokens[key], key
        await client.close()

    _run(hub, [node], body)


def test_wall_documents_survive_the_largest_messages_in_the_hub_and_every_mirror(tmp_path):
    # The teardown's WALL case: five small subjects, then two of the largest messages. The wall table's
    # budget fits WALL_STREAM_BYTES, so the hub stream and the Node's mirror keep all seven however
    # often each is rewritten; a write past the table is refused by Central's writer (E-W1-TD-4).
    hub, [node] = _servers(tmp_path)
    small = ("scene", "timing", "palette", "brightness", "layout")
    largest = MAX_STORED_MESSAGE - HEADER_ALLOWANCE
    table = KeyTable({**{key: 64 for key in small}, "big1": largest, "big2": largest})

    async def body():
        await _linked(hub, 1)
        client = await local(node)
        await declare_wall_mirror(client)
        writer_client = await wall_writer(hub)
        wall = WallWriter(writer_client, table, FileLinkStore(tmp_path / "marks.jsonl"))
        assert await wall.ensure() is True
        for key in small:
            await wall.put(key, f"small-{key}".encode())
        for round_ in range(3):
            for key in ("big1", "big2"):
                last = await wall.put(key, bytes([65 + round_]) * largest)
        for refused, value in (("big3", b"C" * largest), ("scene", b"x" * 65)):
            with pytest.raises(DocumentRefused):
                await wall.put(refused, value)
        hub_wall = writer_client.jetstream()
        assert (await hub_wall.stream_info(WALL_STREAM)).state.last_seq == last

        mirror = client.jetstream()

        async def caught_up():
            return (await mirror.stream_info(WALL_STREAM)).state.last_seq >= last
        await until(caught_up, 10, "the mirror reaches the last wall write")
        for jetstream in (hub_wall, mirror):
            for key in small:
                assert (await jetstream.get_last_msg(WALL_STREAM, f"wall.{key}")).data == f"small-{key}".encode()
            for key in ("big1", "big2"):
                assert (await jetstream.get_last_msg(WALL_STREAM, f"wall.{key}")).data == b"C" * largest
        assert await wall_value(client, "wall.big1") == b"C" * largest
        await writer_client.close()
        await client.close()

    _run(hub, [node], body)


def test_a_token_from_a_lost_store_raises_a_conflict_and_is_never_applied(tmp_path):
    # The teardown's case: the Node's store is lost and a Node component writes its default before
    # Central returns; Central's conditional write on its old token must not land. The new creation's
    # sequences start elsewhere (E-W1-TD-5), so the write meets the server's 10071 and Central's writer
    # raises Conflict: it never writes blind. Central re-reads (which follows the new epoch) and writes
    # on the new token. The Node component holds its bucket's configuration and declares that same
    # object on every connect: the epoch is drawn when the stream is created (E-W1-FV-1).
    hub, [node] = _servers(tmp_path)

    async def body():
        await _linked(hub, 1)
        config = desired_bucket("player", desired_documents("player"))
        client = await local(node)
        await declare_bucket(client.jetstream(), config)
        first = await client.jetstream().stream_info(PLAYER)
        await client.close()
        central_client = await central(hub, "serial-a")
        writer = await DocumentWriter.bind(central_client, PLAYER, writer=CENTRAL_WRITER, domain=NODE_DOMAIN)
        boot_one = await writer.put("show", b"central: run 42", expect=ABSENT)

        node.wipe()
        node.start()
        await _linked(hub, 1)
        client = await local(node)
        await declare_bucket(client.jetstream(), config)
        second = await client.jetstream().stream_info(PLAYER)
        assert epoch_of(second) != epoch_of(first) and second.config.first_seq != first.config.first_seq
        # A configuration read back from a stream names its creation; declaring it is refused, so a
        # re-create never reuses an old epoch or origin.
        with pytest.raises(ValueError, match="declare_needs_a_built_buffer"):
            await declare_bucket(client.jetstream(), first.config)
        node_writer = await DocumentWriter.bind(client, PLAYER, writer="node")
        node_token = await node_writer.put("show", b"node: local default", expect=ABSENT)

        with pytest.raises(Conflict) as conflict:
            await writer.put("show", b"central: run 43", expect=boot_one)
        assert conflict.value.key == "show"
        with pytest.raises(APIError) as raw:
            await central_client.jetstream(domain=NODE_DOMAIN).publish(
                f"$JS.{NODE_DOMAIN}.API.$KV.{PLAYER.removeprefix('KV_')}.show", b"central: raw",
                headers={Header.EXPECTED_LAST_SUBJECT_SEQUENCE.value: str(boot_one.seq)})
        assert raw.value.err_code == WRONG_LAST_SEQUENCE
        assert await node_writer.read("show") == (b"node: local default", "node", node_token)

        found = await writer.read("show")
        assert found == (b"node: local default", "node", node_token)
        assert writer.epoch == epoch_of(second) != boot_one.epoch
        written = await writer.put("show", b"central: run 43", expect=found.token)
        assert await node_writer.read("show") == (b"central: run 43", CENTRAL_WRITER, written)
        await central_client.close()
        await client.close()

    _run(hub, [node], body)
