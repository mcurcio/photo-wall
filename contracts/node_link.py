"""The names both ends of a Node's leaf link derive on their own (decision 0017, C4/C5, API6).

No subject grammar lives here: each component owns its subjects. Fleet's hub generator, the
Node API library and Central's sessions read these names; nothing here crosses a wire except
the user names, which both sides derive from the serial.

The buffer rule (owner steer 2026-10-06, erratum E-W1-BUF-1) lives here too, because both ends
size their stores by it: every stream, bucket and mirror drops its oldest when full and never
refuses a write, and every account's store holds its streams' caps plus one largest message, so
the account check (which runs before a stream drops its oldest) never refuses first. The hub runs
at the Nodes' max_payload, so no message from Central can close a leaf (erratum E-W1-BUF-2).
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import Final

NODE_DOMAIN: Final = "node"                  # every Node bus's JetStream domain; Central reaches it as $JS.node.API
HUB_DOMAIN: Final = "hub"                    # the hub's JetStream domain; only the wall-wide account uses JetStream there
WALL_ACCOUNT: Final = "WALL"                 # the hub account holding the wall-wide stream (API6)
WALL_STREAM: Final = "WALL"                  # its one stream, latest per subject; each Node keeps a mirror of it
WALL_STREAM_BYTES: Final = 512 * 1024        # max_bytes of the hub stream AND of every Node mirror (one number)
WALL_API_PREFIX: Final = "ACC.WALL.API"      # a Node account's import prefix for WALL's consumer API
WALL_DELIVER_PREFIX: Final = "DELIVER.WALL"  # the mirror's delivery prefix, the same in every Node account
WALL_WRITER_USER: Final = "central-wall"     # Central's user in WALL; a selector, not a secret

NODE_MAX_PAYLOAD: Final = 256 * 1024         # node-bus.conf's AND the hub's max_payload (headers + payload); the config test binds both
MAX_SUBJECT_BYTES: Final = 4096              # nats-server's default max_control_line, which bounds every subject
# WALL's max_msg_size: every wall message crosses a leaf into a Node account, where one past the
# Node's max_payload is a protocol error.
WALL_MESSAGE_BYTES: Final = NODE_MAX_PAYLOAD

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


def largest_message_charge(max_message: int) -> int:
    """The store's charge for the largest message a buffer takes: nats-server charges 30 + subject +
    payload, and 4 + headers more with headers (ns:server/filestore.go:10055-10062); `max_message`
    bounds headers + payload, MAX_SUBJECT_BYTES the subject. An upper bound no real message reaches."""
    return 34 + MAX_SUBJECT_BYTES + max_message


def account_store_bytes(stream_caps: Iterable[int], max_message: int) -> int:
    """The smallest account store that never refuses a write: the server adds the new message to the
    account's usage and refuses past its store before a full stream drops its oldest
    (ns:server/stream.go:7274, jetstream.go:2505), so the store holds every stream's cap plus one
    largest message (E-W1-BUF-1)."""
    return sum(stream_caps) + largest_message_charge(max_message)
