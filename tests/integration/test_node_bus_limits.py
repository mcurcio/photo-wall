"""The Node bus's store budget on a real server (E3a-3, API8): the server itself refuses, at create,
a stream past the store limit, an 18th stream and one without a byte cap; with every store line full
at once and the store at its 17 streams, each buffer still takes a write, a circular one dropping its
oldest and a sticky one keeping every key (the buffer rule, E-W1-BUF-2, E-W1-TD-4); each retention
class keeps the promise the Node API page states for it. Also: applying the full store again, or two
applies racing, is a no-op, a racing create included (E-W1-STORE-1, E-W1-FIT-1); no reply for a stored
message is past the leaf (E-W1-TD-2), whatever its subject (E-W1-TD-6), and a reply that grows with a
stream's state does close it, which nodeapi never asks (recorded, E-W1-TD-9); a busy component is
never cut off by a pull, however many it runs at once (E-W1-TD-3, -7).

The Node runs the shipped `node-bus.conf`, unmodified. The store is the harness's `line_slices`: every
store line at its full bytes and streams (E3b design §7.3), with the WALL mirror. Only the tests that
cross the leaf start the hub; elsewhere the Node's leaf retries in the background and nothing crosses
it. Every stream, bucket and subject is the test's.
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
    hub_server,
    leaf_connections,
    line_slices,
    local,
    node_server,
    until,
    wall_value,
    wall_writer,
)
from nats.js.api import AckPolicy, ConsumerConfig
from nats.js.errors import APIError

from contracts.node_link import (
    MAX_STORED_MESSAGE,
    NODE_DOMAIN,
    NODE_MAX_CONTROL_LINE,
    NODE_MAX_PAYLOAD,
    NODE_MAX_STREAMS,
    NODE_STORE_BYTES,
    STORE_LINES,
    WALL_STREAM,
    WALL_STREAM_BYTES,
)
from nodeapi.buffers import (
    HEADER_ALLOWANCE,
    MAX_PUBLISH_SUBJECT,
    WALL_PREFIX,
    KeyTable,
    Role,
    Slice,
    apply,
    declare,
    desired_bucket,
    event_buffer,
    role_of,
    state_bucket,
    table_of,
    wall_mirror_config,
)
from nodeapi.documents import ABSENT, DocumentRefused, DocumentWriter
from nodeapi.epoch import epoch_of, stream_epoch
from nodeapi.pull import PULL_MAX_BYTES, pull

MEMORY_EXCEEDED = 10028         # JSMemoryResourcesExceededErr: past the memory store's reservation
MAX_STREAMS_REACHED = 10027     # JSMaximumStreamsLimitErr: past node-bus.conf's max_streams
MAX_BYTES_REQUIRED = 10113      # JSStreamMaxBytesRequired: account API refuses an uncapped stream
VALUE_TOO_LARGE = 10054         # JSStreamMessageExceedsMaximumErr: over max_value_size

STORE_LIMIT = NODE_STORE_BYTES  # node-bus.conf's max_memory_store, the outer fence
# Every store line and the WALL mirror: 11.5 of the store's 12 MiB (E3b design §7.3).
FULL_STORE = sum(line.max_bytes for line in STORE_LINES.values()) + WALL_STREAM_BYTES
# nats-server's per-message memory-store charge: 16 + subject + headers + payload
# (ns:server/memstore.go:2511-2513).
CHARGE = 16
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


async def _mirror_reaches(jetstream, seq: int):
    async def check():
        state = (await jetstream.stream_info(WALL_STREAM)).state
        return state if state.last_seq >= seq else None
    return await until(check, 10, f"the wall mirror reaches {seq}")


def test_with_every_node_buffer_full_each_still_takes_a_write(tmp_path):
    hub = hub_server(tmp_path, ["serial-a"])
    node = node_server(tmp_path, "serial-a", hub)
    hub.start()

    async def body():
        async def linked():
            return len(leaf_connections(hub)) == 1
        await until(linked, 10, "the Node's leaf link")
        writer = await wall_writer(hub)
        wall_table = _full_wall_table()
        await declare_wall(writer, wall_table)
        client = await local(node)
        jetstream = client.jetstream()
        assert client.max_payload == NODE_MAX_PAYLOAD  # the shipped file's pin, as the server applies it

        # The server, not a library convention, refuses at create an uncapped stream.
        await _refused(declare(jetstream, dataclasses.replace(
            event_buffer("host", "uncapped", 1), max_bytes=None)), MAX_BYTES_REQUIRED)

        # Every store line at its full bytes and streams, the wall copy the real mirror: the store at
        # its 17 streams, every line's bytes reserved.
        split = await _full_store(jetstream)
        caps = {name: config.max_bytes for name, config in split.items()}
        assert sum(caps.values()) == FULL_STORE and len(caps) == NODE_MAX_STREAMS
        await jetstream.publish("player.record.asrun", b"as-run 1")
        before = {name: (await jetstream.stream_info(name)) for name in caps}

        # The fence acts at create only: one more stream does not fit (10027, the stream count).
        await _refused(declare(jetstream, event_buffer("host", "extra", MIB)), MAX_STREAMS_REACHED)

        # Nothing that was there moved.
        assert sorted(info.config.name for info in await jetstream.streams_info()) == sorted(caps)
        for name, cap in caps.items():
            info = await jetstream.stream_info(name)
            assert info.config.max_bytes == cap, name
            assert (info.state.messages, info.state.last_seq) == (
                before[name].state.messages, before[name].state.last_seq), name
        assert (await jetstream.get_last_msg("RECORD_player", "player.record.asrun")).data == b"as-run 1"

        # Fill the whole store: every circular stream holds exactly its cap; every sticky one every
        # key at its largest, `history` times (documents through their writer, state as a session
        # puts it); WALL on the hub too, and the Node's mirror with it.
        states, writers, tokens = {}, {}, {}
        for name, cap in caps.items():
            if name == WALL_STREAM:
                writers[name] = await DocumentWriter.bind(writer, WALL_STREAM, writer="central")
                assert writers[name].table == wall_table
                tokens[name] = await _fill_documents(writers[name])
                await _mirror_reaches(jetstream, max(token.seq for token in tokens[name].values()))
            elif role_of(split[name]) is Role.DESIRED:
                writers[name] = await DocumentWriter.bind(client, name, writer="node")
                tokens[name] = await _fill_documents(writers[name])
            elif role_of(split[name]) is Role.STATE:
                states[name] = table_of(split[name])
                for round_ in range(states[name].history):
                    for key, size in states[name].sizes.items():
                        await jetstream.publish(_key_subject(split[name], key), bytes([97 + round_]) * size)
            else:
                subject = _subject(split[name])
                for index in range(cap // FILL_CHARGE):  # the last one takes the remainder too
                    last = index == cap // FILL_CHARGE - 1
                    await jetstream.publish(subject, _fill(subject, FILL_CHARGE + cap % FILL_CHARGE * last))
            held = (await jetstream.stream_info(name)).state.bytes
            assert held <= cap if name in writers or name in states else held == cap, name
        assert (await jetstream.account_info()).memory <= STORE_LIMIT

        # Every buffer full at once, each takes the next write at the next sequence: a circular one
        # drops its oldest, a hole below first_seq a reader counts (F7: never a refused write); a
        # sticky one drops only the written document's own oldest value and keeps every document.
        for name in caps:
            full = (await jetstream.stream_info(name)).state
            if name in writers:
                key = sorted(writers[name].table.sizes)[0]
                token = await writers[name].put(key, b"n" * writers[name].table.sizes[key], expect=tokens[name][key])
                assert token.seq == full.last_seq + 1, name
                for listed in writers[name].table.sizes:
                    assert await writers[name].read(listed) is not None, (name, listed)
                if name == WALL_STREAM:
                    await _mirror_reaches(jetstream, token.seq)
                    assert await wall_value(client, f"wall.{key}") == b"n" * wall_table.sizes[key]
                assert len(await _held_keys(jetstream, name)) == len(writers[name].table.sizes), name
                continue
            if name in states:
                key = sorted(states[name].sizes)[0]
                acknowledgement = await jetstream.publish(_key_subject(split[name], key), b"n" * states[name].sizes[key])
                assert acknowledgement.seq == full.last_seq + 1, name
                assert len(await _held_keys(jetstream, name)) == len(states[name].sizes), name
                continue
            else:
                acknowledgement = await jetstream.publish(_subject(split[name]), _fill(_subject(split[name])))
                assert (acknowledgement.stream, acknowledgement.seq) == (name, full.last_seq + 1)
            after = (await jetstream.stream_info(name)).state
            assert after.first_seq > full.first_seq and after.bytes <= caps[name], name

        # The largest stored message, into a full stream of a full store.
        full = (await jetstream.stream_info("RECORD_player")).state
        acknowledgement = await jetstream.publish("player.record.asrun", b"L" * MAX_STORED_MESSAGE)
        assert acknowledgement.seq == full.last_seq + 1
        assert (await jetstream.stream_info("RECORD_player")).state.first_seq > full.first_seq
        assert (await jetstream.account_info()).memory <= STORE_LIMIT
        await client.close()
        await writer.close()

    try:
        _run(node, body)
    finally:
        hub.stop()


async def _full_store(jetstream, slices: dict[str, Slice] | None = None) -> dict:
    """Every store line applied at its full bytes and streams (`line_slices`) and the WALL mirror:
    the store at its 17 streams, the server's most (E-W1-FIT-1). Stream name -> configuration."""
    slices = slices or line_slices()
    for slice_ in slices.values():
        await apply(jetstream, slice_)
    await declare(jetstream, wall_mirror_config())
    return {**{config.name: config for slice_ in slices.values() for config in slice_.buffers},
            WALL_STREAM: wall_mirror_config()}


