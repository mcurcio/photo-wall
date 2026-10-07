"""The names both ends of a Node's leaf link derive on their own (decision 0017, C4/C5, API6).

No subject grammar lives here: each component owns its subjects. Fleet's hub generator, the
Node API library and Central's sessions read these names; nothing here crosses a wire except
the user names, which both sides derive from the serial.

The buffer rule (owner steer 2026-10-06, errata E-W1-BUF-2, E-W1-TD-4) needs no number here: every
stream, bucket and mirror is a JetStream limits stream with discard old, built only by `nodeapi`;
event buffers drop their oldest when full, sticky documents are never full. The numbers both ends
bind are the leaf's largest message, the largest stored one, the Node's store and pending caps, and
the bus's memory fence the store must fit (E-W1-STORE-1).
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
# The one place these numbers live: nodeapi's class table and the CI fence test read them here, and
# the line table (E2a) and the unit (E3c) are to read them here too.
NODE_BUS_MEMORY_MAX: Final = 224 * 1024 * 1024
NODE_BUS_GOMEMLIMIT: Final = 140 * 1024 * 1024
# What the server holds that GOMEMLIMIT does not count (thread stacks, runtime metadata, kernel
# memory charged to the cgroup): the low end of the measured 4-20 MiB.
NODE_BUS_HEADROOM: Final = 4 * 1024 * 1024
# What the fence leaves for the store's pages: every class table's store bound fits it (nodeapi).
NODE_BUS_STORE_ROOM: Final = NODE_BUS_MEMORY_MAX - NODE_BUS_GOMEMLIMIT - NODE_BUS_HEADROOM

# nats-server 2.15.0's file-store block size for a stream with a byte cap (ns:server/stream.go:1595-1607,
# ns:server/filestore.go:373-393): cap // 4 + 1 rounded up to 100 bytes, then the 32,000-byte minimum
# at or under it, the 8 MiB maximum at or over it, and 4 MiB between. So a cap under 128,000 bytes gets
# 32,000-byte blocks and one from 128,000 up to 32 MiB gets 4 MiB blocks. No option changes it.
FILESTORE_MIN_BLOCK: Final = 32_000
FILESTORE_MEDIUM_BLOCK: Final = 4 * 1024 * 1024
FILESTORE_MAX_BLOCK: Final = 8 * 1024 * 1024


def filestore_block_bytes(max_bytes: int) -> int:
    """The block size nats-server 2.15.0 gives a file stream capped at `max_bytes`. A discard-old
    stream frees a block only when its last message goes, so its files reach its cap plus one block."""
    if max_bytes <= 0:
        raise ValueError("filestore_needs_a_byte_cap")
    size = max_bytes // 4 + 1
    size += -size % 100
    if size <= FILESTORE_MIN_BLOCK:
        return FILESTORE_MIN_BLOCK
    return FILESTORE_MAX_BLOCK if size >= FILESTORE_MAX_BLOCK else FILESTORE_MEDIUM_BLOCK


# node-bus.conf's max_pending: what the server queues for one local client before it closes it as a
# slow consumer. nodeapi caps every pull at half of it (E-W1-TD-3).
NODE_MAX_PENDING: Final = 2 * 1024 * 1024

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

