"""The Node bus's store budget on a real server (E3a-3, API8): the server itself refuses, at create,
a stream past the store limit, one without a byte cap and one in memory; with every buffer on the
Node full at once and the store wholly reserved, each still takes a write, a circular one dropping
its oldest and a sticky one keeping every document (the buffer rule, E-W1-BUF-2, E-W1-TD-4); each
retention class keeps the promise the Node API page states for it. Also: one class table declares
in any order and re-splits on a full store (E-W1-TD-S2); no reply for a stored message is past the
leaf (E-W1-TD-2), whatever its subject (E-W1-TD-6), and a reply that grows with a stream's state does
close it, which nodeapi never asks (recorded, E-W1-TD-9); a busy component is never cut off by a pull,
however many it runs at once (E-W1-TD-3, -7).

The Node runs the shipped `node-bus.conf`, unmodified. The split is the harness's `node_split` (the
page's numbers; E3b's class table owns them). Only the tests that cross the leaf start the hub;
elsewhere the Node's leaf retries in the background and nothing crosses it. Every stream, bucket
and subject is the test's.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import time

import nats.errors
import pytest
from integration.bus_servers import (
    MIB,
    BusServer,
    central,
    declare_bucket,
    declare_wall,
    desired_documents,
    hub_server,
    kv_bucket_bytes,
    leaf_connections,
    local,
    node_server,
    node_split,
    wall_value,
    wall_writer,
)
from nats.js.api import AckPolicy, ConsumerConfig, StorageType
from nats.js.errors import APIError, KeyNotFoundError

from contracts.node_link import (
    MAX_STORED_MESSAGE,
    NODE_DOMAIN,
    NODE_MAX_CONTROL_LINE,
    NODE_MAX_PAYLOAD,
    NODE_STORE_BYTES,
    WALL_STREAM,
    WALL_STREAM_BYTES,
)
from nodeapi.buffers import (
    HEADER_ALLOWANCE,
    MAX_PUBLISH_SUBJECT,
    STICKY,
    ClassTable,
    Documents,
    apply_table,
    bucket,
    buffer,
    buffer_kind,
    declare,
    declare_table,
    epoch_of,
    sticky_bucket,
    stream_epoch,
)
from nodeapi.documents import DocumentRefused, DocumentWriter, missing_documents
from nodeapi.pull import PULL_MAX_BYTES, pull

STORAGE_EXCEEDED = 10047        # JSStorageResourcesExceededErr: past the store's reservation
MAX_BYTES_REQUIRED = 10113      # JSStreamMaxBytesRequired: account API refuses an uncapped stream
MEMORY_EXCEEDED = 10028         # JSMemoryResourcesExceededErr: the server has no memory store
VALUE_TOO_LARGE = 10054         # JSStreamMessageExceedsMaximumErr: over max_value_size

STORE_LIMIT = NODE_STORE_BYTES  # node-bus.conf's max_file_store, the outer fence
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
        await _refused(declare(jetstream, dataclasses.replace(
            buffer("UNCAPPED", 1, subjects=["uncapped.>"]), max_bytes=None)), MAX_BYTES_REQUIRED)
        await _refused(declare(jetstream, dataclasses.replace(
            buffer("MEMORY", 64 * 1024, subjects=["memory.>"]), storage=StorageType.MEMORY)),
            MEMORY_EXCEEDED)

        # The page's split as one class table, its wall copy the real mirror, and one more buffer for
        # the rest of the store: the caps reserve the whole store, with no headroom anywhere.
        split = _whole_store()
        await declare_table(jetstream, split)
        split = split.buffers
        caps = {name: config.max_bytes for name, config in split.items()}
        assert sum(caps.values()) == STORE_LIMIT and caps["REST"] > 0
        await jetstream.publish("player.record.asrun", b"as-run 1")
        before = {name: (await jetstream.stream_info(name)) for name in caps}

        # The fence acts at create only: one more stream, or a raised cap, does not fit (10047).
        await _refused(declare(jetstream, buffer("EXTRA", MIB, subjects=["extra.>"])), STORAGE_EXCEEDED)
        raised = ClassTable({"REC_player": dataclasses.replace(before["REC_player"].config, max_bytes=5 * MIB)})
        await _refused(apply_table(jetstream, raised), STORAGE_EXCEEDED)

        # Nothing that was there moved.
        assert sorted(info.config.name for info in await jetstream.streams_info()) == sorted(caps)
        for name, cap in caps.items():
            info = await jetstream.stream_info(name)
            assert info.config.max_bytes == cap, name
            assert (info.state.messages, info.state.last_seq) == (
                before[name].state.messages, before[name].state.last_seq), name
        assert (await jetstream.get_last_msg("REC_player", "player.record.asrun")).data == b"as-run 1"

        # Fill the whole store: every circular stream and bucket holds exactly its cap; every sticky
        # one every document at its largest, `history` times, through its writer; WALL on the hub too,
        # and the Node's mirror with it.
        buckets, writers = {}, {}
        wall_table = _full_wall_table()
        for name, cap in caps.items():
            if name == WALL_STREAM:
                writers[name] = DocumentWriter(wall, wall_table)
                acknowledgement = await _fill_documents(writers[name])
                await _mirror_reaches(jetstream, acknowledgement)
            elif buffer_kind(split[name]) == STICKY:
                writers[name] = DocumentWriter(jetstream, desired_documents(name.removeprefix("KV_desired_")))
                await _fill_documents(writers[name])
            elif name.startswith("KV_"):
                buckets[name] = kv = await jetstream.key_value(name.removeprefix("KV_"))
                for index in range(cap // FILL_CHARGE):
                    await kv.put(f"fill{index}", _fill(f"$KV.{name.removeprefix('KV_')}.fill{index}"))
            else:
                subject = _subject(split[name])
                for index in range(cap // FILL_CHARGE):  # the last one takes the remainder too
                    last = index == cap // FILL_CHARGE - 1
                    await jetstream.publish(subject, _fill(subject, FILL_CHARGE + cap % FILL_CHARGE * last))
            held = (await jetstream.stream_info(name)).state.bytes
            assert held <= cap if name in writers else held == cap, name
        assert (await jetstream.account_info()).storage <= STORE_LIMIT

        # Every buffer full at once, each takes the next write at the next sequence: a circular one
        # drops its oldest, a hole below first_seq a reader counts (F7: never a refused write); a
        # sticky one drops only the written document's own oldest value and keeps every document.
        for name in caps:
            full = (await jetstream.stream_info(name)).state
            if name in writers:
                key = sorted(writers[name].table.sizes)[0]
                token = await writers[name].put(key, b"n" * writers[name].table.sizes[key])
                assert token.seq == full.last_seq + 1, name
                if name == WALL_STREAM:
                    await _mirror_reaches(jetstream, token.seq)
                    assert await wall_value(client, f"wall.{key}") == b"n" * wall_table.sizes[key]
                assert await missing_documents(jetstream, name, writers[name].table.subject_prefix) == set()
                assert len(await _held_keys(jetstream, name)) == len(writers[name].table.sizes) + 1, name
                continue
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

        # The largest stored message, into a full stream of a full store.
        full = (await jetstream.stream_info("REC_player")).state
        acknowledgement = await jetstream.publish("player.record.asrun", b"L" * MAX_STORED_MESSAGE)
        assert acknowledgement.seq == full.last_seq + 1
        assert (await jetstream.stream_info("REC_player")).state.first_seq > full.first_seq
        assert (await jetstream.account_info()).storage <= STORE_LIMIT
        await client.close()
        await writer.close()

    try:
        _run(node, body)
    finally:
        hub.stop()


def _whole_store() -> ClassTable:
    """The page's split plus a REST buffer for the rest of the store: the caps total the store."""
    split = node_split()
    rest = STORE_LIMIT - sum(config.max_bytes for config in split.buffers.values())
    return ClassTable({**split.buffers, "REST": buffer("REST", rest, subjects=["rest.>"])})


