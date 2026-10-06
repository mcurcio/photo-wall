"""The Node bus's store budget on a real server (E3a-3, API8): the server itself refuses a stream
past the store limit, one without a byte cap and one in memory; a full split still takes every
write, each buffer dropping its oldest (the buffer rule, E-W1-BUF-1); each retention class keeps
the promise the Node API page states for it.

The Node runs the shipped `node-bus.conf`, unmodified. The store limit is a property of the Node's
own server, so no hub runs: the Node's leaf retries in the background and nothing here crosses it.
The split is the harness's `node_split` (the page's numbers; E3b's class table owns them). Every
stream, bucket and subject is the test's.
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
    hub_server,
    kv_bucket_bytes,
    local,
    node_server,
    node_split,
)
from nats.js.api import RetentionPolicy, StorageType
from nats.js.errors import APIError, KeyNotFoundError

from contracts.node_link import NODE_MAX_PAYLOAD, WALL_STREAM

STORAGE_EXCEEDED = 10047        # JSStorageResourcesExceededErr: past the store's reservation
MAX_BYTES_REQUIRED = 10113      # JSStreamMaxBytesRequired: account API refuses an uncapped stream
MEMORY_EXCEEDED = 10028         # JSMemoryResourcesExceededErr: the account has no memory store
VALUE_TOO_LARGE = 10054         # JSStreamMessageExceedsMaximumErr: over max_value_size

STORE_LIMIT = 12 * MIB           # node-bus.conf's max_file_store and account API's max_file
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


def _writable_split() -> dict[str, object]:
    """`node_split` with the wall mirror replaced by a writable stream of the same cap (a mirror
    takes no publish; its own drop is the seam test's)."""
    split = node_split()
    mirror = split.pop(WALL_STREAM)
    split["WALL_COPY"] = buffer("WALL_COPY", mirror.max_bytes, subjects=["wall_copy.>"])
    return split


def test_a_full_node_store_takes_every_write_and_refuses_only_streams_past_its_limit(tmp_path):
    node = _node(tmp_path)

    async def body():
        client = await local(node)
        jetstream = client.jetstream()
        assert client.max_payload == NODE_MAX_PAYLOAD  # the shipped file's pin, as the server applies it

        # The server, not a library convention, refuses an uncapped stream and a memory one.
        await _refused(jetstream.add_stream(dataclasses.replace(
            buffer("UNCAPPED", 1, subjects=["uncapped.>"]), max_bytes=None)), MAX_BYTES_REQUIRED)
        await _refused(jetstream.add_stream(dataclasses.replace(
            buffer("MEMORY", 64 * 1024, subjects=["memory.>"]), storage=StorageType.MEMORY)),
            MEMORY_EXCEEDED)

        # The page's split reserves the store less the room the buffer rule keeps for one message.
        split = _writable_split()
        for config in split.values():
            await jetstream.add_stream(config)
        caps = {name: config.max_bytes for name, config in split.items()}
        assert sum(caps.values()) <= STORE_LIMIT - (34 + 4096 + NODE_MAX_PAYLOAD)
        await jetstream.publish("player.record.asrun", b"as-run 1")
        before = {name: (await jetstream.stream_info(name)) for name in caps}

        # One more stream past the store does not fit, and raising an existing cap does not either.
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

        # Fill the whole split: every stream and bucket holds exactly its cap.
        buckets = {}
        for name, cap in caps.items():
            if name.startswith("KV_"):
                buckets[name] = kv = await jetstream.key_value(name.removeprefix("KV_"))
                for index in range(cap // FILL_CHARGE):
                    await kv.put(f"fill{index}", _fill(f"$KV.{name.removeprefix('KV_')}.fill{index}"))
            else:
                subject = _subject(split[name])
                for _ in range(cap // FILL_CHARGE):
                    await jetstream.publish(subject, _fill(subject))
            assert (await jetstream.stream_info(name)).state.bytes == cap, name
        assert (await jetstream.account_info()).storage == sum(caps.values())

        # Full, every buffer takes the next write: it gets the next sequence and the oldest is gone,
        # never a refused write with no sequence. A log's drop is a hole a reader counts (F7); a
        # bucket's is a key it no longer holds (`lost_subjects`, E-W1-BUF-2).
        for name in caps:
            full = (await jetstream.stream_info(name)).state
            if name in buckets:
                revision = await buckets[name].put("one-more", b"x")
                assert revision == full.last_seq + 1, name
                with pytest.raises(KeyNotFoundError):
                    await buckets[name].get("fill0")
            else:
                acknowledgement = await jetstream.publish(_subject(split[name]), _fill(_subject(split[name])))
                assert (acknowledgement.stream, acknowledgement.seq) == (name, full.last_seq + 1)
            after = (await jetstream.stream_info(name)).state
            assert after.first_seq > full.first_seq and after.bytes <= caps[name], name

        # The account keeps room for the largest message on top of full streams.
        full = (await jetstream.stream_info("REC_player")).state
        acknowledgement = await jetstream.publish("player.record.asrun", b"L" * NODE_MAX_PAYLOAD)
        assert acknowledgement.seq == full.last_seq + 1
        assert (await jetstream.stream_info("REC_player")).state.first_seq > full.first_seq
        await client.close()

    _run(node, body)


def _subject(config) -> str:
    """A publish subject inside a stream's one wildcard subject."""
    return config.subjects[0].replace(">", "fill")


def _fill(subject: str) -> bytes:
    """A payload the store charges exactly FILL_CHARGE bytes on `subject` (no headers)."""
    return b"f" * (FILL_CHARGE - CHARGE - len(subject))


async def _record(jetstream) -> None:
    """Past its cap the oldest go, every publish is accepted, and first_seq advances by the drop."""
    cap = 16 * 1024
    await jetstream.add_stream(buffer(
        "REC_probe", cap, subjects=["probe.record.>"], retention=RetentionPolicy.LIMITS))
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
    await jetstream.add_stream(buffer(
        "OBS_probe", 16 * 1024, subjects=["probe.observation.>"], max_age=1,
        retention=RetentionPolicy.LIMITS))
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
    `history` values. A key beyond the list is accepted too: after the headroom's one, each costs
    the bucket its oldest message (a listed key's oldest value here, so no key is lost). An oversized value is refused (a message
    limit, not fullness); every listed key keeps its last good value."""
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

    # Keys beyond the list: every put accepted; once the headroom is spent the bucket's oldest goes
    # (here the first key's oldest value) and its first sequence moves past it.
    full = (await jetstream.stream_info(f"KV_{name}")).state
    strays = 0
    while (after := (await jetstream.stream_info(f"KV_{name}")).state).first_seq == full.first_seq:
        assert strays < 3, "a full bucket never dropped its oldest"
        assert await kv.put(f"unlisted{strays}", b"x" * max_value) == full.last_seq + 1 + strays
        strays += 1
    assert strays >= 2, "the headroom did not take the first stray without a drop"
    assert after.bytes <= capacity
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
