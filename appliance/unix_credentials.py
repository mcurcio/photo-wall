"""Linux per-packet credentials; close passed descriptors even on refused input."""
from __future__ import annotations

import os
import socket
import struct


def receive_credential_packet(connection: socket.socket, *, maximum: int) -> tuple[tuple[int, int, int], bytes]:
    size = struct.calcsize("=iII")
    raw, ancillary, flags, _ = connection.recvmsg(maximum + 1, socket.CMSG_SPACE(size),
                                                getattr(socket, "MSG_CMSG_CLOEXEC", 0))
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            for offset in range(0, len(data) - len(data) % 4, 4):
                try:
                    os.close(struct.unpack_from("=i", data, offset)[0])
                except OSError:
                    pass
    if not raw or len(raw) > maximum or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
        raise ValueError("invalid_packet")
    if len(ancillary) != 1 or ancillary[0][0] != socket.SOL_SOCKET or ancillary[0][1] != getattr(socket, "SCM_CREDENTIALS", 2) or len(ancillary[0][2]) != size:
        raise ValueError("invalid_peer")
    credentials = struct.unpack("=iII", ancillary[0][2])
    peer = struct.unpack("=iII", connection.getsockopt(socket.SOL_SOCKET, getattr(socket, "SO_PEERCRED", 17), size))
    if credentials != peer or credentials[0] <= 0:
        raise ValueError("invalid_peer")
    return credentials, raw
