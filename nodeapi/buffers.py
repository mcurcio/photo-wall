"""Every stream, KV bucket and mirror on a Node bus or in the hub's WALL account, built in one place.

The buffer rule (owner, 2026-10-06; errata E-W1-BUF-2, E-W1-TD-4) has two kinds. Both are
JetStream limits retention with discard old in the file store, so no buffer refuses a write for
being full:

- **circular** (`buffer`, `bucket`): event buffers (records, observations, reported state). A byte
  cap and any other limit; full, the server drops the oldest and takes the write, and a reader
  sees the loss as a sequence gap.
- **sticky** (`state_bucket`, `desired_bucket`, `wall_config`, `wall_mirror_config`): keyed data
  that must stay (state, desired documents, WALL and every Node mirror of it). Per-subject history
  only and no count limit. The byte cap is the key table's budget (keys x history x largest value),
  and the table travels in the stream's metadata, so every writer (`documents.DocumentWriter`, the
  session's state put) refuses its own unlisted or oversize write before sending. So the per-subject
  limit always drops a key's own oldest value first (ns:server/filestore.go:5340-5380, before the
  byte limit) and no key loses its only value to bytes or count. Nothing on the Node refuses anything.

Every builder sets `max_msg_size` to at most `MAX_STORED_MESSAGE`, so a reply carrying a stored
message always fits the leaf (E-W1-TD-2). A builder carries no epoch: `declare` writes a fresh one
into the metadata of every stream it creates, at the create, and a Node stream also starts at a
sequence derived from it, so a revision or cursor read before a store loss names no message of the
re-created stream, however long its declarer held the configuration (E-W1-TD-5, E-W1-FV-1). A
caller never sets the policy, the storage or the metadata.

No buffer forwards: `buffer` refuses republish, sources, mirror and subject transforms. A republish
is an internal server publish that no permission checks, so it is the one way a Node stream could
send across the leaf what Central did not pull (erratum E-W1-LEAF-1); the WALL mirror is built only
by `wall_mirror_config`.

`ClassTable` declares a Node's whole store at once: its caps never total more than the store, so
no declare meets 10047 whatever the order (E-W1-TD-S2). The store is tmpfs inside the bus's memory
fence, so a table whose files (caps plus a flat 4 MiB file-store block per stream) cannot fit beside
the server's heap, or that has more streams than the server admits, fails to build: a store that
cannot fit its fence would OOM-loop on every restart (E-W1-STORE-1, E-W1-FIT-1). Nothing here
changes a declared stream's limits: a per-Node override (re-splitting the store) waits for E3b's
design, and the server's own `max_file_store` and `max_streams` bound the files of any client.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
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
    MAX_STORED_MESSAGE,
    NODE_BUS_GOMEMLIMIT,
    NODE_BUS_HEADROOM,
    NODE_BUS_MEMORY_MAX,
    NODE_MAX_CONTROL_LINE,
    NODE_MAX_STREAMS,
    NODE_STORE_BYTES,
    STORE_LINES,
    STREAM_BOUND,
    WALL_API_PREFIX,
    WALL_DELIVER_PREFIX,
    WALL_STREAM,
    WALL_STREAM_BYTES,
    StoreLine,
)
from nodeapi.epoch import EPOCH_KEY, epoch_origin, stream_epoch

if TYPE_CHECKING:
    from nats.js.client import JetStreamContext

KIND_KEY: Final = "photo_wall_kind"     # stream metadata: CIRCULAR or STICKY
CIRCULAR: Final = "circular"
STICKY: Final = "sticky"
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
_FIXED: Final = frozenset({"retention", "discard", "storage", "metadata"})
_FORWARDS: Final = frozenset({"republish", "sources", "mirror", "subject_transform"})


def message_charge(subject: str, value_bytes: int, header_bytes: int = 0) -> int:
    """nats-server's file-store charge for one message (ns:server/filestore.go:10054-10062)."""
    return 30 + len(subject.encode()) + value_bytes + (4 + header_bytes if header_bytes else 0)


