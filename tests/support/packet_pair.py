"""The one connected AF_UNIX packet pair every node-transport test uses.

Production speaks SOCK_SEQPACKET on every node socket (`connect_seqpacket`, the feeds, the health
socket, the probe channel). Linux tests get exactly that type, so they prove both packet
boundaries and the lifecycle a peer's close produces (EOF as `b""`, or ECONNRESET when unread
data was left behind). macOS has no AF_UNIX SOCK_SEQPACKET (EPROTONOSUPPORT), so there the pair is
SOCK_DGRAM: it proves packet boundaries only. A DGRAM peer's close is not EOF (darwin: the
survivor's `recv` raises ConnectionResetError; Linux DGRAM would never even become readable), so
a test that asserts close-on-EOF behaviour proves it on Linux only and must say so.
"""

from __future__ import annotations

import socket
import sys

LINUX = sys.platform.startswith("linux")
PACKET_TYPE = socket.SOCK_SEQPACKET if LINUX else socket.SOCK_DGRAM


def packet_pair() -> tuple[socket.socket, socket.socket]:
    """A connected (ours, theirs) packet pair: SOCK_SEQPACKET on Linux, SOCK_DGRAM elsewhere."""
    return socket.socketpair(socket.AF_UNIX, PACKET_TYPE)
