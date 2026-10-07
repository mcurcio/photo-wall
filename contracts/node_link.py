"""The names both ends of a Node's leaf link derive on their own (decision 0017, C4/C5, API6).

No subject grammar lives here: each component owns its subjects. Fleet's hub generator, the
Node API library and Central's sessions read these names; nothing here crosses a wire except
the user names, which both sides derive from the serial.

The buffer rule (owner steer 2026-10-06, errata E-W1-BUF-2, E-W1-TD-4) needs no number here: every
stream, bucket and mirror is a JetStream limits stream with discard old, built only by `nodeapi`;
event buffers drop their oldest when full, sticky documents are never full. The numbers both ends
bind are the leaf's largest message, the largest stored one, and the Node's store and pending caps.
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
# leaf: node-bus.conf's max_control_line AND the hub's (the config test binds both). The server
# closes a client that sends a longer one; a leaf is exempt (it carries only what some client sent).
# Every message a Node stores came through a client on one end, so no stored subject (a KV key
# included) is longer than this (E-W1-TD-6).
NODE_MAX_CONTROL_LINE: Final = 1024
# What a reply naming a stored message adds besides the message: the subject, JSON-escaped at worst
# six bytes per byte (Go's encoder writes `<` as `<`), plus 1 KiB for the reply's own fields.
REPLY_ENVELOPE: Final = 6 * NODE_MAX_CONTROL_LINE + 1024
# The largest message any stream, bucket or mirror stores (headers + payload; the max_msg_size every
# nodeapi builder sets). A server reply carrying a stored message is at most 4/3 of it plus the
# envelope: a JSON STREAM.MSG.GET base64-encodes it and names its subject, a direct get or a
# delivery adds a few headers. (L - REPLY_ENVELOPE) * 3/4 keeps every such reply under L whatever
# the subject, so no reply the Node generates can close the leaf (E-W1-TD-2, E-W1-TD-6).
MAX_STORED_MESSAGE: Final = (NODE_MAX_PAYLOAD - REPLY_ENVELOPE) * 3 // 4
# node-bus.conf's max_file_store: the whole Node store a class table may split (the config test binds it).
NODE_STORE_BYTES: Final = 12 * 1024 * 1024
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