def _build(kind: str, name: str, max_bytes: int, fields: dict,
           metadata: Mapping[str, str] | None = None) -> StreamConfig:
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
                        discard=DiscardPolicy.OLD, storage=StorageType.FILE,
                        metadata={**(metadata or {}), KIND_KEY: kind}, **fields)


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
    # hard-coded discard NEW, which refuses every put once the bucket is full.
    return dict(subjects=[f"$KV.{bucket_name}.>"], allow_rollup_hdrs=True, allow_msg_ttl=True,
                deny_delete=True, duplicate_window=120, max_consumers=-1, max_msgs=-1,
                max_msg_size=max_value_size, max_msgs_per_subject=history)


def bucket(name: str, *, history: int, max_bytes: int, max_value_size: int | None = None) -> StreamConfig:
    """A circular KV bucket (reported state): `history` values per key, a key's own oldest first;
    an unlisted key past the byte cap costs the bucket its oldest message."""
    return _build(CIRCULAR, f"KV_{name}", max_bytes, _kv_fields(name, history, max_value_size))


def buffer_kind(config: StreamConfig) -> str:
    return (config.metadata or {})[KIND_KEY]


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
    at most the streams the server admits (10027). Its files (every cap plus a flat STREAM_BOUND per
    stream: the block each may have whatever cap it was created with, and the consumers the server
    admits on it) fit the bus's memory fence beside the server's heap, so a full store reloads inside
    the fence after any restart (E-W1-STORE-1, E-W1-FIT-1, E-W1-CONS-2)."""
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
        if len(buffers) > NODE_MAX_STREAMS:
            raise ValueError("class_table_too_many_streams")
        if caps + len(buffers) * STREAM_BOUND + NODE_BUS_GOMEMLIMIT + NODE_BUS_HEADROOM > NODE_BUS_MEMORY_MAX:
            raise ValueError("class_table_past_the_bus_fence")


async def declare_table(jetstream: JetStreamContext, table: ClassTable) -> None:
    """Create every buffer of the table that is absent."""
    for config in table.buffers.values():
        await declare(jetstream, config)



# Self-describing Node streams (E3b design §4 rule 2, §7.2): each one built here carries its role, its
# line's namespace, its pipe and, when keyed, its key table in metadata, so every client reads them
# from the stream. A component's buffers make one `Slice` inside its store line (§7.3).
ROLE_KEY: Final = "photo_wall_role"
NAMESPACE_KEY: Final = "photo_wall_namespace"
PIPE_KEY: Final = "photo_wall_pipe"
TABLE_KEY: Final = "photo_wall_table"
# The most a key table's encoding may take in metadata: a stream description stays far under the
# leaf's largest reply (L, contracts.node_link.NODE_MAX_PAYLOAD) whatever the table.
MAX_TABLE_METADATA: Final = 16 * 1024
BIRTH_KEY: Final = "birth"               # every state bucket: the session's birth (§7.2 node)
OUTBOX_KEY: Final = "outbox"             # every state bucket: the outbox's drop count (written from S6)
RESERVED_STATE_BYTES: Final = 4096       # each reserved key's largest value
_LOWER = re.compile(r"[a-z]+")
_KEY = re.compile(r"[A-Za-z0-9_-]+")


class Role(StrEnum):
    EVENTS = "events"     # circular
    STATE = "state"       # sticky by its state table
    DESIRED = "desired"   # sticky by its document table (built from S2)
    WALL = "wall"         # sticky: the hub's WALL and every Node mirror


@dataclass(frozen=True)
class KeyTable:
    """A keyed buffer's table: every key it may hold, each with its largest value, and the history
    kept per key. Its budget is the buffer's byte cap, so bytes never evict a key's only value."""
    sizes: Mapping[str, int]             # key -> largest value in bytes
    history: int = 1

    def __post_init__(self) -> None:
        if not self.sizes:
            raise ValueError("key_table_empty")
        if type(self.history) is not int or self.history < 1:
            raise ValueError("key_table_history")
        sizes = dict(self.sizes)
        for key, size in sizes.items():
            if type(key) is not str or not _KEY.fullmatch(key):
                raise ValueError("key_table_key")
            if type(size) is not int or not 0 < size <= MAX_STORED_MESSAGE - HEADER_ALLOWANCE:
                raise ValueError("key_table_value_past_the_leaf")
        object.__setattr__(self, "sizes", MappingProxyType(sizes))
        if len(self.encoded().encode()) > MAX_TABLE_METADATA:
            raise ValueError("key_table_metadata_too_large")

    def budget(self, subject_prefix: str) -> int:
        """Bytes the buffer holds with every key at its largest value and largest headers, `history`
        times: the keyed buffer's byte cap."""
        return self.history * sum(message_charge(subject_prefix + key, size, HEADER_ALLOWANCE)
                                  for key, size in self.sizes.items())

    @property
    def largest(self) -> int:
        """The largest message (headers + value) the table admits: the buffer's max_msg_size."""
        return max(self.sizes.values()) + HEADER_ALLOWANCE

    def encoded(self) -> str:
        return json.dumps({"history": self.history, "sizes": dict(self.sizes)}, sort_keys=True,
                          separators=(",", ":"))

    @classmethod
    def decoded(cls, text: str) -> KeyTable:
        data = json.loads(text)
        return cls(data["sizes"], data["history"])