def _full_wall_table() -> Documents:
    """As many 4 KiB wall documents as WALL's budget holds."""
    def sizes(count: int) -> dict[str, int]:
        return {f"fill{index:03}": 4096 for index in range(count)}
    count = 1
    while Documents(WALL_STREAM, "wall.", sizes(count + 1)).budget <= WALL_STREAM_BYTES:
        count += 1
    return Documents.wall(sizes(count))


async def _fill_documents(writer: DocumentWriter) -> int:
    """Every document at its largest, `history` times; the last sequence written."""
    for round_ in range(writer.table.history):
        for key, size in writer.table.sizes.items():
            last = (await writer.put(key, bytes([97 + round_]) * size)).seq
    return last


async def _held_keys(jetstream, stream: str) -> set[str]:
    """The subjects the stream holds a message for."""
    info = await jetstream.stream_info(stream, subjects_filter=">")
    return set(info.state.subjects or {})


def _subject(config) -> str:
    """A publish subject inside a stream's one wildcard subject."""
    return config.subjects[0].replace(">", "fill")


def _fill(subject: str, charge: int = FILL_CHARGE) -> bytes:
    """A payload the store charges exactly `charge` bytes on `subject` (no headers)."""
    return b"f" * (charge - CHARGE - len(subject))


async def _record(jetstream) -> None:
    """Past its cap the oldest go, every publish is accepted, and first_seq advances by the drop."""
    cap = 16 * 1024
    await declare(jetstream, buffer("REC_probe", cap, subjects=["probe.record.>"]))
    origin = (await jetstream.stream_info("REC_probe")).config.first_seq
    published = 1000
    for index in range(published):
        acknowledgement = await jetstream.publish("probe.record.asrun", f"as-run {index:06}".encode() * 4)
        assert acknowledgement.seq == origin + index
    state = (await jetstream.stream_info("REC_probe")).state
    dropped = published - state.messages
    assert dropped > 0 and state.bytes <= cap
    assert (state.first_seq, state.last_seq) == (origin + dropped, origin + published - 1)
    config = (await jetstream.stream_info("REC_probe")).config
    assert (config.max_age or 0) == 0 and config.max_msgs_per_subject in (None, -1)


