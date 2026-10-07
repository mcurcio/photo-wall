"""Every stream, KV bucket and mirror on a Node bus or in the hub's WALL account, built in one place.

The buffer rule (owner, 2026-10-06; errata E-W1-BUF-2, E-W1-TD-4) has two kinds. Both are
JetStream limits retention with discard old in the file store, so no buffer refuses a write for
being full:

- **circular** (`buffer`, `bucket`): event buffers (records, observations, reported state). A byte
  cap and any other limit; full, the server drops the oldest and takes the write, and a reader
  sees the loss as a sequence gap.
- **sticky** (`Documents`, `sticky_bucket`, `wall_config`, `wall_mirror_config`): documents that
  must stay (desired-state buckets, WALL and every Node mirror of it). Per-subject history only and
  no count limit. The byte cap is the document table's budget (keys x history x largest value),
  which the table's one writer, `documents.DocumentWriter`, never exceeds: it refuses its own
  unlisted or oversize write before sending. So the per-subject limit always drops a key's own
  oldest value first (ns:server/filestore.go:5340-5380, before the byte limit) and no document is
  evicted by bytes or count. Nothing on the Node refuses anything.

Every builder sets `max_msg_size` to at most `MAX_STORED_MESSAGE`, so a reply carrying a stored
message always fits the leaf (E-W1-TD-2). A builder carries no epoch: `declare` writes a fresh one
into the metadata of every stream it creates, at the create, and a Node stream also starts at a
sequence derived from it, so a revision or cursor read before a store loss names no message of the
re-created stream, however long its declarer held the configuration (E-W1-TD-5, E-W1-FV-1). A
caller never sets the policy, the storage or the metadata.

No buffer forwards: `buffer` refuses republish, sources, mirror and subject transforms; the WALL
mirror is built only by `wall_mirror_config`, and lives in the Node server's mirror account, reached
through `wall_copy` (erratum E-W1-LEAF-2). No buffer removes a message except by its own limits: every
builder sets deny_delete and deny_purge and refuses rollups and per-message TTLs, since a removal that
is not a limit's writes a tombstone into a new file-store block, which the store frees only at its
2-minute sync (ns:server/filestore.go:6239-6249, 7363-7367), past the cap plus one block the fence
charges each stream (E-W1-LEAF-2). The Node's server refuses a local client's delete, purge and update
of any stream too (contracts.node_link.LOCAL_PUBLISH_DENY).

`ClassTable` declares a Node's whole store at once: its caps never total more than the store, so
no declare meets 10047 whatever the order (E-W1-TD-S2). The store is tmpfs inside the bus's memory
fence, so a table whose files (caps plus a flat 4 MiB file-store block per stream) cannot fit beside
the server's heap, or that has more streams than the server admits, fails to build: a store that
cannot fit its fence would OOM-loop on every restart (E-W1-STORE-1, E-W1-FIT-1); it holds at most the
one wall copy the mirror account admits. Nothing here
changes a declared stream's limits: a per-Node override (re-splitting the store) waits for E3b's
design, and the server's own `max_file_store` and `max_streams` bound the files of any client.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from nats.js.api import (
    DiscardPolicy,
    ExternalStream,
    RetentionPolicy,
    StorageType,
    StreamConfig,
    StreamSource,
)
from nats.js.errors import APIError, NotFoundError

from contracts.node_link import (
    FILESTORE_BLOCK_BOUND,
    MAX_STORED_MESSAGE,
    MIRROR_API_PREFIX,
    MIRROR_MAX_STREAMS,
    NODE_ACCOUNT_MAX_STREAMS,
    NODE_BUS_GOMEMLIMIT,
    NODE_BUS_HEADROOM,
    NODE_BUS_MEMORY_MAX,
    NODE_MAX_CONTROL_LINE,
    NODE_MAX_STREAMS,
    NODE_STORE_BYTES,
    WALL_API_PREFIX,
    WALL_DELIVER_PREFIX,
    WALL_STREAM,
    WALL_STREAM_BYTES,
)

if TYPE_CHECKING:
    from nats.aio.client import Client
    from nats.js.api import StreamInfo
    from nats.js.client import JetStreamContext

EPOCH_KEY: Final = "photo_wall_epoch"   # stream metadata: a fresh uuid at every create
KIND_KEY: Final = "photo_wall_kind"     # stream metadata: CIRCULAR or STICKY
CIRCULAR: Final = "circular"
STICKY: Final = "sticky"
MANIFEST_KEY: Final = "_manifest"       # a sticky buffer's list of the documents written to it
HEADER_ALLOWANCE: Final = 256           # the header block a document writer may send with a document
# The longest document subject: half of NODE_MAX_CONTROL_LINE, so the line of any client naming it
# fits with what it adds (a writer's inbox and sizes, a reader's `$JS.<domain>.API.DIRECT.GET.<stream>.`
# prefix) and the server never closes either for a document's subject (E-W1-TD-6).
MAX_PUBLISH_SUBJECT: Final = NODE_MAX_CONTROL_LINE // 2
STREAM_NAME_IN_USE: Final = 10058       # two declarers raced: the other one created it
# The same race on a store whose reservations are near full: the server checks the new stream's
# reservation (ns:server/jetstream_api.go:1615) before it looks for the name (ns:server/stream.go:891).
STORAGE_EXCEEDED: Final = 10047
# The same race on a store at its stream count (node-bus.conf's max_streams), checked before the name too.
MAX_STREAMS_REACHED: Final = 10027
# What every builder sets and no caller may: the buffer rule, and no removal but a limit's.
_NO_REMOVALS: Final = {"deny_delete": True, "deny_purge": True, "allow_rollup_hdrs": False, "allow_msg_ttl": False}
_FIXED: Final = frozenset({"retention", "discard", "storage", "metadata", *_NO_REMOVALS})
_FORWARDS: Final = frozenset({"republish", "sources", "mirror", "subject_transform"})


def message_charge(subject: str, value_bytes: int, header_bytes: int = 0) -> int:
    """nats-server's file-store charge for one message (ns:server/filestore.go:10054-10062)."""
    return 30 + len(subject.encode()) + value_bytes + (4 + header_bytes if header_bytes else 0)