def _line_metadata(line: str, role: Role) -> dict[str, str]:
    store_line = STORE_LINES.get(line)
    if store_line is None:
        raise ValueError("buffer_unknown_line")
    return {ROLE_KEY: role.value, NAMESPACE_KEY: line, PIPE_KEY: store_line.pipe.value}


def event_buffer(line: str, topic: str, max_bytes: int, *, max_age: float | None = None,
                 max_msg_size: int | None = None) -> StreamConfig:
    """A circular event buffer of `line`: stream `<TOPIC>_<line>` on subjects `<line>.<topic>.>`
    (erratum E-E3B-CUT-5). Full, the server drops the oldest and takes the write."""
    if type(topic) is not str or not _LOWER.fullmatch(topic):
        raise ValueError("buffer_topic")
    fields: dict = {"subjects": [f"{line}.{topic}.>"]}
    if max_age is not None:
        fields["max_age"] = max_age
    if max_msg_size is not None:
        fields["max_msg_size"] = max_msg_size
    return _build(CIRCULAR, f"{topic.upper()}_{line}", max_bytes, fields, _line_metadata(line, Role.EVENTS))


def _keyed(name: str, table: KeyTable, metadata: Mapping[str, str]) -> StreamConfig:
    """A sticky KV bucket `KV_<name>` sized by `table`, which rides in its metadata."""
    _check_subjects(f"$KV.{name}.", table)
    return _build(STICKY, f"KV_{name}", table.budget(f"$KV.{name}."), _kv_fields(name, table.history, table.largest),
                  {**metadata, TABLE_KEY: table.encoded()})


def _check_subjects(prefix: str, table: KeyTable) -> None:
    # Every stored subject fits the control line both servers enforce, with room for what a client adds.
    if any(len((prefix + key).encode()) > MAX_PUBLISH_SUBJECT for key in table.sizes):
        raise ValueError("key_table_key")


def state_bucket(line: str, table: KeyTable) -> StreamConfig:
    """`line`'s state bucket `KV_state_<line>`, sticky by `table` plus the reserved BIRTH_KEY and
    OUTBOX_KEY: its byte cap is that table's budget, and the session refuses a put outside it."""
    if BIRTH_KEY in table.sizes or OUTBOX_KEY in table.sizes:
        raise ValueError("state_key_reserved")
    full = KeyTable({**table.sizes, BIRTH_KEY: RESERVED_STATE_BYTES, OUTBOX_KEY: RESERVED_STATE_BYTES},
                    table.history)
    return _keyed(f"state_{line}", full, _line_metadata(line, Role.STATE))


def desired_bucket(line: str, table: KeyTable) -> StreamConfig:
    """`line`'s desired bucket `KV_desired_<line>`, sticky by its document table: its byte cap is the
    table's budget, and every `DocumentWriter` refuses a write outside it."""
    return _keyed(f"desired_{line}", table, _line_metadata(line, Role.DESIRED))


WALL_PREFIX: Final = "wall."   # the subject of wall key k is WALL_PREFIX + k, in WALL and every mirror


