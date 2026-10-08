"""The Node's leaf link: the names both ends derive on their own, and the subject contract of the leaf
(decision 0017, C4/C5/C18, API6).

Both ends read this module. Fleet's hub generator turns it into the hub's accounts and user
permissions, the shipped node-bus.conf repeats it literally (the config test binds the two), and the
Node API library and Central's sessions take their inbox prefix, numbers and fit check from it.
Nothing here crosses a wire except the user names, which both sides derive from the serial.

The leaf rule (erratum E-W1-LEAF-1): nothing crosses the leaf toward the hub except replies to
Central, the WALL mirror's own consumer API and flow control (LEAF_EXPORTS). Central reaches into the
Node only on LEAF_IMPORTS: the Node's JetStream API, acknowledgements, service discovery, component
methods (`<component>.method.<name>`) and the mirror's deliveries. The hub enforces both lists on the
Node's leaf user, and Central may subscribe only to two-token-deep inboxes under CENTRAL_INBOX_PREFIX,
so no Node stream can push to it. A Node program built on `nodeapi` publishes on no export, so it
queues nothing toward a stalled hub: the leaf carries only what Central's own requests and pulls asked
for. A raw client can still publish on an export, aim a JetStream API reply at Central's inbox or churn
subscriptions on an import; that is out of scope (R16), never normal operation, and a restart of the
bus clears it (erratum E-W1-CONS-3; the local deny list L3 is gone, E3b design §12).

The buffer rule (owner steer 2026-10-06, errata E-W1-BUF-2, E-W1-TD-4) needs no number here: every
stream, bucket and mirror is a JetStream limits stream with discard old, built only by `nodeapi`;
event buffers drop their oldest when full, sticky documents are never full. The numbers both ends
bind are the leaf's largest message, the largest stored one, the Node's store, stream, consumer and
pending caps, and the bus's memory fence the memory store must fit (E3b design §6, E-W1-CONS-2).
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

NODE_DOMAIN: Final = "node"                  # every Node bus's JetStream domain; Central reaches it as $JS.node.API
HUB_DOMAIN: Final = "hub"                    # the hub's JetStream domain; only the wall-wide account uses JetStream there
WALL_ACCOUNT: Final = "WALL"                 # the hub account holding the wall-wide stream (API6)
WALL_STREAM: Final = "WALL"                  # its one stream, latest per subject; each Node keeps a mirror of it
WALL_STREAM_BYTES: Final = 512 * 1024        # max_bytes of the hub stream AND of every Node mirror (one number)
WALL_API_PREFIX: Final = "ACC.WALL.API"      # a Node account's import prefix for WALL's consumer API
WALL_DELIVER_PREFIX: Final = "DELIVER.WALL"  # the mirror's delivery prefix, the same in every Node account
WALL_WRITER_USER: Final = "central-wall"     # Central's user in WALL; a selector, not a secret

NODE_BUS_PORT: Final = 4222                                # the bus's client port on 127.0.0.1, on every Node
NODE_BUS_URL: Final = f"nats://127.0.0.1:{NODE_BUS_PORT}"  # what every Node component and the local UI dial
# The boot origin's path prefix routed to the hub's leaf listener (E3d/E4): a Node's leaf dials
# `<origin>/photo-wall/bus/leafnode` (nats-server appends `/leafnode`), over ws for an http origin and
# wss for https, with the origin's host and port unchanged (erratum E-E3C-CUT-5).
LEAF_PATH: Final = "photo-wall/bus"

# L, the largest message (headers + payload) on either end of a leaf: node-bus.conf's max_payload AND
# the hub's (the config test binds both). A message past a Node's max_payload that reached its leaf
# would close the leaf, so the hub refuses it first, at its own client (E-W1-BUF-3).
NODE_MAX_PAYLOAD: Final = 256 * 1024
# The longest control line (a PUB's subject, reply and sizes) a client may send on either end of a
# leaf: nats-server's default (MAX_CONTROL_LINE_SIZE, ns:server/const.go:90), pinned as node-bus.conf's
# max_control_line AND the hub's (the config test binds both) so an upgrade cannot move it under the
# envelope. It is the default, so it refuses nothing the base server accepted. The server closes a
# client that sends a longer line; a leaf is exempt (it carries only what some client sent). Every
# message a Node stores came through a client on one end, so no stored subject (a KV key included)
# is longer than this (E-W1-TD-6, E-W1-TD-8).
NODE_MAX_CONTROL_LINE: Final = 4096
# What a reply naming a stored message adds besides the message: the subject, JSON-escaped at worst
# six bytes per byte (Go's encoder writes `<` as `\u003c`), plus 1 KiB for the reply's own fields.
REPLY_ENVELOPE: Final = 6 * NODE_MAX_CONTROL_LINE + 1024
# The largest message any stream, bucket or mirror stores (headers + payload; the max_msg_size every
# nodeapi builder sets): 177,408 B. A server reply about ONE stored message is at most 4/3 of it plus
# the envelope: a JSON STREAM.MSG.GET base64-encodes it and names its subject, a direct get or a
# delivery adds a few headers. (L - REPLY_ENVELOPE) * 3/4 keeps every such reply under L whatever the
# subject (E-W1-TD-2, E-W1-TD-6).
# NOT covered: a reply whose size grows with the stream's state rather than with one message. A
# STREAM.INFO asking `deleted_details` (every interior delete) or `subjects_filter` (every matching
# subject), STREAM.LIST/NAMES and CONSUMER.LIST/NAMES can pass L, and Central asking one across the
# leaf closes the leaf, again on every retry. nodeapi sends none of them (its STREAM.INFO carries no
# options); nothing stops a raw client that does (E-W1-TD-9).
MAX_STORED_MESSAGE: Final = (NODE_MAX_PAYLOAD - REPLY_ENVELOPE) * 3 // 4
# node-bus.conf's max_memory_store: the whole Node store the store lines split (the config test binds it).
NODE_STORE_BYTES: Final = 12 * 1024 * 1024

# The bus's memory fence (owner answer Q1 = start clean, E3b design §6): every Node buffer, and the
# hub's WALL, is a memory stream, so every bus start is empty and nothing a store held outlives the
# server (E-W1-CRASH-1, E-W1-FIT-2 closed). The store is heap: the bus unit (E3c) runs nats-server with
# MemoryMax = NODE_BUS_MEMORY_MAX and Environment=GOMEMLIMIT=NODE_BUS_GOMEMLIMIT. 256 MiB since the
# consumer cap (owner: RAM is not a limiting factor; erratum E-W1-CONS-2). The one place these numbers
# live: the CI fence test reads them here, and the line table (E2a) and the unit (E3c) are to read
# them here too.
NODE_BUS_MEMORY_MAX: Final = 256 * 1024 * 1024
NODE_BUS_GOMEMLIMIT: Final = 141 * 1024 * 1024   # the smallest whole MiB the fit below holds at
# What the server holds that GOMEMLIMIT does not count (thread stacks, runtime metadata, kernel
# memory charged to the cgroup): the low end of the measured 4-20 MiB.
NODE_BUS_HEADROOM: Final = 4 * 1024 * 1024
# node-bus.conf's API account `max_streams`: the server refuses an 18th stream at create (10027), never
# a write into a full one (erratum E-W1-FIT-1).
NODE_MAX_STREAMS: Final = 17
# node-bus.conf's API account `max_consumers`: the most consumers one stream holds; the server refuses
# the next at create (10026), never a write (erratum E-W1-CONS-2).
NODE_MAX_CONSUMERS: Final = 12
# Heap per stored byte at the smallest nodeapi event: the memory store keeps each message as its own
# object, so a store of small messages costs a multiple of their bytes. Measured by the fence job's
# smallest-event leg on 2.15.0 linux-arm64: 4.33 to 5.13 (erratum E-E3C-S2-1; X14: 4.7 on darwin),
# rounded up.
MEMORY_STORE_FACTOR: Final = 6
# Heap per consumer: the measured anon growth per consumer (103.5 KiB: 272 idle consumers on 17
# streams, 2.15.0 linux-arm64; rounded up; E-W1-CONS-2).
CONSUMER_HEAP: Final = 104 * 1024
# The server's idle resident memory (X14: 43.9 MiB), rounded up.
NODE_BUS_BASELINE: Final = 48 * 1024 * 1024

# The fit (E3b design §7.2), checked here at import so no build of this tree can ship a store past its
# fence: the store at the heap factor (72 MiB), plus 12 consumers on each of 17 streams (20.72 MiB),
# plus the idle server (48 MiB) is 140.72 MiB, under GOMEMLIMIT (141 MiB); GOMEMLIMIT plus the
# headroom (145 MiB) is under the fence (256 MiB). The server caps the store (max_memory_store), the
# stream count and the consumers per stream for any client, so the fit holds whatever is declared.
# The fence job measures the factor on linux-arm64 with the smallest event on every run.
if (NODE_STORE_BYTES * MEMORY_STORE_FACTOR + NODE_MAX_STREAMS * NODE_MAX_CONSUMERS * CONSUMER_HEAP
        + NODE_BUS_BASELINE > NODE_BUS_GOMEMLIMIT
        or NODE_BUS_GOMEMLIMIT + NODE_BUS_HEADROOM > NODE_BUS_MEMORY_MAX):
    raise RuntimeError("node_bus_store_past_its_fence")


# node-bus.conf's max_pending: what the server queues for one local client before it closes it as a
# slow consumer. nodeapi caps every pull at half of it (E-W1-TD-3).
NODE_MAX_PENDING: Final = 2 * 1024 * 1024

# Central's inbox namespace: Central's every hub client connects with this inbox prefix, so every
# reply to it, and only a reply to it, starts with `_CENTRAL.`.
CENTRAL_INBOX_PREFIX: Final = "_CENTRAL"
# A component's method subjects are `<component>.method.<name>` (C4): the one place Central sends a
# core request into the Node, besides the APIs below.
METHOD_TOKEN: Final = "method"

# What a Node's mirror of WALL needs from the WALL account: the consumer API it creates and deletes its
# mirror consumer through (imported under WALL_API_PREFIX), and flow control (v1 and v2 forms). The
# consumer create is a wildcard, never the literal `CREATE.WALL`: a push consumer starts delivering
# only to a subject some subscription names literally (ns:server/sublist.go:169-195), so no Node
# consumer can push its stream at the hub's import of it.
WALL_CONSUMER_SERVICES: Final = (
    "$JS.API.CONSUMER.CREATE.*",
    f"$JS.API.CONSUMER.CREATE.{WALL_STREAM}.>",
    f"$JS.API.CONSUMER.DELETE.{WALL_STREAM}.*",
)
WALL_FLOW_CONTROL_SERVICES: Final = (f"$JS.FC.{WALL_STREAM}.>", f"$JS.FC.*.*.{WALL_STREAM}.>")

# The leaf's outbound list: the only subjects a message may cross the leaf toward the hub on, the
# Node's leaf user's publish allow-list. The hub sends it to the Node in its INFO and the Node checks
# every message against it before it queues one on the leaf (ns:server/leafnode.go:1716-1735,
# client.go:3811-3818). Replies to Central, the mirror's consumer API and its flow control.
LEAF_EXPORTS: Final = (
    f"{CENTRAL_INBOX_PREFIX}.>",
    *(WALL_API_PREFIX + subject.removeprefix("$JS.API") for subject in WALL_CONSUMER_SERVICES),
    *WALL_FLOW_CONTROL_SERVICES,
)
# The leaf's inbound list: the only subjects the hub carries to the Node, the leaf user's subscribe
# allow-list and Central's publish allow-list. The Node announces a local subscription to the hub
# only inside it, so subscription churn on the Node sends the hub nothing. The Node's JetStream API,
# acknowledgements, service discovery, component methods, the mirror's deliveries and its consumer
# create replies.
LEAF_IMPORTS: Final = (
    f"$JS.{NODE_DOMAIN}.API.>",
    "$JS.ACK.>",
    "$SRV.>",
    f"*.{METHOD_TOKEN}.>",
    f"{WALL_DELIVER_PREFIX}.>",
    "$JSC.R.>",
)
# Central's subscribe allow-list: a wildcard inbox (`_CENTRAL.<id>.*`) only. A literal inbox, the only
# subject a push consumer binds to, is refused at the hub (nodeapi.pull subscribes `<inbox>.*`).
CENTRAL_SUBSCRIPTIONS: Final = (f"{CENTRAL_INBOX_PREFIX}.*.*",)

# The envelope (E3b design §7.2): the header names `nodeapi.envelope` alone builds and reads.
MESSAGE_ID_HEADER: Final = "Nats-Msg-Id"              # the server's dedupe; Central's projection dedupe
SCHEMA_MAJOR_HEADER: Final = "Photo-Wall-Schema-Major"   # Central's adapter per major (C12)
WRITER_HEADER: Final = "Photo-Wall-Writer"            # documents' writer; a method call's caller
CENTRAL_WRITER: Final = "central"                     # every Central instance writes as this


class Pipe(StrEnum):
    """The two pipes (0017 R13): each stream, line and NodeLink belongs to one."""
    FLEET = "fleet"
    SHOW = "show"


@dataclass(frozen=True)
class StoreLine:
    """A component's share of the Node store (E3b design §7.3): a byte total and a stream count. The
    split inside it ships with the component's release (`nodeapi.buffers.Slice`)."""
    name: str        # [a-z]+: the line's namespace, in each stream's metadata and subjects
    pipe: Pipe
    declarer: str    # the component whose session applies this line
    streams: int
    max_bytes: int