async def _observation(jetstream) -> None:
    """A message older than max_age is gone; a fresh one stays."""
    await declare(jetstream, buffer("OBS_probe", 16 * 1024, subjects=["probe.observation.>"], max_age=1))
    origin = (await jetstream.stream_info("OBS_probe")).config.first_seq
    published = time.monotonic()
    for index in range(3):
        await jetstream.publish("probe.observation.verdict", f"verdict {index}".encode())
    assert (await jetstream.stream_info("OBS_probe")).state.messages == 3
    deadline = published + 5
    while (state := (await jetstream.stream_info("OBS_probe")).state).messages:
        assert time.monotonic() < deadline, f"aged messages still held: {state}"
        await asyncio.sleep(.1)
    assert time.monotonic() - published >= 1
    assert state.first_seq == origin + 3
    await jetstream.publish("probe.observation.verdict", b"fresh")
    assert (await jetstream.get_last_msg("OBS_probe", "probe.observation.verdict")).data == b"fresh"


async def _state(jetstream) -> None:
    """A circular bucket (reported state). Listed keys, at up to max_value bytes and ten times the
    capacity, are always accepted and keep `history` values. A key beyond the list is accepted too:
    the bucket has no headroom, so the first costs the bucket its oldest message, a hole in its
    sequence. An oversized value is refused (a message limit, not fullness); every listed key keeps
    its last good value."""
    name, history, keys, max_value = "state_probe", 4, ("conditions", "position", "horizon", "verdicts"), 512
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


