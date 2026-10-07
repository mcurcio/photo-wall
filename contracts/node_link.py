"""The Node's leaf link: the names both ends derive on their own, and the subject contract of the leaf
(decision 0017, C4/C5/C18, API6).

Both ends read this module. Fleet's hub generator turns it into the hub's accounts and user
permissions, the shipped node-bus.conf repeats it literally (the config test binds the two), and the
Node API library and Central's sessions take their inbox prefix, numbers and fit check from it.
Nothing here crosses a wire except the user names, which both sides derive from the serial.

The leaf rule (errata E-W1-LEAF-1, E-W1-LEAF-2): nothing crosses the leaf toward the hub except
what Central asked for and the WALL mirror's own traffic, and a Node program cannot make anything else
cross. The Node's server holds three accounts. Programs connect into NODE_ACCOUNT; the leaf binds
LEAF_ACCOUNT, which holds no client and no stream; the WALL mirror lives alone in MIRROR_ACCOUNT, which
holds no client either. The hub's interest (Central's inboxes, the WALL consumer API and flow control)
lives only in LEAF_ACCOUNT, and nothing in NODE_ACCOUNT routes to it: LEAF_ACCOUNT reaches the Node's
API only through service imports (NODE_SERVICES, NODE_PULL_SERVICE), so a reply goes back only on a
response the server tracks for one of Central's requests. A client may not name a tracked response
(`_R_.`) as a reply subject (ns:server/client.go:4400-4405), so neither a program nor JetStream on its
behalf can aim a reply at one; a program may answer a request it was delivered once (`allow_responses`)
and sees no other tracked response (LOCAL_SUBSCRIBE_DENY). Every import but the pull's is answered once
(singleton), whatever a program does; a pull's replies come from JetStream alone, up to the bytes the
pull asked for. LEAF_ACCOUNT's interest toward the hub is its fixed imports, so subscription churn on
the Node announces nothing. MIRROR_ACCOUNT reaches the hub only through LEAF_ACCOUNT's WALL imports
(WALL_LINK_SERVICES); programs reach the mirror only through MIRROR_SERVICES, under MIRROR_API_PREFIX,
whose replies are tracked responses into NODE_ACCOUNT. The hub enforces the same lists on the Node's
leaf user (LEAF_EXPORTS out, LEAF_IMPORTS in) and on Central (CENTRAL_PUBLISH, CENTRAL_SUBSCRIPTIONS).

The buffer rule (owner steer 2026-10-06, errata E-W1-BUF-2, E-W1-TD-4) needs no number here: every
stream, bucket and mirror is a JetStream limits stream with discard old, built only by `nodeapi`;
event buffers drop their oldest when full, sticky documents are never full. The numbers both ends
bind are the leaf's largest message, the largest stored one, the Node's store, stream count and
pending caps, and the bus's memory fence the store must fit (E-W1-STORE-1, E-W1-FIT-1).
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
# subject) can pass L, and Central asking one across the leaf closes the leaf, again on every retry.
# nodeapi sends neither (its STREAM.INFO carries no options); nothing stops a raw client that does
# (E-W1-TD-9). STREAM.LIST/NAMES and CONSUMER.LIST/NAMES, which grow too, do not reach a Node from
# Central at all: NODE_SERVICES leaves them out (E-W1-LEAF-2).
MAX_STORED_MESSAGE: Final = (NODE_MAX_PAYLOAD - REPLY_ENVELOPE) * 3 // 4
# node-bus.conf's max_file_store: the whole Node store a class table may split (the config test binds it).
NODE_STORE_BYTES: Final = 12 * 1024 * 1024

# The bus's memory fence (owner answer STORE1 = A, erratum E-W1-STORE-1): every Node buffer is a file
# stream whose store directory is tmpfs, so the store's pages are RAM charged to the bus's cgroup and
# stay charged across a restart of the server in that cgroup. The bus unit (E3c) runs nats-server with
# MemoryMax = NODE_BUS_MEMORY_MAX and Environment=GOMEMLIMIT=NODE_BUS_GOMEMLIMIT, and the memory
# line table's bus line is NODE_BUS_MEMORY_MAX. Measured on 2.15.0 linux-arm64 with every buffer full
# and written flat out: 224/140 MiB survived 10 minutes; without GOMEMLIMIT every fence OOM-looped.
# The one place these numbers live: nodeapi's class table and the CI fence test read them here, and
# the line table (E2a) and the unit (E3c) are to read them here too.
NODE_BUS_MEMORY_MAX: Final = 224 * 1024 * 1024
NODE_BUS_GOMEMLIMIT: Final = 140 * 1024 * 1024
# What the server holds that GOMEMLIMIT does not count (thread stacks, runtime metadata, kernel
# memory charged to the cgroup): the low end of the measured 4-20 MiB.
NODE_BUS_HEADROOM: Final = 4 * 1024 * 1024
# The streams the Node's server holds in all: node-bus.conf's NODE_ACCOUNT `max_streams` (16) plus
# MIRROR_ACCOUNT's (1, the WALL mirror). The server refuses a stream past either at create (10027),
# never a write into a full one (erratum E-W1-FIT-1). With max_file_store capping the sum of every cap
# (the mirror's included), it bounds the store's files for any client and any class table.
NODE_MAX_STREAMS: Final = 17
MIRROR_MAX_STREAMS: Final = 1
NODE_ACCOUNT_MAX_STREAMS: Final = NODE_MAX_STREAMS - MIRROR_MAX_STREAMS
# node-bus.conf's `max_consumers` on both JetStream accounts: the consumers one stream may have (the
# server's default is 1,000, ns:server/jetstream_api.go:418), refused at create (10026). Central's
# durable, a component's watcher, Health's, and room for an ordered consumer's reset.
# NOT in the fit below (erratum E-W1-LEAF-2, the owner's to decide): each durable keeps three state
# files on the store's tmpfs (a page each: 48 KiB on the Pi's 16 KiB pages) and about 50 KiB of heap
# (49.4 KiB measured on 2.15.0 darwin-arm64), about 6.5 MiB for 17 x 4 consumers, and the fit has
# zero slack. The limit bounds them; nothing yet charges them.
NODE_MAX_CONSUMERS: Final = 4
# The most one file stream's block can be: nats-server 2.15.0 fixes a stream's block when it creates
# it, from its cap (ns:server/stream.go:1595-1607, ns:server/filestore.go:373-393): cap // 4 + 1 rounded
# up to 100 bytes, 32,000 at or under that, 8 MiB at or over 8 MiB, 4 MiB between. A cap is at most
# NODE_STORE_BYTES, so no block passes 4 MiB. A discard-old stream frees a block only when its last
# message goes, so its files reach its cap plus one block; every stream is charged this flat bound,
# whatever cap it was created with or has since.
FILESTORE_BLOCK_BOUND: Final = 4 * 1024 * 1024
_FILESTORE_MAX_BLOCK = 8 * 1024 * 1024   # nats-server's largest block, which no cap here may reach

# The fit (E-W1-FIT-1), checked here at import so no build of this tree can ship a store past its
# fence: every byte the store may hold (12 MiB) plus a block per stream (17 x 4 MiB) plus the heap
# GOMEMLIMIT allows (140 MiB) plus the headroom (4 MiB) is 224 MiB, the fence, with zero slack.
if (NODE_STORE_BYTES + NODE_MAX_STREAMS * FILESTORE_BLOCK_BOUND + NODE_BUS_GOMEMLIMIT + NODE_BUS_HEADROOM
        > NODE_BUS_MEMORY_MAX or -(-(NODE_STORE_BYTES // 4 + 1) // 100) * 100 >= _FILESTORE_MAX_BLOCK):
    raise RuntimeError("node_bus_store_past_its_fence")


# node-bus.conf's max_pending: what the server queues for one local client before it closes it as a
# slow consumer. nodeapi caps every pull at half of it (E-W1-TD-3).
NODE_MAX_PENDING: Final = 2 * 1024 * 1024

# Central's inbox namespace: Central's every hub client connects with this inbox prefix, so every
# reply to it, and only a reply to it, starts with `_CENTRAL.`.
CENTRAL_INBOX_PREFIX: Final = "_CENTRAL"
# A component's methods are `<component>.method.<name>` (C4): the one place Central sends a core
# request into the Node, besides the APIs below.
METHOD_TOKEN: Final = "method"

# The Node's three accounts (node-bus.conf; the config test binds it to everything below). Programs
# land in NODE_ACCOUNT (no_auth_user) and own its JetStream; the leaf binds LEAF_ACCOUNT; the WALL
# mirror is MIRROR_ACCOUNT's one stream. Neither of the last two has a user (E-W1-LEAF-2).
NODE_ACCOUNT: Final = "API"
LEAF_ACCOUNT: Final = "LEAF"
MIRROR_ACCOUNT: Final = "MIRROR"
# What NODE_ACCOUNT exports to LEAF_ACCOUNT, which imports each at the same subject: Central's whole
# reach into the Node. The JetStream API Central uses (stream info and message reads, its consumers,
# reached as `$JS.node.API.<verb>` through the domain mapping the leaf adds to LEAF_ACCOUNT,
# ns:server/leafnode.go:2115-2122), a bucket write (`$JS.node.API.$KV.` maps to `$KV.`),
# acknowledgements, service discovery and component methods. Each is answered once: the server keeps
# a request's tracked response for its first reply only (singleton), so however many programs answer,
# one reply per request crosses. Nothing that deletes, purges or changes a stream, and nothing whose
# reply grows with the store (LIST, NAMES) is here, so Central's request for one is dropped on the
# Node. `$SRV.PING` is answered by the first micro service only: Central addresses a component by
# name (`$SRV.PING.<name>`, `$SRV.INFO.<name>`).
NODE_SERVICES: Final = (
    "$JS.API.STREAM.INFO.*",
    "$JS.API.STREAM.MSG.GET.*",
    "$JS.API.DIRECT.GET.>",
    "$JS.API.CONSUMER.CREATE.>",
    "$JS.API.CONSUMER.DURABLE.CREATE.>",
    "$JS.API.CONSUMER.DELETE.>",
    "$JS.API.CONSUMER.INFO.>",
    "$KV.>",
    "$JS.ACK.>",
    "$SRV.>",
    f"*.{METHOD_TOKEN}.>",
)
# Central's pull: its export streams replies (many per request), which only JetStream sends: no
# program sees a pull's tracked response (LOCAL_SUBSCRIBE_DENY), so none can answer one, and JetStream
# sends at most the bytes the pull asked for (nodeapi.pull). The server's default response threshold
# (2 minutes, ns:server/const.go:237) ends a pull's route: no pull waits longer.
NODE_PULL_SERVICE: Final = "$JS.API.CONSUMER.MSG.NEXT.>"
# What a local program may not publish. A tracked response by name (`_R_.`): it answers a request it
# was delivered through `allow_responses`, once. And whatever removes a message or changes a stream's
# limits: a user delete or purge writes a tombstone into a new block, which the store frees only at
# its 2-minute sync (ns:server/filestore.go:6239-6249), and an update re-caps a stream past the block
# it was created with (E-W1-FIT-1, E-W1-LEAF-2). The domain form a local client may also use
# (`$JS.node.API.`) is mapped to `$JS.API.` before the server checks a publish (ns:server/parser.go:518-521,
# ns:server/jetstream.go:819-830), so this one form covers both.
STORE_CHANGING_API: Final = ("STREAM.MSG.DELETE.>", "STREAM.PURGE.>", "STREAM.UPDATE.>")
LOCAL_PUBLISH_DENY: Final = ("_R_.>", *(f"$JS.API.{verb}" for verb in STORE_CHANGING_API))
# What a local program may not subscribe to: every tracked response, and the JetStream API's requests,
# Central's pulls among them, whose replies are tracked responses.
LOCAL_SUBSCRIBE_DENY: Final = ("_R_.>", "$JS.API.>")

# What a Node's mirror of WALL needs from the WALL account: the consumer API it creates and deletes its
# mirror consumer through (imported under WALL_API_PREFIX), and flow control (v1 and v2 forms). The
# consumer create is a wildcard, never the literal `CREATE.WALL` (ns:server/sublist.go:169-195).
WALL_CONSUMER_SERVICES: Final = (
    "$JS.API.CONSUMER.CREATE.*",
    f"$JS.API.CONSUMER.CREATE.{WALL_STREAM}.>",
    f"$JS.API.CONSUMER.DELETE.{WALL_STREAM}.*",
)
WALL_FLOW_CONTROL_SERVICES: Final = (f"$JS.FC.{WALL_STREAM}.>", f"$JS.FC.*.*.{WALL_STREAM}.>")
# The same as the Node sees them: LEAF_ACCOUNT exports each to MIRROR_ACCOUNT, and the mirror's
# deliveries (WALL_DELIVER_PREFIX) as a stream.
WALL_LINK_SERVICES: Final = (
    *(WALL_API_PREFIX + subject.removeprefix("$JS.API") for subject in WALL_CONSUMER_SERVICES),
    *WALL_FLOW_CONTROL_SERVICES,
)
# What MIRROR_ACCOUNT exports to NODE_ACCOUNT, imported there under MIRROR_API_PREFIX: a program
# declares and reads the wall copy through `client.jetstream(prefix=MIRROR_API_PREFIX)`. No update,
# delete or purge: the mirror keeps the configuration it was created with until the store is lost.
MIRROR_API_PREFIX: Final = "MIRROR.API"
MIRROR_SERVICES: Final = tuple(f"$JS.API.{verb}.{WALL_STREAM}" for verb in ("STREAM.INFO", "STREAM.CREATE",
                                                                            "STREAM.MSG.GET"))

# The leaf's outbound list: the only subjects a message may cross the leaf toward the hub on, the
# Node's leaf user's publish allow-list. The hub sends it to the Node in its INFO and the Node checks
# every message against it before it queues one on the leaf (ns:server/leafnode.go:1716-1735,
# client.go:3811-3818). Replies to Central, the mirror's consumer API and its flow control.
LEAF_EXPORTS: Final = (f"{CENTRAL_INBOX_PREFIX}.>", *WALL_LINK_SERVICES)
# What Central may send into a Node account (its publish allow-list): the Node's JetStream API,
# acknowledgements, service discovery and component methods.
CENTRAL_PUBLISH: Final = (f"$JS.{NODE_DOMAIN}.API.>", "$JS.ACK.>", "$SRV.>", f"*.{METHOD_TOKEN}.>")
# The leaf's inbound list: the only subjects the hub carries to the Node, the leaf user's subscribe
# allow-list: what Central sends, the mirror's deliveries, and the replies to the mirror's own
# requests, which reach the hub with a reply that is LEAF_ACCOUNT's tracked response (`_R_.`).
LEAF_IMPORTS: Final = (*CENTRAL_PUBLISH, f"{WALL_DELIVER_PREFIX}.>", "_R_.>")
# Central's subscribe allow-list: a wildcard inbox (`_CENTRAL.<id>.*`) only (nodeapi.pull subscribes
# `<inbox>.*`).
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

