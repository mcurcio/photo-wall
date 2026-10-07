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
Node's leaf user, the Node's own server refuses every local publish on an export, and Central may
subscribe only to two-token-deep inboxes under CENTRAL_INBOX_PREFIX, so no Node stream can push to
it. A Node program built on `nodeapi` therefore queues nothing toward a stalled hub: the leaf carries
only what Central's own requests and pulls asked for. A raw client can still aim a JetStream API reply
at Central's inbox or churn subscriptions on an import; neither happens in normal operation and a
restart of the bus clears either (erratum E-W1-CONS-3).

The buffer rule (owner steer 2026-10-06, errata E-W1-BUF-2, E-W1-TD-4) needs no number here: every
stream, bucket and mirror is a JetStream limits stream with discard old, built only by `nodeapi`;
event buffers drop their oldest when full, sticky documents are never full. The numbers both ends
bind are the leaf's largest message, the largest stored one, the Node's store, stream, consumer and
pending caps, and the bus's memory fence the store must fit (E-W1-STORE-1, E-W1-FIT-1, E-W1-CONS-2).
"""
from __future__ import annotations

import hashlib
import re
from typing import Final

NODE_DOMAIN: Final = "node"                  # every Node bus's JetStream domain; Central reaches it as $JS.node.API
HUB_DOMAIN: Final = "hub"                    # the hub's JetStream domain; only the wall-wide account uses JetStream there
WALL_ACCOUNT: Final = "WALL"                 # the hub account holding the wall-wide stream (API6)
WALL_STREAM: Final = "WALL"                  # its one stream, latest per subject; each Node keeps a mirror of it
WALL_STREAM_BYTES: Final = 512 * 1024        # max_bytes of the hub stream AND of every Node mirror (one number)
WALL_API_PREFIX: Final = "ACC.WALL.API"      # a Node account's import prefix for WALL's consumer API
WALL_DELIVER_PREFIX: Final = "DELIVER.WALL"  # the mirror's delivery prefix, the same in every Node account
WALL_WRITER_USER: Final = "central-wall"     # Central's user in WALL; a selector, not a secret

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
# node-bus.conf's max_file_store: the whole Node store a class table may split (the config test binds it).
NODE_STORE_BYTES: Final = 12 * 1024 * 1024

# The bus's memory fence (owner answer STORE1 = A, erratum E-W1-STORE-1): every Node buffer is a file
# stream whose store directory is tmpfs, so the store's pages are RAM charged to the bus's cgroup and
# stay charged across a restart of the server in that cgroup. The bus unit (E3c) runs nats-server with
# MemoryMax = NODE_BUS_MEMORY_MAX and Environment=GOMEMLIMIT=NODE_BUS_GOMEMLIMIT, and the memory
# line table's bus line is NODE_BUS_MEMORY_MAX. Measured on 2.15.0 linux-arm64 with every buffer full
# and written flat out: 224/140 MiB survived 10 minutes; without GOMEMLIMIT every fence OOM-looped.
# 256 MiB since the consumer cap (owner: RAM is not a limiting factor; erratum E-W1-CONS-2).
# The one place these numbers live: nodeapi's class table and the CI fence test read them here, and
# the line table (E2a) and the unit (E3c) are to read them here too.
NODE_BUS_MEMORY_MAX: Final = 256 * 1024 * 1024
NODE_BUS_GOMEMLIMIT: Final = 140 * 1024 * 1024
# What the server holds that GOMEMLIMIT does not count (thread stacks, runtime metadata, kernel
# memory charged to the cgroup): the low end of the measured 4-20 MiB.
NODE_BUS_HEADROOM: Final = 4 * 1024 * 1024
# node-bus.conf's API account `max_streams`: the server refuses an 18th stream at create (10027), never
# a write into a full one (erratum E-W1-FIT-1). With max_file_store capping the sum of every cap, it
# bounds the store's files for any client and any class table.
NODE_MAX_STREAMS: Final = 17
# The most one file stream's block can be: nats-server 2.15.0 fixes a stream's block when it creates
# it, from its cap (ns:server/stream.go:1595-1607, ns:server/filestore.go:373-393): cap // 4 + 1 rounded
# up to 100 bytes, 32,000 at or under that, 8 MiB at or over 8 MiB, 4 MiB between. A cap is at most
# NODE_STORE_BYTES, so no block passes 4 MiB. A discard-old stream frees a block only when its last
# message goes, so its files reach its cap plus one block; every stream is charged this flat bound,
# whatever cap it was created with or has since.
FILESTORE_BLOCK_BOUND: Final = 4 * 1024 * 1024
_FILESTORE_MAX_BLOCK = 8 * 1024 * 1024   # nats-server's largest block, which no cap here may reach
# node-bus.conf's API account `max_consumers`: the most consumers one stream holds; the server refuses
# the next at create (10026), never a write (erratum E-W1-CONS-2). A durable consumer persists on the
# tmpfs store across a restart, and nats-py's KV keys, history and watch each hold one for up to
# 5 minutes, so without a cap normal use could grow past the fence and OOM-loop the bus on restart.
# The largest count whose charge fits the 256 MiB fence: 13 would need 256.8 MiB.
NODE_MAX_CONSUMERS: Final = 12
# What one consumer costs in the fence: its 3 state files (meta.inf, meta.sum, o.dat) on the tmpfs
# store, one 16 KiB Pi 5 page each, plus 104 KiB of heap, the measured anon growth per consumer
# (103.5 KiB: 272 durables on 17 streams, 2.15.0 linux-arm64, 4 KiB pages; rounded up).
CONSUMER_BOUND: Final = 3 * 16 * 1024 + 104 * 1024
# Every stream is charged its block and a full set of consumers, whatever it holds.
STREAM_BOUND: Final = FILESTORE_BLOCK_BOUND + NODE_MAX_CONSUMERS * CONSUMER_BOUND

# The fit (E-W1-FIT-1, E-W1-CONS-2), checked here at import so no build of this tree can ship a store
# past its fence: every byte the store may hold (12 MiB) plus, per stream, a block and 12 consumers
# (17 x (4 MiB + 12 x 152 KiB)) plus the heap GOMEMLIMIT allows (140 MiB) plus the headroom (4 MiB) is
# 254.28 MiB, under the 256 MiB fence by 1.72 MiB.
if (NODE_STORE_BYTES + NODE_MAX_STREAMS * STREAM_BOUND + NODE_BUS_GOMEMLIMIT + NODE_BUS_HEADROOM
        > NODE_BUS_MEMORY_MAX or -(-(NODE_STORE_BYTES // 4 + 1) // 100) * 100 >= _FILESTORE_MAX_BLOCK):
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