async def _desired(jetstream) -> None:
    """A sticky bucket (desired state, E-W1-TD-4). Its writer's listed documents, at up to their
    largest and ten times the budget, are always accepted and keep `history` values; a document
    written once stays. The writer refuses its own unlisted or oversize write before sending, so
    the stream never sees one; the reader finds nothing missing."""
    table = desired_documents("probe")
    await declare_bucket(jetstream, sticky_bucket(table))
    writer = DocumentWriter(jetstream, table)
    await writer.put("retention", b"written-once")
    keys = sorted(set(table.sizes) - {"retention"})
    written, put = 0, 0
    while written < 10 * table.budget:
        key = keys[put % len(keys)]
        size = table.sizes[key] if put % 3 else table.sizes[key] // 3
        await writer.put(key, f"{put:08}".encode().ljust(size, b"."))
        written, put = written + size, put + 1
    state = (await jetstream.stream_info(table.stream)).state
    for unlisted, value in (("unlisted", b"x"), ("show", b"x" * (table.sizes["show"] + 1))):
        with pytest.raises(DocumentRefused):
            await writer.put(unlisted, value)
    assert (await jetstream.stream_info(table.stream)).state.last_seq == state.last_seq
    assert state.bytes <= table.budget
    assert (await writer.read("retention"))[0] == b"written-once"
    kv = await jetstream.key_value(table.stream.removeprefix("KV_"))
    for key in keys:
        assert len(await kv.history(key)) == table.history, key
    assert await missing_documents(jetstream, table.stream, table.subject_prefix) == set()


@pytest.mark.parametrize("retention_class", ["record", "observation", "state", "desired"])
def test_each_retention_class_keeps_its_promise(tmp_path, retention_class):
    node = _node(tmp_path)

    async def body():
        client = await local(node)
        jetstream = client.jetstream()
        await {"record": _record, "observation": _observation, "state": _state,
               "desired": _desired}[retention_class](jetstream)
        await client.close()

    _run(node, body)


def test_one_class_table_declares_in_any_order_and_resplits_a_wholly_reserved_store(tmp_path):
    # One owner declares the whole table; its caps total the store, so whichever buffer comes last
    # is never the one refused (10047), and Central's override moves bytes inside that total on a
    # live store, every shrink before any growth (E-W1-TD-S2).
    node = _node(tmp_path)

    async def body():
        client = await local(node)
        jetstream = client.jetstream()
        table = _whole_store()
        await declare_table(jetstream, ClassTable(dict(reversed(list(table.buffers.items())))))
        await declare_table(jetstream, table)   # every connect re-declares: nothing changes
        epochs = {name: epoch_of(await jetstream.stream_info(name)) for name in table.buffers}
        caps = {name: config.max_bytes for name, config in table.buffers.items()}
        assert sum(caps.values()) == STORE_LIMIT

        moved = table.resplit({"REC_player": caps["REC_player"] - MIB, "REST": caps["REST"] + MIB})
        await apply_table(jetstream, moved)
        for name, config in moved.buffers.items():
            info = await jetstream.stream_info(name)
            assert (info.config.max_bytes, epoch_of(info)) == (config.max_bytes, epochs[name]), name
        await apply_table(jetstream, table)
        assert {name: (await jetstream.stream_info(name)).config.max_bytes for name in caps} == caps
        await client.close()

    _run(node, body)


