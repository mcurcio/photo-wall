"""The Node bus's store budget on a real server (E3a-3, API8): the server itself refuses, at create,
a stream past the store limit, one without a byte cap and one in memory; with every buffer on the
Node full at once and the store wholly reserved, each still takes a write, the server dropping its
oldest (the buffer rule, E-W1-BUF-2); each retention class keeps the promise the Node API page
states for it.

The Node runs the shipped `node-bus.conf`, unmodified. The split is the harness's `node_split` (the
page's numbers; E3b's class table owns them). Only the full-store test starts the hub, for the wall
mirror; elsewhere the Node's leaf retries in the background and nothing crosses it. Every stream,
bucket and subject is the test's.
"""
from __future__ import annotations

import asyncio
import dataclasses
import time

import pytest
from integration.bus_servers import (
    MIB,
    BusServer,
    bucket,
    buffer,
    declare_bucket,
    declare_wall,
    declare_wall_mirror,
    hub_server,
    kv_bucket_bytes,
    leaf_connections,
    local,
    node_server,
    node_split,
    wall_value,
    wall_writer,
)
from nats.js.api import StorageType
from nats.js.errors import APIError, KeyNotFoundError

from contracts.node_link import NODE_MAX_PAYLOAD, WALL_STREAM

STORAGE_EXCEEDED = 10047        # JSStorageResourcesExceededErr: past the store's reservation
MAX_BYTES_REQUIRED = 10113      # JSStreamMaxBytesRequired: account API refuses an uncapped stream
MEMORY_EXCEEDED = 10028         # JSMemoryResourcesExceededErr: the server has no memory store
VALUE_TOO_LARGE = 10054         # JSStreamMessageExceedsMaximumErr: over max_value_size

STORE_LIMIT = 12 * MIB           # node-bus.conf's max_file_store, the outer fence
# nats-server's per-message store charge: 30 + subject + payload, and 4 + headers more with headers
# (ns:server/filestore.go:10055-10062, erratum E-W1-E3a-3-1).
CHARGE = 30
FILL_CHARGE = 4096               # every fill message is charged exactly this, so the caps tile exactly


def _node(tmp_path) -> BusServer:
    """A Node on the shipped file; its hub is configured but never started."""
    return node_server(tmp_path, "serial-a", hub_server(tmp_path, ["serial-a"]))


def _run(node: BusServer, body) -> None:
    node.start()
    try:
        asyncio.run(body())
    finally:
        node.stop()


async def _refused(declare, code: int) -> None:
    with pytest.raises(APIError) as refusal:
        await declare
    assert refusal.value.err_code == code, refusal.value


async def _until(check, seconds: float, what: str):
    deadline = time.monotonic() + seconds
    while not (value := await check()):
        assert time.monotonic() < deadline, f"not within {seconds}s: {what}"
        await asyncio.sleep(.05)
    return value


async def _mirror_reaches(jetstream, seq: int):
    async def check():
        state = (await jetstream.stream_info(WALL_STREAM)).state
        return state if state.last_seq >= seq else None
    return await _until(check, 10, f"the wall mirror reaches {seq}")