def _key_subject(config, key: str) -> str:
    return config.subjects[0].removesuffix(">") + key


def _full_wall_table() -> KeyTable:
    """As many 4 KiB wall documents as WALL's budget holds."""
    def sizes(count: int) -> dict[str, int]:
        return {f"fill{index:03}": 4096 for index in range(count)}
    count = 1
    while KeyTable(sizes(count + 1)).budget(WALL_PREFIX) <= WALL_STREAM_BYTES:
        count += 1
    return KeyTable(sizes(count))


async def _fill_documents(writer: DocumentWriter) -> dict:
    """Every document at its largest, `history` times; each key's last token."""
    tokens: dict = {}
    for round_ in range(writer.table.history):
        for key, size in writer.table.sizes.items():
            tokens[key] = await writer.put(key, bytes([97 + round_]) * size, expect=tokens.get(key, ABSENT))
    return tokens


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


async def _record(client) -> None:
    """Past its cap the oldest go, every publish is accepted, and first_seq advances by the drop."""
    jetstream = client.jetstream()
    await apply(jetstream, line_slices()["host"])
    config = (await jetstream.stream_info("RECORD_host")).config
    origin, published = config.first_seq, 2 * config.max_bytes // 4096
    for index in range(published):
        acknowledgement = await jetstream.publish("host.record.asrun", f"as-run {index:06}".encode().ljust(4096, b"."))
        assert acknowledgement.seq == origin + index
    state = (await jetstream.stream_info("RECORD_host")).state
    dropped = published - state.messages
    assert dropped > 0 and state.bytes <= config.max_bytes
    assert (state.first_seq, state.last_seq) == (origin + dropped, origin + published - 1)
    assert (config.max_age or 0) == 0 and config.max_msgs_per_subject in (None, -1)