def test_no_reply_for_a_stored_message_is_past_the_leaf(tmp_path):
    # A reply the Node generates for a stored message (a JSON get base64-encodes it, a direct get and
    # a delivery add headers) crossing the leaf past L would close it; every stream stores at most
    # MAX_STORED_MESSAGE, so none does, and Central reads the largest every way (E-W1-TD-2).
    hub = hub_server(tmp_path, ["serial-a"])
    node = node_server(tmp_path, "serial-a", hub)
    hub.start()

    async def body():
        async def linked():
            return len(leaf_connections(hub)) == 1
        await _until(linked, 10, "the Node's leaf link")
        before = leaf_connections(hub)
        client = await local(node)
        jetstream = client.jetstream()
        await declare(jetstream, buffer("REC_player", MIB, subjects=["player.record.>"], allow_direct=True))
        table = Documents.bucket("desired_big", {"show": MAX_STORED_MESSAGE - 512}, history=1)
        await declare_bucket(jetstream, sticky_bucket(table))
        for size in (MAX_STORED_MESSAGE + 1, NODE_MAX_PAYLOAD - 1024):
            await _refused(jetstream.publish("player.record.asrun", b"x" * size), VALUE_TOO_LARGE)
        await jetstream.publish("player.record.asrun", b"x" * MAX_STORED_MESSAGE)
        await DocumentWriter(jetstream, table).put("show", b"s" * table.sizes["show"], headers={"Writer": "node"})

        central_client = await central(hub, "serial-a")
        across = central_client.jetstream(domain=NODE_DOMAIN)
        for direct in (False, True):
            message = await across.get_last_msg("REC_player", "player.record.asrun", direct=direct)
            assert len(message.data) == MAX_STORED_MESSAGE, direct
        assert len((await (await across.key_value("desired_big")).get("show")).value) == table.sizes["show"]
        await across.add_consumer("REC_player", ConsumerConfig(durable_name="central", ack_policy=AckPolicy.EXPLICIT))
        [delivered] = await pull(central_client, "REC_player", "central", 1, timeout=2, domain=NODE_DOMAIN)
        assert len(delivered.data) == MAX_STORED_MESSAGE
        await delivered.ack_sync()
        assert leaf_connections(hub) == before
        assert "maximum payload" not in (hub.log_tail(400) + node.log_tail(400)).lower()
        await central_client.close()
        await client.close()

    try:
        _run(node, body)
    finally:
        hub.stop()


def test_no_reply_for_the_longest_subject_a_client_can_store_is_past_the_leaf(tmp_path):
    # A reply naming a stored message repeats its subject, JSON-escaped at six bytes for `<` or `&`,
    # so a long subject on the largest record once closed the leaf. Both servers' max_control_line
    # (the server's default, pinned) bounds every stored subject: the longest line a Node component or
    # Central sends is stored, one byte more closes only that client, and Central reads the largest record under each worst-escaping
    # longest subject, and the largest document under the longest key a writer admits, every way that
    # carries a subject back across the leaf, with the leaf kept (E-W1-TD-6).
    hub = hub_server(tmp_path, ["serial-a"])
    node = node_server(tmp_path, "serial-a", hub)
    hub.start()

    async def body():
        async def linked():
            return len(leaf_connections(hub)) == 1
        await _until(linked, 10, "the Node's leaf link")
        before = leaf_connections(hub)
        client = await local(node)
        jetstream = client.jetstream()
        await declare(jetstream, buffer("REC_player", MIB, subjects=["player.record.>"]))
        record = b"x" * MAX_STORED_MESSAGE
        # nats-py's line for a publish with no reply: PUB, the subject, two spaces and the size.
        longest = NODE_MAX_CONTROL_LINE - len(f"  {len(record)}") - len("player.record.")
        subjects = []
        for connect, escaped in ((lambda: local(node), "<"), (lambda: central(hub, "serial-a"), "&")):
            subject = "player.record." + escaped * longest
            past = await connect()
            await past.publish(subject + escaped, record)

            async def closed(past=past):
                return past.is_closed
            await _until(closed, 5, f"the server closes the client past the control line ({escaped})")
            writer = await connect()
            await writer.publish(subject, record)
            await writer.flush(2)
            await writer.close()
            subjects.append(subject)

        async def stored():
            return (await jetstream.stream_info("REC_player")).state.messages == len(subjects)
        await _until(stored, 5, "both longest-subject records are stored, neither past one")
        # A component's KV put under a 1004-byte key, which a lowered control line once answered by
        # closing the component: the server's default line takes it and keeps the client (E-W1-TD-8).
        state = await declare_bucket(jetstream, bucket("state_host", history=1, max_bytes=MIB))
        await state.put("k" * 1004, b"kept")
        assert (await state.get("k" * 1004)).value == b"kept" and client.is_connected
        bucket_name = "desired_long"
        key = "<" * (MAX_PUBLISH_SUBJECT - len(f"$KV.{bucket_name}."))
        table = Documents.bucket(bucket_name, {key: MAX_STORED_MESSAGE - HEADER_ALLOWANCE}, history=1)
        await declare_bucket(jetstream, sticky_bucket(table))
        await DocumentWriter(jetstream, table).put(key, b"d" * table.sizes[key], headers={"Writer": "node"})

        central_client = await central(hub, "serial-a")
        across = central_client.jetstream(domain=NODE_DOMAIN)
        for subject in subjects:
            assert len((await across.get_last_msg("REC_player", subject)).data) == MAX_STORED_MESSAGE
        document = await across.get_last_msg(table.stream, table.subject_prefix + key)
        assert len(document.data) == table.sizes[key]
        await across.add_consumer("REC_player", ConsumerConfig(durable_name="central", ack_policy=AckPolicy.EXPLICIT))
        delivered = await pull(central_client, "REC_player", "central", 2, timeout=2, domain=NODE_DOMAIN)
        assert [message.subject for message in delivered] == subjects
        for message in delivered:
            await message.ack_sync()
        assert leaf_connections(hub) == before
        assert "maximum payload" not in (hub.log_tail(400) + node.log_tail(400)).lower()
        await central_client.close()
        await client.close()

    try:
        _run(node, body)
    finally:
        hub.stop()


