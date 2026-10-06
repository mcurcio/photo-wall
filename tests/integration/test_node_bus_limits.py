"""The Node bus's store budget on a real server (E3a-3, API8): the server itself refuses a stream
past the store limit, one without a byte cap and one in memory; each retention class keeps the
promise the Node API page states for it.

The Node runs the shipped `node-bus.conf`, unmodified. The store limit is a property of the Node's
own server, so no hub runs: the Node's leaf retries in the background and nothing here crosses it.
The class numbers are the page's starting split (section 6), held here only; E3b's class table
owns them. Every stream, bucket and subject is the test's.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from integration.bus_servers import BusServer, hub_server, kv_bucket_bytes, local, node_server
from nats.js.api import DiscardPolicy, KeyValueConfig, RetentionPolicy, StorageType, StreamConfig
from nats.js.errors import APIError

MIB = 1024 * 1024
STORAGE_EXCEEDED = 10047        # JSStorageResourcesExceededErr: past the store's reservation
MAX_BYTES_REQUIRED = 10113      # JSStreamMaxBytesRequired: account API refuses an uncapped stream
MEMORY_EXCEEDED = 10028         # JSMemoryResourcesExceededErr: the account has no memory store
KEY_REFUSED = 10077             # JSStreamStoreFailedF "maximum bytes exceeded": the bucket is full
VALUE_TOO_LARGE = 10054         # JSStreamMessageExceedsMaximumErr: over max_value_size

# The page's 12 MiB split (section 6): records, observations, state, desired, the wall copy and
# the unassigned room. A stream stands in for the wall mirror; its bytes are the point.
RECORD_STREAMS = {"player": 4 * MIB, "apps": MIB, "display": MIB, "host": MIB // 2}
OBSERVATION_STREAMS = {"health": MIB, "content": MIB}
STATE_BUCKETS = ("host", "apps", "content", "display", "health", "player")
DESIRED_BUCKETS = ("apps", "display", "health", "player")
BUCKET_BYTES = MIB // 4
WALL_COPY_BYTES = MIB // 2
STORE_LIMIT = 12 * MIB           # node-bus.conf's max_file_store and account API's max_file
# The unassigned room, held by a reserved stream that nothing can write (it takes one message of at
# most one byte), so no stream can reserve it. The server checks the account's usage plus the new
# message before a full limits stream drops its oldest; without unreserved room, a full split
# refuses every record and observation publish (10002; erratum E-W1-E3a-R-3).
ROOM_BYTES = MIB // 2
MAX_PAYLOAD = 256 * 1024         # node-bus.conf's max_payload: headers + payload
MAX_CONTROL_LINE = 4096          # nats-server's default, which bounds a message's subject
# nats-server's per-message store charge: 30 + subject + payload, and 4 + headers more with headers
# (ns:server/filestore.go:10055-10062, erratum E-W1-E3a-3-1).
CHARGE = 30
LARGEST_MESSAGE = CHARGE + 4 + MAX_CONTROL_LINE + MAX_PAYLOAD
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


def test_the_node_server_refuses_streams_past_its_store_limit_and_without_a_cap(tmp_path):
    node = _node(tmp_path)

    async def body():
        client = await local(node)
        jetstream = client.jetstream()
        assert client.max_payload == MAX_PAYLOAD  # the shipped file's pin, as the server applies it
        assert ROOM_BYTES >= LARGEST_MESSAGE

        # The server, not a library convention, refuses an uncapped stream and a memory one.
        await _refused(jetstream.add_stream(StreamConfig(
            name="UNCAPPED", subjects=["uncapped.>"], storage=StorageType.FILE)), MAX_BYTES_REQUIRED)
        await _refused(jetstream.add_stream(StreamConfig(
            name="MEMORY", subjects=["memory.>"], max_bytes=64 * 1024, storage=StorageType.MEMORY)),
            MEMORY_EXCEEDED)

        # The page's split, every byte cap reserved, fills the store exactly.
        caps: dict[str, int] = {}
        subjects: dict[str, str] = {}   # a stream's publish subject, for the fill below
        for component, cap in RECORD_STREAMS.items():
            caps[f"REC_{component}"] = cap
            subjects[f"REC_{component}"] = f"{component}.record.asrun"
            await jetstream.add_stream(StreamConfig(
                name=f"REC_{component}", subjects=[f"{component}.record.>"], max_bytes=cap,
                retention=RetentionPolicy.LIMITS, discard=DiscardPolicy.OLD, storage=StorageType.FILE))
        for component, cap in OBSERVATION_STREAMS.items():
            caps[f"OBS_{component}"] = cap
            subjects[f"OBS_{component}"] = f"{component}.observation.verdict"
            await jetstream.add_stream(StreamConfig(
                name=f"OBS_{component}", subjects=[f"{component}.observation.>"], max_bytes=cap,
                max_age=6 * 3600, discard=DiscardPolicy.OLD, storage=StorageType.FILE))
        for prefix, components, history in (("state", STATE_BUCKETS, 4), ("desired", DESIRED_BUCKETS, 2)):
            for component in components:
                bucket = f"{prefix}_{component}"
                caps[f"KV_{bucket}"] = BUCKET_BYTES
                await jetstream.create_key_value(KeyValueConfig(
                    bucket=bucket, history=history, max_bytes=BUCKET_BYTES, max_value_size=4096,
                    storage=StorageType.FILE))
        caps["WALL_COPY"] = WALL_COPY_BYTES
        subjects["WALL_COPY"] = "wall_copy.timing"
        await jetstream.add_stream(StreamConfig(
            name="WALL_COPY", subjects=["wall_copy.>"], max_bytes=WALL_COPY_BYTES, storage=StorageType.FILE))
        caps["ROOM"] = ROOM_BYTES
        await jetstream.add_stream(StreamConfig(
            name="ROOM", subjects=["room.>"], max_bytes=ROOM_BYTES, max_msgs=1, max_msg_size=1,
            discard=DiscardPolicy.NEW, storage=StorageType.FILE))
        assert sum(caps.values()) == STORE_LIMIT
        await jetstream.publish("player.record.asrun", b"as-run 1")
        before = {name: (await jetstream.stream_info(name)) for name in caps}

        # One more stream does not fit, and raising an existing cap does not either.
        await _refused(jetstream.add_stream(StreamConfig(
            name="EXTRA", subjects=["extra.>"], max_bytes=MIB, storage=StorageType.FILE)), STORAGE_EXCEEDED)
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

        # Fill the whole split: every stream and bucket holds exactly its cap. The room takes
        # nothing that counts, so the store keeps one largest message unreserved and unused.
        await _refused(jetstream.publish("room.held", b"xx"), VALUE_TOO_LARGE)
        for name, cap in caps.items():
            if name.startswith("KV_"):
                kv = await jetstream.key_value(name.removeprefix("KV_"))
                for index in range(cap // FILL_CHARGE):
                    key = f"fill{index}"
                    await kv.put(key, _fill(f"$KV.{name.removeprefix('KV_')}.{key}"))
                await _refused(kv.put("one-more", b"x"), KEY_REFUSED)
            elif name in subjects:
                for _ in range(cap // FILL_CHARGE):
                    await jetstream.publish(subjects[name], _fill(subjects[name]))
            assert (await jetstream.stream_info(name)).state.bytes == (0 if name == "ROOM" else cap), name
        assert (await jetstream.account_info()).storage == STORE_LIMIT - ROOM_BYTES

        # Full, every record and observation stream still takes a publish, dropping its oldest
        # (F7: a gap stays a counted hole, never a refused publish with no sequence).
        for name, subject in subjects.items():
            if name.startswith(("REC_", "OBS_")):
                before_full = (await jetstream.stream_info(name)).state
                acknowledgement = await jetstream.publish(subject, _fill(subject))
                assert (acknowledgement.stream, acknowledgement.seq) == (name, before_full.last_seq + 1)
                assert (await jetstream.stream_info(name)).state.first_seq > before_full.first_seq, name
        await client.close()

    _run(node, body)


def _fill(subject: str) -> bytes:
    """A payload the store charges exactly FILL_CHARGE bytes on `subject` (no headers)."""
    return b"f" * (FILL_CHARGE - CHARGE - len(subject))


async def _record(jetstream) -> None:
    """Past its cap the oldest go, every publish is accepted, and first_seq advances by the drop."""
    cap = 16 * 1024
    await jetstream.add_stream(StreamConfig(
        name="REC_probe", subjects=["probe.record.>"], max_bytes=cap, retention=RetentionPolicy.LIMITS,
        discard=DiscardPolicy.OLD, storage=StorageType.FILE))
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
    await jetstream.add_stream(StreamConfig(
        name="OBS_probe", subjects=["probe.observation.>"], max_bytes=16 * 1024, max_age=1,
        retention=RetentionPolicy.LIMITS, discard=DiscardPolicy.OLD, storage=StorageType.FILE))
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
    `history` values; an unlisted key is refused once the bucket is full (its headroom spent); an
    oversized value is refused; every listed key keeps its last good value."""
    bucket, keys, max_value = f"{prefix}_probe", ("conditions", "position", "horizon", "verdicts"), 512
    capacity = kv_bucket_bytes(bucket, keys, history, max_value)
    kv = await jetstream.create_key_value(KeyValueConfig(
        bucket=bucket, history=history, max_value_size=max_value, max_bytes=capacity,
        storage=StorageType.FILE))
    config = (await jetstream.stream_info(f"KV_{bucket}")).config
    assert (config.discard, config.max_msgs_per_subject, config.max_bytes) == (
        DiscardPolicy.NEW, history, capacity)

    # Sizes vary up to max_value, and each key in turn lags the others with one short value: the
    # worst case for a full bucket is a key whose oldest value is shorter than its next one.
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

    # A key beyond the list: the headroom (one largest message) takes at most one, then refusal.
    strays = 0
    while True:
        try:
            await kv.put(f"unlisted{strays}", b"x" * max_value)
        except APIError as unlisted:
            assert unlisted.err_code == KEY_REFUSED, unlisted
            break
        strays += 1
        assert strays <= 1, "a full bucket took more than its headroom from unlisted keys"
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