def epoch_origin(epoch: str) -> int:
    """The first sequence of a Node stream created with `epoch`: 1 plus 40 bits of the epoch, so two
    creations of one stream share no sequence unless their origins fall within a stream's length."""
    return 1 + int(epoch[:10], 16)


def _build(kind: str, name: str, max_bytes: int, fields: dict) -> StreamConfig:
    # No epoch and no epoch-derived first_seq: `declare` stamps both when it creates the stream, so a
    # configuration held across a store loss never re-creates its stream as the old creation.
    if _FIXED & fields.keys():
        raise ValueError("buffer_policy_is_fixed")
    if max_bytes <= 0:
        raise ValueError("buffer_needs_a_byte_cap")
    size = fields.pop("max_msg_size", None)
    if size is None:
        size = MAX_STORED_MESSAGE
    elif not 0 < size <= MAX_STORED_MESSAGE:
        raise ValueError("buffer_message_past_the_leaf")
    return StreamConfig(name=name, max_bytes=max_bytes, max_msg_size=size, retention=RetentionPolicy.LIMITS,
                        discard=DiscardPolicy.OLD, storage=StorageType.FILE, metadata={KIND_KEY: kind},
                        **_NO_REMOVALS, **fields)


def buffer(name: str, max_bytes: int, **fields) -> StreamConfig:
    """A circular event buffer: its byte cap and any other limit the caller gives (subjects, max_age,
    max_msgs, max_msgs_per_subject, a smaller max_msg_size). Full, the server drops the oldest and
    takes the write; a reader sees the drop as the hole below the stream's first sequence. Never one
    that forwards what it stores (republish, sources, mirror, a subject transform)."""
    if _FORWARDS & fields.keys():
        raise ValueError("buffer_never_forwards")
    return _build(CIRCULAR, name, max_bytes, dict(fields))


def _kv_fields(bucket_name: str, history: int, max_value_size: int | None) -> dict:
    # A KV bucket's stream as nats-py's create_key_value builds it (nats/js/client.py:1447), less its
    # hard-coded discard NEW, which refuses every put once the bucket is full, and less rollups (a KV
    # purge) and per-key TTLs, which remove messages past the limits (`_NO_REMOVALS`). A KV delete is
    # a write (a marker), and stays.
    return dict(subjects=[f"$KV.{bucket_name}.>"], duplicate_window=120, max_consumers=-1, max_msgs=-1,
                max_msg_size=max_value_size, max_msgs_per_subject=history)


def bucket(name: str, *, history: int, max_bytes: int, max_value_size: int | None = None) -> StreamConfig:
    """A circular KV bucket (reported state): `history` values per key, a key's own oldest first;
    an unlisted key past the byte cap costs the bucket its oldest message."""
    return _build(CIRCULAR, f"KV_{name}", max_bytes, _kv_fields(name, history, max_value_size))


