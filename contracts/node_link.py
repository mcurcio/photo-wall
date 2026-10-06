"""The names both ends of a Node's leaf link derive on their own (decision 0017, C4/C5, API6).

No subject grammar lives here: each component owns its subjects. Fleet's hub generator, the
Node API library and Central's sessions read these names; nothing here crosses a wire except
the user names, which both sides derive from the serial.
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