_KIB = 1024
STORE_LINES: Final[Mapping[str, StoreLine]] = MappingProxyType({line.name: line for line in (
    StoreLine("host", Pipe.FLEET, "host", 2, 768 * _KIB),
    StoreLine("apps", Pipe.FLEET, "apps", 3, 1536 * _KIB),
    StoreLine("display", Pipe.FLEET, "display", 3, 1536 * _KIB),
    StoreLine("health", Pipe.FLEET, "health", 3, 1536 * _KIB),
    StoreLine("content", Pipe.SHOW, "content", 2, 1280 * _KIB),
    StoreLine("player", Pipe.SHOW, "apps", 3, 4608 * _KIB),   # the app line: apps declares it (§7.3)
)})
# Every line and the WALL mirror fit the store and the server's stream count, so no line's apply is
# refused for room by another's (11.5 of 12 MiB, 17 of 17 streams).
if (sum(line.max_bytes for line in STORE_LINES.values()) + WALL_STREAM_BYTES > NODE_STORE_BYTES
        or sum(line.streams for line in STORE_LINES.values()) + 1 > NODE_MAX_STREAMS):
    raise RuntimeError("store_lines_past_the_store")

# The enrolment serial's charset (central/content_catalog/catalog.py). A `:` would break a leaf
# URL's user:password@ and a `.` would split $SYS.ACCOUNT.<name> subjects, so the account name
# is a hash of the serial, never the serial itself.
_SERIAL = re.compile(r"[A-Za-z0-9:_.-]{1,128}")


def account_id(serial: str) -> str:
    """The Node's hub account name: "N" plus 24 hex digits of the serial's sha256."""
    if type(serial) is not str or not _SERIAL.fullmatch(serial):
        raise ValueError("node_link_serial")
    return "N" + hashlib.sha256(serial.encode()).hexdigest()[:24]


def node_user(serial: str) -> str:
    """The Node's leaf user in its account; a selector, not a secret."""
    return "node-" + account_id(serial)


def central_user(serial: str) -> str:
    """Central's client user inside the Node's account; a selector, not a secret."""
    return "central-" + account_id(serial)