async def _observation(client) -> None:
    """A message older than max_age is gone; a fresh one stays. The health line with its observations
    kept one second, not six hours."""
    jetstream = client.jetstream()
    line = line_slices()["health"]
    observations = event_buffer("health", "observation", line.buffers[0].max_bytes, max_age=1)
    await apply(jetstream, Slice("health", (observations, *line.buffers[1:])))
    origin = (await jetstream.stream_info("OBSERVATION_health")).config.first_seq
    published = time.monotonic()
    for index in range(3):
        await jetstream.publish("health.observation.verdict", f"verdict {index}".encode())
    assert (await jetstream.stream_info("OBSERVATION_health")).state.messages == 3
    deadline = published + 5
    while (state := (await jetstream.stream_info("OBSERVATION_health")).state).messages:
        assert time.monotonic() < deadline, f"aged messages still held: {state}"
        await asyncio.sleep(.1)
    assert time.monotonic() - published >= 1
    assert state.first_seq == origin + 3
    await jetstream.publish("health.observation.verdict", b"fresh")
    assert (await jetstream.get_last_msg("OBSERVATION_health", "health.observation.verdict")).data == b"fresh"


async def _state(client) -> None:
    """A sticky state bucket (E3b design §12 change 4). Its listed keys, at up to their largest and
    ten times the budget, are always accepted and keep `history` values: no key loses its only value.
    A value past the table's largest message is refused (a message limit, not fullness); every key
    keeps its last good value. The session refuses an unlisted key before sending."""
    jetstream = client.jetstream()
    line = line_slices()["display"]
    await apply(jetstream, line)
    [config] = [config for config in line.buffers if role_of(config) is Role.STATE]
    table, keys = table_of(config), sorted(table_of(config).sizes)

    # Sizes vary up to each key's largest, and each key in turn lags the others with one short value.
    last: dict[str, bytes] = {}
    written, put = 0, 0
    while written < 10 * config.max_bytes:
        key = keys[put % len(keys)]
        size = table.sizes[key] if (put // len(keys) + keys.index(key)) % (table.history + 1) else table.sizes[key] // 3
        value = f"{put:08}".encode().ljust(size, b".")
        await jetstream.publish(_key_subject(config, key), value)
        last[key], written, put = value, written + size, put + 1
    assert (await jetstream.stream_info(config.name)).state.bytes <= config.max_bytes
    prefix = _key_subject(config, "")
    held = (await jetstream.stream_info(config.name, subjects_filter=f"{prefix}>")).state.subjects
    for key in keys:
        assert held[prefix + key] == table.history, key
    with pytest.raises(APIError) as oversized:
        await jetstream.publish(_key_subject(config, keys[0]), b"x" * (config.max_msg_size + 1))
    assert oversized.value.err_code == VALUE_TOO_LARGE, oversized.value
    for key in keys:
        assert (await jetstream.get_last_msg(config.name, prefix + key)).data == last[key], key


async def _desired(client) -> None:
    """A sticky bucket (desired state, E-W1-TD-4). Its writer's listed documents, at up to their
    largest and ten times the budget, are always accepted and keep `history` values; a document
    written once stays. The writer refuses its own unlisted or oversize write before sending, so
    the stream never sees one; every listed document is held."""
    jetstream = client.jetstream()
    line = line_slices()["apps"]
    await apply(jetstream, line)
    [config] = [config for config in line.buffers if role_of(config) is Role.DESIRED]
    table = table_of(config)
    writer = await DocumentWriter.bind(client, config.name, writer="node")
    tokens = {"layout": await writer.put("layout", b"written-once", expect=ABSENT)}
    keys = sorted(set(table.sizes) - {"layout"})
    written, put = 0, 0
    while written < 10 * config.max_bytes:
        key = keys[put % len(keys)]
        size = table.sizes[key] if put % 3 else table.sizes[key] // 3
        tokens[key] = await writer.put(key, f"{put:08}".encode().ljust(size, b"."), expect=tokens.get(key, ABSENT))
        written, put = written + size, put + 1
    state = (await jetstream.stream_info(config.name)).state
    for unlisted, value in (("unlisted", b"x"), ("show", b"x" * (table.sizes["show"] + 1))):
        with pytest.raises(DocumentRefused):
            await writer.put(unlisted, value, expect=tokens.get(unlisted, ABSENT))
    assert (await jetstream.stream_info(config.name)).state.last_seq == state.last_seq
    assert state.bytes <= config.max_bytes
    assert (await writer.read("layout")).value == b"written-once"
    # One stream info counts every key's values: a kv.history per key holds a consumer for 5 minutes,
    # and 22 of them pass the server's per-stream consumer cap (10026, erratum E-W1-CONS-2).
    prefix = _key_subject(config, "")
    held = (await jetstream.stream_info(config.name, subjects_filter=f"{prefix}>")).state.subjects
    for key in keys:
        assert held[prefix + key] == table.history, key
    for key in table.sizes:
        assert await writer.read(key) is not None, key


@pytest.mark.parametrize("retention_class", ["record", "observation", "state", "desired"])
def test_each_retention_class_keeps_its_promise(tmp_path, retention_class):
    node = _node(tmp_path)

    async def body():
        client = await local(node)
        await {"record": _record, "observation": _observation, "state": _state,
               "desired": _desired}[retention_class](client)
        await client.close()

    _run(node, body)


def test_the_server_refuses_an_eighteenth_stream_and_a_stream_past_the_store(tmp_path):
    # The fit's two server limits act at create only (E-W1-FIT-1): on a store at its 17 streams an
    # 18th is refused for the count (10027) and every stream still takes writes; with one stream gone,
    # a stream past the room left is refused for the store (10028), and one inside it is created.
    node = _node(tmp_path)

    async def body():
        client = await local(node)
        jetstream = client.jetstream()
        caps = {name: config.max_bytes for name, config in (await _full_store(jetstream)).items()}
        assert len(await jetstream.streams_info()) == NODE_MAX_STREAMS
        await _refused(declare(jetstream, event_buffer("host", "extra", 1024)), MAX_STREAMS_REACHED)
        for index in range(caps["RECORD_player"] // FILL_CHARGE + 8):
            acknowledgement = await jetstream.publish("player.record.fill", _fill("player.record.fill"))
            assert acknowledgement.stream == "RECORD_player", index
        assert (await jetstream.stream_info("RECORD_player")).state.bytes <= caps["RECORD_player"]

        room = STORE_LIMIT - FULL_STORE + caps["RECORD_host"]
        await jetstream.delete_stream("RECORD_host")
        await _refused(declare(jetstream, event_buffer("host", "extra", room + 1)), MEMORY_EXCEEDED)
        assert await declare(jetstream, event_buffer("host", "extra", room)) is True
        await _refused(declare(jetstream, line_slices()["host"].buffers[0]), MAX_STREAMS_REACHED)
        await client.close()

    _run(node, body)


def test_two_applies_of_the_full_store_racing_are_no_ops(tmp_path):
    # Every attach applies its slice, and a Node's store is full. Applying the full store again, from
    # the same build or another, or two applies at once, changes nothing and refuses nothing: no
    # delete, purge, update or create is sent. The server checks a create's reservation
    # (ns:server/jetstream_api.go:1615) before it looks for the name (ns:server/stream.go:891), so a
    # create that loses a race on a full store is 10028, not 10058: the loser's apply is a no-op
    # too (E-W1-STORE-1).
    node = _node(tmp_path)

    async def body():
        client = await local(node)
        jetstream = client.jetstream()
        mine, theirs = line_slices(), line_slices()   # two declarers, each with its own build
        names = await _full_store(jetstream, mine)
        epochs = {name: epoch_of(await jetstream.stream_info(name)) for name in names}

        calls = _Changes(jetstream)
        for slices in (mine, theirs, mine):
            for slice_ in slices.values():
                assert await apply(calls, slice_) == {config.name: epochs[config.name] for config in slice_.buffers}
        await asyncio.gather(*(apply(calls, slice_) for slice_ in [*mine.values(), *theirs.values()]))
        assert calls.sent == []
        assert {name: epoch_of(await jetstream.stream_info(name)) for name in names} == epochs
        assert sorted(info.config.name for info in await jetstream.streams_info()) == sorted(names)

        # The race's worst order, on the same full store less one stream: they look and find it
        # absent, I create it, then their create meets the full reservation. Theirs is a no-op, not a
        # refusal, and mine stays.
        racing = "RECORD_player"
        await jetstream.delete_stream(racing)
        created = []

        async def i_create() -> None:
            created.append((await apply(jetstream, mine["player"]))[racing])
        assert (await apply(_LooksBefore(jetstream, racing, i_create), theirs["player"]))[racing] == created[0]
        assert epoch_of(await jetstream.stream_info(racing)) == created[0]

        # A stream that is absent still meets the full store's refusal (its 17 streams): the no-op
        # covers only a stream that exists.
        await _refused(declare(jetstream, event_buffer("host", "extra", MIB)), MAX_STREAMS_REACHED)
        await client.close()

    _run(node, body)


class _Changes:
    """A JetStream that records every call that would change the store; every call goes to the real
    server."""

    def __init__(self, jetstream) -> None:
        self._jetstream, self.sent = jetstream, []

    def __getattr__(self, name: str):
        if name in ("add_stream", "update_stream", "delete_stream", "purge_stream"):
            self.sent.append(name)
        return getattr(self._jetstream, name)


class _LooksBefore:
    """A declarer's JetStream whose first lookup of `stream` returns only after `meanwhile` has run:
    another declarer creates the stream between this one's look and its create. Every call goes to
    the real server; only the order is fixed."""

    def __init__(self, jetstream, stream: str, meanwhile) -> None:
        self._jetstream, self._stream, self._meanwhile = jetstream, stream, meanwhile

    def __getattr__(self, name: str):
        return getattr(self._jetstream, name)

    async def stream_info(self, name: str, *args, **kwargs):
        try:
            return await self._jetstream.stream_info(name, *args, **kwargs)
        finally:
            meanwhile = self._meanwhile if name == self._stream else None
            if meanwhile is not None:
                self._meanwhile = None
                await meanwhile()


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
        await until(linked, 10, "the Node's leaf link")
        before = leaf_connections(hub)
        client = await local(node)
        jetstream = client.jetstream()
        await declare(jetstream, dataclasses.replace(event_buffer("player", "record", MIB), allow_direct=True))
        table = KeyTable({"show": MAX_STORED_MESSAGE - 512})
        await declare_bucket(jetstream, desired_bucket("player", table))
        for size in (MAX_STORED_MESSAGE + 1, NODE_MAX_PAYLOAD - 1024):
            await _refused(jetstream.publish("player.record.asrun", b"x" * size), VALUE_TOO_LARGE)
        await jetstream.publish("player.record.asrun", b"x" * MAX_STORED_MESSAGE)
        await (await DocumentWriter.bind(client, "KV_desired_player", writer="node")).put(
            "show", b"s" * table.sizes["show"], expect=ABSENT)

        central_client = await central(hub, "serial-a")
        across = central_client.jetstream(domain=NODE_DOMAIN)
        for direct in (False, True):
            message = await across.get_last_msg("RECORD_player", "player.record.asrun", direct=direct)
            assert len(message.data) == MAX_STORED_MESSAGE, direct
        assert len((await (await across.key_value("desired_player")).get("show")).value) == table.sizes["show"]
        await across.add_consumer("RECORD_player", ConsumerConfig(durable_name="central", ack_policy=AckPolicy.EXPLICIT))
        [delivered] = (await pull(central_client, "RECORD_player", "central", 1, timeout=2, domain=NODE_DOMAIN)).messages
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
    # A reply naming a stored message repeats its subject, JSON-escaped at six bytes for `<`, so a long
    # subject on the largest record once closed the leaf. Both servers' max_control_line (the server's
    # default, pinned) bounds every stored subject: the longest line a Node component sends is stored,
    # one byte more closes only that client, and Central reads the largest record under the
    # worst-escaping longest subject, and the largest document under the longest key a writer admits,
    # every way that carries a subject back across the leaf, with the leaf kept (E-W1-TD-6). Central
    # itself stores only through `$JS.node.API.$KV`, which stores a subject 13 bytes shorter than the
    # line it sent (E-W1-LEAF-1), so the local line is the longest.
    hub = hub_server(tmp_path, ["serial-a"])
    node = node_server(tmp_path, "serial-a", hub)
    hub.start()

    async def body():
        async def linked():
            return len(leaf_connections(hub)) == 1
        await until(linked, 10, "the Node's leaf link")
        before = leaf_connections(hub)
        client = await local(node)
        jetstream = client.jetstream()
        await declare(jetstream, event_buffer("player", "record", MIB))
        record = b"x" * MAX_STORED_MESSAGE
        # nats-py's line for a publish with no reply: PUB, the subject, two spaces and the size.
        longest = NODE_MAX_CONTROL_LINE - len(f"  {len(record)}") - len("player.record.")
        subjects = []
        subject = "player.record." + "<" * longest
        past = await local(node)
        await past.publish(subject + "<", record)

        async def closed():
            return past.is_closed
        await until(closed, 5, "the server closes the client past the control line")
        writer = await local(node)
        await writer.publish(subject, record)
        await writer.flush(2)
        await writer.close()
        subjects.append(subject)

        async def stored():
            return (await jetstream.stream_info("RECORD_player")).state.messages == len(subjects)
        await until(stored, 5, "the longest-subject record is stored, not the one past it")
        # A component's KV put under a 1004-byte key, which a lowered control line once answered by
        # closing the component: the server's default line takes it and keeps the client (E-W1-TD-8).
        state = await declare_bucket(jetstream, state_bucket("host", KeyTable({"k" * 1004: 64})))
        await state.put("k" * 1004, b"kept")
        assert (await state.get("k" * 1004)).value == b"kept" and client.is_connected
        # A key table admits only [A-Za-z0-9_-] keys, so the longest key escapes to itself.
        bucket_name = "desired_player"
        key = "k" * (MAX_PUBLISH_SUBJECT - len(f"$KV.{bucket_name}."))
        table = KeyTable({key: MAX_STORED_MESSAGE - HEADER_ALLOWANCE})
        await declare_bucket(jetstream, desired_bucket("player", table))
        await (await DocumentWriter.bind(client, f"KV_{bucket_name}", writer="node")).put(
            key, b"d" * table.sizes[key], expect=ABSENT)

        central_client = await central(hub, "serial-a")
        across = central_client.jetstream(domain=NODE_DOMAIN)
        for subject in subjects:
            assert len((await across.get_last_msg("RECORD_player", subject)).data) == MAX_STORED_MESSAGE
        document = await across.get_last_msg(f"KV_{bucket_name}", f"$KV.{bucket_name}.{key}")
        assert len(document.data) == table.sizes[key]
        await across.add_consumer("RECORD_player", ConsumerConfig(durable_name="central", ack_policy=AckPolicy.EXPLICIT))
        delivered = (await pull(central_client, "RECORD_player", "central", 1, timeout=2, domain=NODE_DOMAIN)).messages
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
        await declare(jetstream, event_buffer("player", "record", 8 * MIB))
        for _ in range(16):
            await jetstream.publish("player.record.asrun", b"r" * MAX_STORED_MESSAGE)
        reader = await local(node)
        await reader.jetstream().add_consumer("RECORD_player", ConsumerConfig(
            durable_name="component", ack_policy=AckPolicy.EXPLICIT))
        pending = asyncio.ensure_future(pull(reader, "RECORD_player", "component", 16, timeout=8))
        await asyncio.sleep(.05)   # the pull request is out
        time.sleep(3)              # the loop is busy past write_deadline ("2s")
        received = (await pending).messages
        assert received and sum(len(message.data) for message in received) <= PULL_MAX_BYTES
        while len(received) < 16:
            more = (await pull(reader, "RECORD_player", "component", 16, timeout=2)).messages
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
    topics = [f"part{letter}" for letter in "abcd"]
    streams = [f"{topic.upper()}_player" for topic in topics]

    async def body():
        writer = await local(node)
        jetstream = writer.jetstream()
        for topic in topics:
            await declare(jetstream, event_buffer("player", topic, 2 * MIB))
            for _ in range(6):
                await jetstream.publish(f"player.{topic}.asrun", b"r" * MAX_STORED_MESSAGE)
        reader = await local(node)
        for stream in streams:
            await reader.jetstream().add_consumer(stream, ConsumerConfig(
                durable_name="component", ack_policy=AckPolicy.EXPLICIT))
        pending = [asyncio.ensure_future(pull(reader, stream, "component", 6, timeout=8)) for stream in streams]
        await asyncio.sleep(.05)   # the pulls have started
        time.sleep(3)              # the loop is busy past write_deadline ("2s")
        received = {stream: (await task).messages for stream, task in zip(streams, pending, strict=True)}
        for stream in streams:
            assert received[stream], stream
            while len(received[stream]) < 6:
                more = (await pull(reader, stream, "component", 6, timeout=2)).messages
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
        await until(linked, 10, "the Node's leaf link")
        client = await local(node)
        jetstream = client.jetstream()
        await declare(jetstream, state_bucket("host", KeyTable({"pinned": 64, "hot": 64})))
        await declare(jetstream, event_buffer("host", "record", 4 * MIB))
        await jetstream.publish("$KV.state_host.pinned", b"pinned")
        for _ in range(60_000):     # each write deletes the hot key's last value below the newest
            await client.publish("$KV.state_host.hot", b"h")
        for index in range(12_000):  # a distinct subject per record
            await client.publish(f"host.record.{index:010}.{'s' * 16}", b"r")
        await jetstream.publish("$KV.state_host.hot", b"h")   # acknowledged after every write before it
        await jetstream.publish("host.record.last", b"r")

        central_client = await central(hub, "serial-a")
        across = central_client.jetstream(domain=NODE_DOMAIN)
        for stream, details in (("KV_state_host", {"deleted_details": True}), ("RECORD_host", {"subjects_filter": ">"})):
            before = await until(linked, 10, "the Node's leaf link")

            async def epoch_read(stream=stream):
                try:
                    return await stream_epoch(across, stream)
                except (nats.errors.NoRespondersError, nats.errors.TimeoutError):
                    return None
            await until(epoch_read, 10, f"nodeapi's epoch read of {stream} across the leaf")
            assert leaf_connections(hub) == before
            request = json.dumps(details).encode()
            assert len((await client.request(f"$JS.API.STREAM.INFO.{stream}", request, timeout=5)).data) > NODE_MAX_PAYLOAD
            with pytest.raises(nats.errors.TimeoutError):
                await central_client.request(f"$JS.{NODE_DOMAIN}.API.STREAM.INFO.{stream}", request, timeout=2)

            async def relinked(before=before):
                now = leaf_connections(hub)
                return now and now != before
            await until(relinked, 10, f"the leaf closed for {stream} and relinked")
        assert hub.log_tail(400).count("Leafnode connection closed: Maximum Message Payload Exceeded") >= 2
        await central_client.close()
        await client.close()

    try:
        _run(node, body)
    finally:
        hub.stop()