@dataclass(frozen=True)
class Documents:
    """A sticky buffer's document table: every key it may hold, each with its largest value, and the
    history kept per key. Its budget is the sticky buffer's byte cap; its one writer keeps to it."""
    stream: str
    subject_prefix: str             # the stored subject of key k is subject_prefix + k
    sizes: Mapping[str, int]        # key -> largest value in bytes (headers are HEADER_ALLOWANCE more)
    history: int = 1

    def __post_init__(self) -> None:
        if type(self.history) is not int or self.history < 1:
            raise ValueError("documents_need_history")
        if not self.sizes or MANIFEST_KEY in self.sizes:
            raise ValueError("documents_key_set")
        object.__setattr__(self, "sizes", MappingProxyType(dict(self.sizes)))
        for key, size in self.all_sizes().items():
            if not 0 < size <= MAX_STORED_MESSAGE - HEADER_ALLOWANCE:
                raise ValueError("documents_value_past_the_leaf")
            if len((self.subject_prefix + key).encode()) > MAX_PUBLISH_SUBJECT:
                raise ValueError("documents_subject_past_the_control_line")

    @classmethod
    def bucket(cls, bucket_name: str, sizes: Mapping[str, int], *, history: int) -> Documents:
        """A sticky KV bucket's table (a desired-state bucket)."""
        return cls(f"KV_{bucket_name}", f"$KV.{bucket_name}.", sizes, history)

    @classmethod
    def wall(cls, sizes: Mapping[str, int]) -> Documents:
        """WALL's table (keys are subjects under `wall.`). Its budget must fit WALL_STREAM_BYTES, the
        one cap of the hub stream and of every Node mirror, which hold no key list of their own."""
        table = cls(WALL_STREAM, "wall.", sizes, 1)
        if table.budget > WALL_STREAM_BYTES:
            raise ValueError("wall_documents_over_budget")
        return table

    def manifest(self, keys: Iterable[str]) -> bytes:
        """The manifest document's value for these written keys."""
        return json.dumps(sorted(keys), separators=(",", ":")).encode()

    def all_sizes(self) -> dict[str, int]:
        return {**self.sizes, MANIFEST_KEY: len(self.manifest(self.sizes))}

    @property
    def budget(self) -> int:
        """Bytes the stream holds with every document, the manifest included, at its largest value
        and largest headers, `history` times: the sticky buffer's byte cap."""
        return self.history * sum(message_charge(self.subject_prefix + key, size, HEADER_ALLOWANCE)
                                  for key, size in self.all_sizes().items())

    @property
    def largest(self) -> int:
        """The largest message (headers + value) the table admits: the stream's max_msg_size."""
        return max(self.all_sizes().values()) + HEADER_ALLOWANCE


def sticky_bucket(table: Documents) -> StreamConfig:
    """A sticky KV bucket: per-key history only; its byte cap is the table's budget."""
    if not table.stream.startswith("KV_"):
        raise ValueError("sticky_bucket_needs_a_bucket_table")
    return _build(STICKY, table.stream, table.budget,
                  _kv_fields(table.stream.removeprefix("KV_"), table.history, table.largest))


def wall_config(*, first_seq: int = 1) -> StreamConfig:
    """The hub's wall-wide stream: sticky, latest value per subject, WALL_STREAM_BYTES (the cap its
    mirrors share). `first_seq` continues a lost WALL past Central's last acknowledged sequence
    (erratum E-W1-E3a-R-4)."""
    return _build(STICKY, WALL_STREAM, WALL_STREAM_BYTES,
                  dict(subjects=["wall.>"], max_msgs_per_subject=1, max_msgs=-1, first_seq=first_seq))


def wall_mirror_config() -> StreamConfig:
    """A Node's read-only local mirror of WALL, sticky as its origin: max_msgs_per_subject 1 and the
    same cap (erratum E-W1-E3a-R-1). Declared and read through `wall_copy`."""
    return _build(STICKY, WALL_STREAM, WALL_STREAM_BYTES, dict(
        max_msgs_per_subject=1, max_msgs=-1, mirror=StreamSource(
            name=WALL_STREAM, external=ExternalStream(api=WALL_API_PREFIX, deliver=WALL_DELIVER_PREFIX))))


def wall_copy(client: Client, **options) -> JetStreamContext:
    """A Node program's JetStream view of the wall copy: the one stream of the Node server's mirror
    account, which the program's account imports under MIRROR_API_PREFIX (info, create, read; erratum
    E-W1-LEAF-2)."""
    return client.jetstream(prefix=MIRROR_API_PREFIX, **options)


def buffer_kind(config: StreamConfig) -> str:
    return (config.metadata or {})[KIND_KEY]