def test_a_busy_component_is_never_cut_off_by_a_large_pull(tmp_path):
    # A component whose loop is busy (GC, a long frame, a blocking call) for longer than the write
    # deadline while sixteen of the largest records come due: the server queues at most one capped
    # pull for it and retries its writes, so the component keeps its connection and gets every
    # record (E-W1-TD-3).
    node = _node(tmp_path)

    async def body():
        writer = await local(node)
        jetstream = writer.jetstream()
        await declare(jetstream, buffer("REC_player", 8 * MIB, subjects=["player.record.>"]))
        for _ in range(16):
            await jetstream.publish("player.record.asrun", b"r" * MAX_STORED_MESSAGE)
        reader = await local(node)
        await reader.jetstream().add_consumer("REC_player", ConsumerConfig(
            durable_name="component", ack_policy=AckPolicy.EXPLICIT))
        pending = asyncio.ensure_future(pull(reader, "REC_player", "component", 16, timeout=8))
        await asyncio.sleep(.05)   # the pull request is out
        time.sleep(3)              # the loop is busy past write_deadline ("2s")
        received = await pending
        assert received and sum(len(message.data) for message in received) <= PULL_MAX_BYTES
        while len(received) < 16:
            more = await pull(reader, "REC_player", "component", 16, timeout=2)
            assert more, f"{len(received)} of 16 records"
            received += more
        for message in received:
            await message.ack_sync()
        assert reader.is_connected and not reader.is_closed
        assert len(received) == 16
        assert "Slow Consumer" not in node.log_tail(400)
        await reader.close()
        await writer.close()

    _run(node, body)