def test_with_every_node_buffer_full_each_still_takes_a_write(tmp_path):
    hub = hub_server(tmp_path, ["serial-a"])
    node = node_server(tmp_path, "serial-a", hub)
    hub.start()

    async def body():
        async def linked():
            return len(leaf_connections(hub)) == 1
        await _until(linked, 10, "the Node's leaf link")
        writer = await wall_writer(hub)
        await declare_wall(writer)
        wall = writer.jetstream()
        client = await local(node)
        jetstream = client.jetstream()
        assert client.max_payload == NODE_MAX_PAYLOAD  # the shipped file's pin, as the server applies it

        # The server, not a library convention, refuses at create an uncapped stream and a memory one.
        await _refused(jetstream.add_stream(dataclasses.replace(
            buffer("UNCAPPED", 1, subjects=["uncapped.>"]), max_bytes=None)), MAX_BYTES_REQUIRED)
        await _refused(jetstream.add_stream(dataclasses.replace(
            buffer("MEMORY", 64 * 1024, subjects=["memory.>"]), storage=StorageType.MEMORY)),
            MEMORY_EXCEEDED)

        # The page's split, its wall copy the real mirror, and one more buffer for the rest of the
        # store: the caps reserve the whole store, with no headroom anywhere.
        split = node_split()
        split["REST"] = buffer("REST", STORE_LIMIT - sum(c.max_bytes for c in split.values()),
                               subjects=["rest.>"])
        for name, config in split.items():
            if name == WALL_STREAM:
                assert await declare_wall_mirror(client)
            else:
                await jetstream.add_stream(config)
        caps = {name: config.max_bytes for name, config in split.items()}
        assert sum(caps.values()) == STORE_LIMIT and caps["REST"] > 0
        await jetstream.publish("player.record.asrun", b"as-run 1")
        before = {name: (await jetstream.stream_info(name)) for name in caps}

        # The fence acts at create only: one more stream, or a raised cap, does not fit (10047).
        await _refused(jetstream.add_stream(buffer("EXTRA", MIB, subjects=["extra.>"])), STORAGE_EXCEEDED)
        raised = before["REC_player"].config
        raised.max_bytes = 5 * MIB
        await _refused(jetstream.update_stream(raised), STORAGE_EXCEEDED)

        # Nothing that was there moved.
        assert sorted(info.config.name for info in await jetstream.streams_info()) == sorted(caps)
        for name, cap in caps.items():
            info = await jetstream.stream_info(name)
            assert info.config.max_bytes == cap, name
            assert (info.state.messages, info.state.last_seq) == (
                before[name].state.messages, before[name].state.last_seq), name
        assert (await jetstream.get_last_msg("REC_player", "player.record.asrun")).data == b"as-run 1"

        # Fill the whole store: every stream and bucket holds exactly its cap; WALL on the hub too,
        # one value per subject, and the Node's mirror with it.
        buckets = {}
        for name, cap in caps.items():
            if name == WALL_STREAM:
                for index in range(cap // FILL_CHARGE):
                    acknowledgement = await wall.publish(f"wall.fill{index:04}", _fill(f"wall.fill{index:04}"))
                await _mirror_reaches(jetstream, acknowledgement.seq)
            elif name.startswith("KV_"):
                buckets[name] = kv = await jetstream.key_value(name.removeprefix("KV_"))
                for index in range(cap // FILL_CHARGE):
                    await kv.put(f"fill{index}", _fill(f"$KV.{name.removeprefix('KV_')}.fill{index}"))
            else:
                subject = _subject(split[name])
                for _ in range(cap // FILL_CHARGE):
                    await jetstream.publish(subject, _fill(subject))
            assert (await jetstream.stream_info(name)).state.bytes == cap, name
        assert (await jetstream.account_info()).storage == STORE_LIMIT

        # Every buffer full at once, each takes the next write: the next sequence, and the server
        # dropped the oldest, a hole below first_seq a reader counts (F7: never a refused write).
        for name in caps:
            full = (await jetstream.stream_info(name)).state
            if name == WALL_STREAM:
                acknowledgement = await wall.publish("wall.one_more", _fill("wall.one_more"))
                assert acknowledgement.seq == full.last_seq + 1
                await _mirror_reaches(jetstream, acknowledgement.seq)
                assert await wall_value(client, "wall.fill0000") is None
            elif name in buckets:
                revision = await buckets[name].put("one-more", b"x")
                assert revision == full.last_seq + 1, name
                with pytest.raises(KeyNotFoundError):
                    await buckets[name].get("fill0")
            else:
                acknowledgement = await jetstream.publish(_subject(split[name]), _fill(_subject(split[name])))
                assert (acknowledgement.stream, acknowledgement.seq) == (name, full.last_seq + 1)
            after = (await jetstream.stream_info(name)).state
            assert after.first_seq > full.first_seq and after.bytes <= caps[name], name

        # The largest message, into a full stream of a full store.
        full = (await jetstream.stream_info("REC_player")).state
        acknowledgement = await jetstream.publish("player.record.asrun", b"L" * NODE_MAX_PAYLOAD)
        assert acknowledgement.seq == full.last_seq + 1
        assert (await jetstream.stream_info("REC_player")).state.first_seq > full.first_seq
        assert (await jetstream.account_info()).storage <= STORE_LIMIT
        await client.close()
        await writer.close()

    try:
        _run(node, body)
    finally:
        hub.stop()


def _subject(config) -> str:
    """A publish subject inside a stream's one wildcard subject."""
    return config.subjects[0].replace(">", "fill")


def _fill(subject: str) -> bytes:
    """A payload the store charges exactly FILL_CHARGE bytes on `subject` (no headers)."""
    return b"f" * (FILL_CHARGE - CHARGE - len(subject))


async def _record(jetstream) -> None:
    """Past its cap the oldest go, every publish is accepted, and first_seq advances by the drop."""
    cap = 16 * 1024
    await jetstream.add_stream(buffer("REC_probe", cap, subjects=["probe.record.>"]))
    published = 1000
    for index in range(published):
        acknowledgement = await jetstream.publish("probe.record.asrun", f"as-run {index:06}".encode() * 4)
        assert acknowledgement.seq == index + 1
    state = (await jetstream.stream_info("REC_probe")).state
    dropped = published - state.messages
    assert dropped > 0 and state.bytes <= cap
    assert (state.first_seq, state.last_seq) == (1 + dropped, published)
    config = (await jetstream.stream_info("REC_probe")).config
    assert (config.max_age or 0) == 0 and config.max_msgs_per_subject in (None, -1)


async def _observation(jetstream) -> None:
    """A message older than max_age is gone; a fresh one stays."""
    await jetstream.add_stream(buffer("OBS_probe", 16 * 1024, subjects=["probe.observation.>"], max_age=1))
    published = time.monotonic()
    for index in range(3):
        await jetstream.publish("probe.observation.verdict", f"verdict {index}".encode())
    assert (await jetstream.stream_info("OBS_probe")).state.messages == 3
    deadline = published + 5
    while (state := (await jetstream.stream_info("OBS_probe")).state).messages:
        assert time.monotonic() < deadline, f"aged messages still held: {state}"
        await asyncio.sleep(.1)
    assert time.monotonic() - published >= 1
    assert state.first_seq == 4
    await jetstream.publish("probe.observation.verdict", b"fresh")
    assert (await jetstream.get_last_msg("OBS_probe", "probe.observation.verdict")).data == b"fresh"


async def _bucket(jetstream, prefix: str, history: int) -> None:
    """Listed keys, at up to max_value bytes and ten times the capacity, are always accepted and keep
    `history` values. A key beyond the list is accepted too: the bucket has no headroom, so the
    first costs the bucket its oldest message, a hole in its sequence. An oversized value is refused
    (a message limit, not fullness); every listed key keeps its last good value."""
    name, keys, max_value = f"{prefix}_probe", ("conditions", "position", "horizon", "verdicts"), 512
    capacity = kv_bucket_bytes(name, keys, history, max_value)
    kv = await declare_bucket(jetstream, bucket(
        name, history=history, max_bytes=capacity, max_value_size=max_value))

    # Sizes vary up to max_value, and each key in turn lags the others with one short value.
    last: dict[str, bytes] = {}
    written, put = 0, 0
    while written < 10 * capacity:
        key = keys[put % len(keys)]
        size = max_value if (put // len(keys) + keys.index(key)) % (history + 1) else max_value // 3
        value = f"{put:08}".encode().ljust(size, b".")
        await kv.put(key, value)
        last[key], written, put = value, written + len(value), put + 1
    for key in keys:  # end full: every key holds `history` values of max_value bytes
        for index in range(history):
            value = f"full {index}".encode().ljust(max_value, b"#")
            await kv.put(key, value)
            last[key] = value
    for key in keys:
        assert len(await kv.history(key)) == history, key

    # A key beyond the list: the put is accepted, the bucket's oldest goes (here the first key's
    # oldest value) and its first sequence moves past it, the hole a reader counts.
    full = (await jetstream.stream_info(f"KV_{name}")).state
    assert await kv.put("unlisted", b"x" * max_value) == full.last_seq + 1
    after = (await jetstream.stream_info(f"KV_{name}")).state
    assert after.first_seq > full.first_seq and after.bytes <= capacity
    with pytest.raises(APIError) as oversized:
        await kv.put(keys[0], b"x" * (max_value + 1))
    assert oversized.value.err_code == VALUE_TOO_LARGE, oversized.value
    for key in keys:
        assert (await kv.get(key)).value == last[key], key


@pytest.mark.parametrize("retention_class", ["record", "observation", "state", "desired"])
def test_each_retention_class_keeps_its_promise(tmp_path, retention_class):
    node = _node(tmp_path)

    async def body():
        client = await local(node)
        jetstream = client.jetstream()
        if retention_class == "record":
            await _record(jetstream)
        elif retention_class == "observation":
            await _observation(jetstream)
        else:
            await _bucket(jetstream, retention_class, 4 if retention_class == "state" else 2)
        await client.close()

    _run(node, body)