def epoch_of(info: StreamInfo) -> str:
    """The epoch the stream was created with."""
    epoch = (info.config.metadata or {}).get(EPOCH_KEY)
    if not epoch:
        raise ValueError("stream_has_no_epoch")
    return epoch


async def stream_epoch(jetstream: JetStreamContext, stream: str) -> str:
    return epoch_of(await jetstream.stream_info(stream))


def _stamped(config: StreamConfig) -> StreamConfig:
    """`config` as one create sends it: a fresh epoch in its metadata and, on a Node stream (not a
    mirror, no first_seq of the builder's own such as WALL's continuation), the first sequence
    derived from that epoch."""
    epoch = uuid.uuid4().hex
    first_seq = config.first_seq
    if config.mirror is None and first_seq is None:
        first_seq = epoch_origin(epoch)
    return replace(config, metadata={**config.metadata, EPOCH_KEY: epoch}, first_seq=first_seq)


async def declare(jetstream: JetStreamContext, config: StreamConfig) -> bool:
    """Create-if-absent; True when this call created it. A stream that exists keeps its
    configuration and its epoch: only a create starts a new epoch, drawn here at the create, so the
    same configuration declared again after a store loss is a new creation (E-W1-FV-1). Only a
    builder's configuration is declared: one read back from a stream carries that creation's epoch
    and first sequence, which a re-create must never reuse. Re-declaring is a no-op on any store, a
    wholly reserved one included: the stream is looked up, never re-added, since the server answers
    an add of a stream that exists with 10047 once the reservations are near full, and with 10027 once
    the store holds its most streams (E-W1-STORE-1, E-W1-FIT-1). A create that loses a race to another
    declarer is a no-op too."""
    metadata = config.metadata or {}
    if metadata.get(KIND_KEY) not in (CIRCULAR, STICKY) or EPOCH_KEY in metadata:
        raise ValueError("declare_needs_a_built_buffer")
    try:
        await jetstream.stream_info(config.name)
        return False
    except NotFoundError:
        pass
    try:
        await jetstream.add_stream(_stamped(config))
    except APIError as error:
        if error.err_code not in (STREAM_NAME_IN_USE, STORAGE_EXCEEDED, MAX_STREAMS_REACHED):
            raise
        try:
            await jetstream.stream_info(config.name)
        except NotFoundError:
            raise error from None   # 10047 or 10027 for a stream that is still absent: the store is full
        return False
    return True


@dataclass(frozen=True)
class ClassTable:
    """One Node store's whole declaration, by one owner. Its caps total at most `total`, itself at
    most the store, so the server never refuses a declare of it for room (10047) in any order; it has
    at most the streams the server admits (10027). Its files (every cap plus a flat FILESTORE_BLOCK_BOUND
    per stream, the block each may have whatever cap it was created with) fit the bus's memory fence
    beside the server's heap, so a full store reloads inside the fence after any restart
    (E-W1-STORE-1, E-W1-FIT-1)."""
    buffers: Mapping[str, StreamConfig]
    total: int = NODE_STORE_BYTES

    def __post_init__(self) -> None:
        if not 0 < self.total <= NODE_STORE_BYTES:
            raise ValueError("class_table_past_the_store")
        buffers = dict(self.buffers)
        for name, config in buffers.items():
            if config.name != name:
                raise ValueError("class_table_names")
            if (config.metadata or {}).get(KIND_KEY) not in (CIRCULAR, STICKY):
                raise ValueError("class_table_unbuilt_buffer")
        object.__setattr__(self, "buffers", MappingProxyType(buffers))
        caps = sum(config.max_bytes for config in buffers.values())
        if caps > self.total:
            raise ValueError("class_table_over_total")
        mirrors = sum(config.mirror is not None for config in buffers.values())
        if (len(buffers) > NODE_MAX_STREAMS or mirrors > MIRROR_MAX_STREAMS
                or len(buffers) - mirrors > NODE_ACCOUNT_MAX_STREAMS):
            raise ValueError("class_table_too_many_streams")
        if caps + len(buffers) * FILESTORE_BLOCK_BOUND + NODE_BUS_GOMEMLIMIT + NODE_BUS_HEADROOM > NODE_BUS_MEMORY_MAX:
            raise ValueError("class_table_past_the_bus_fence")


async def declare_table(client: Client, table: ClassTable, **options) -> None:
    """Create every buffer of the table that is absent: the wall copy through `wall_copy`, the rest in
    the program's own account. `options` go to both JetStream contexts (a timeout)."""
    own, mirror = client.jetstream(**options), wall_copy(client, **options)
    for config in table.buffers.values():
        await declare(mirror if config.mirror is not None else own, config)