def test_concurrent_pulls_on_one_busy_connection_share_one_byte_budget(tmp_path):
    # Four tasks of one component pull four streams of the largest records on one connection while
    # its loop is busy past the write deadline. Each request alone is capped, but a connection's open
    # requests add up past max_pending, so the cap is the connection's: the pulls queue for one
    # budget, and the component keeps its connection and gets every record (E-W1-TD-7).
    node = _node(tmp_path)
    streams = [f"REC_part{index}" for index in range(4)]

    async def body():
        writer = await local(node)
        jetstream = writer.jetstream()
        for stream in streams:
            await declare(jetstream, buffer(stream, 2 * MIB, subjects=[f"{stream.lower()}.>"]))
            for _ in range(6):
                await jetstream.publish(f"{stream.lower()}.asrun", b"r" * MAX_STORED_MESSAGE)
        reader = await local(node)
        for stream in streams:
            await reader.jetstream().add_consumer(stream, ConsumerConfig(
                durable_name="component", ack_policy=AckPolicy.EXPLICIT))
        pending = [asyncio.ensure_future(pull(reader, stream, "component", 6, timeout=8)) for stream in streams]
        await asyncio.sleep(.05)   # the pulls have started
        time.sleep(3)              # the loop is busy past write_deadline ("2s")
        received = {stream: await task for stream, task in zip(streams, pending, strict=True)}
        for stream in streams:
            assert received[stream], stream
            while len(received[stream]) < 6:
                more = await pull(reader, stream, "component", 6, timeout=2)
                assert more, f"{stream}: {len(received[stream])} of 6 records"
                received[stream] += more
            for message in received[stream]:
                await message.ack_sync()
        assert reader.is_connected and not reader.is_closed
        assert "Slow Consumer" not in node.log_tail(400)
        await reader.close()
        await writer.close()

    _run(node, body)


def test_a_reply_that_grows_with_stream_state_closes_the_leaf_and_nodeapi_asks_none(tmp_path):
    # Recorded, not fixed (E-W1-TD-9). MAX_STORED_MESSAGE bounds every reply about one stored message,
    # not one that grows with a stream's state: Central's STREAM.INFO across the leaf asking for every
    # interior delete of a hot state bucket, or every subject of a record stream, passes L, and the
    # hub closes the leaf for it. nodeapi's own STREAM.INFO (the epoch read) asks neither and keeps the
    # leaf. The day this fails, such replies are bounded or fenced: update contracts.node_link with it.
    hub = hub_server(tmp_path, ["serial-a"])
    node = node_server(tmp_path, "serial-a", hub)
    hub.start()

    async def body():
        async def linked():
            return leaf_connections(hub)
        await _until(linked, 10, "the Node's leaf link")
        client = await local(node)
        jetstream = client.jetstream()
        await declare(jetstream, bucket("state_host", history=1, max_bytes=MIB))
        await declare(jetstream, buffer("REC_host", 4 * MIB, subjects=["host.record.>"]))
        await jetstream.publish("$KV.state_host.pinned", b"pinned")
        for _ in range(60_000):     # each write deletes the hot key's last value below the newest
            await client.publish("$KV.state_host.hot", b"h")
        for index in range(12_000):  # a distinct subject per record
            await client.publish(f"host.record.{index:010}.{'s' * 16}", b"r")
        await jetstream.publish("$KV.state_host.hot", b"h")   # acknowledged after every write before it
        await jetstream.publish("host.record.last", b"r")

        central_client = await central(hub, "serial-a")
        across = central_client.jetstream(domain=NODE_DOMAIN)
        for stream, details in (("KV_state_host", {"deleted_details": True}), ("REC_host", {"subjects_filter": ">"})):
            before = await _until(linked, 10, "the Node's leaf link")

            async def epoch_read(stream=stream):
                try:
                    return await stream_epoch(across, stream)
                except (nats.errors.NoRespondersError, nats.errors.TimeoutError):
                    return None
            await _until(epoch_read, 10, f"nodeapi's epoch read of {stream} across the leaf")
            assert leaf_connections(hub) == before
            request = json.dumps(details).encode()
            assert len((await client.request(f"$JS.API.STREAM.INFO.{stream}", request, timeout=5)).data) > NODE_MAX_PAYLOAD
            with pytest.raises(nats.errors.TimeoutError):
                await central_client.request(f"$JS.{NODE_DOMAIN}.API.STREAM.INFO.{stream}", request, timeout=2)

            async def relinked(before=before):
                now = leaf_connections(hub)
                return now and now != before
            await _until(relinked, 10, f"the leaf closed for {stream} and relinked")
        assert hub.log_tail(400).count("Leafnode connection closed: Maximum Message Payload Exceeded") >= 2
        await central_client.close()
        await client.close()

    try:
        _run(node, body)
    finally:
        hub.stop()