def wall_config(table: KeyTable, *, first_seq: int) -> StreamConfig:
    """The hub's wall-wide stream: sticky by the wall table (in its metadata), at WALL_STREAM_BYTES,
    the frozen cap its mirrors share (Q3). `first_seq` continues a lost WALL past Central's last
    acknowledged sequence plus a margin (E3b design §9.6, erratum E-W1-E3a-R-4)."""
    if table.budget(WALL_PREFIX) > WALL_STREAM_BYTES:
        raise ValueError("wall_table_over_budget")
    _check_subjects(WALL_PREFIX, table)
    return _build(STICKY, WALL_STREAM, WALL_STREAM_BYTES,
                  dict(subjects=[WALL_PREFIX + ">"], max_msgs_per_subject=table.history, max_msgs=-1,
                       first_seq=first_seq),
                  {ROLE_KEY: Role.WALL.value, TABLE_KEY: table.encoded()})


def wall_mirror_config() -> StreamConfig:
    """A Node's read-only local mirror of WALL, sticky as its origin: max_msgs_per_subject 1 and the
    same frozen cap (erratum E-W1-E3a-R-1). It carries no table: no one writes it."""
    return _build(STICKY, WALL_STREAM, WALL_STREAM_BYTES, dict(
        max_msgs_per_subject=1, max_msgs=-1, mirror=StreamSource(
            name=WALL_STREAM, external=ExternalStream(api=WALL_API_PREFIX, deliver=WALL_DELIVER_PREFIX))),
                  {ROLE_KEY: Role.WALL.value})


def role_of(config: StreamConfig) -> Role | None:
    """The stream's role; None for a stream that is not a self-describing Node stream."""
    value = (config.metadata or {}).get(ROLE_KEY)
    return Role(value) if value in Role._value2member_map_ else None


def table_of(config: StreamConfig) -> KeyTable | None:
    text = (config.metadata or {}).get(TABLE_KEY)
    return KeyTable.decoded(text) if text else None


def _subject_matches(pattern: str, subject: str) -> bool:
    tokens, wanted = subject.split("."), pattern.split(".")
    for index, token in enumerate(wanted):
        if token == ">":
            return len(tokens) > index
        if index >= len(tokens) or token not in ("*", tokens[index]):
            return False
    return len(tokens) == len(wanted)


@dataclass(frozen=True)
class Slice:
    """One component's buffers inside one store line: built here, within the line's bytes and
    streams, in the line's namespace, with exactly one state bucket. It ships with the release."""
    line: str
    buffers: tuple[StreamConfig, ...]

    def __post_init__(self) -> None:
        if self.line not in STORE_LINES:
            raise ValueError("slice_unknown_line")
        buffers = tuple(self.buffers)
        object.__setattr__(self, "buffers", buffers)
        for config in buffers:
            if role_of(config) in (None, Role.WALL) or EPOCH_KEY in config.metadata:
                raise ValueError("slice_unbuilt_buffer")
            if config.metadata.get(NAMESPACE_KEY) != self.line:
                raise ValueError("slice_outside_its_line")
        if len({config.name for config in buffers}) != len(buffers):
            raise ValueError("slice_names")
        line = self.store_line
        if len(buffers) > line.streams or sum(config.max_bytes for config in buffers) > line.max_bytes:
            raise ValueError("slice_past_its_line")
        if sum(role_of(config) is Role.STATE for config in buffers) != 1:
            raise ValueError("slice_needs_state")

    @property
    def store_line(self) -> StoreLine:
        return STORE_LINES[self.line]

    @property
    def digest(self) -> str:
        """sha256 of the canonical slice; a session's birth carries it."""
        canonical = {"line": self.line,
                     "buffers": sorted((config.as_dict() for config in self.buffers), key=lambda c: c["name"])}
        return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def captures(self, subject: str) -> str | None:
        """The stream of this slice whose subjects take `subject`, or None."""
        for config in self.buffers:
            if any(_subject_matches(pattern, subject) for pattern in config.subjects or ()):
                return config.name
        return None


async def apply(jetstream: JetStreamContext, slice_: Slice) -> Mapping[str, str]:
    """Stream -> epoch for every buffer of the slice: each absent one is created (stamped as
    `declare` stamps it), each existing one is kept as it is (S1; S4 adds prune, purge, shrink and
    grow)."""
    epochs = {}
    for config in slice_.buffers:
        await declare(jetstream, config)
        epochs[config.name] = await stream_epoch(jetstream, config.name)
    return MappingProxyType(epochs)
