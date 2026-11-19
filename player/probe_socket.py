"""The Player's end of the broker's probe socket (what stays of the retired app link)."""
from __future__ import annotations

import socket
import stat
import struct
from pathlib import Path

from contracts.node_app_link import PLAYER_PROBE_SOCKET

CONNECT_TIMEOUT = 1.0


class ProbeSocketError(ValueError):
    """One bounded local diagnostic; never contains wire bytes."""


def root_socket(path: Path = Path(PLAYER_PROBE_SOCKET)) -> socket.socket:
    """Require a protected directory and the actual connected peer to be root.

    The socket-path owner/type check gives useful diagnostics. The protected
    parent prevents a non-root path swap; SO_PEERCRED/getpeereid checks the
    actual connected peer before the Player speaks.
    """
    if not path.is_absolute():
        raise ProbeSocketError("socket_path")
    parent = path.parent
    while True:
        try:
            info = parent.lstat()
        except OSError as error:
            raise ProbeSocketError("socket_directory") from error
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ProbeSocketError("socket_directory")
        if parent == parent.parent:
            break
        parent = parent.parent
    try:
        info = path.lstat()
    except OSError as error:
        raise ProbeSocketError("socket_unavailable") from error
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0:
        raise ProbeSocketError("socket_owner")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    connection.settimeout(CONNECT_TIMEOUT)
    try:
        connection.connect(str(path))
        if hasattr(socket, "SO_PEERCRED"):
            raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                        struct.calcsize("=iII"))
            pid, uid, _ = struct.unpack("=iII", raw)
            if pid <= 0 or uid != 0:
                raise ProbeSocketError("socket_peer")
        elif hasattr(connection, "getpeereid"):
            uid, _ = connection.getpeereid()
            if uid != 0:
                raise ProbeSocketError("socket_peer")
        else:
            raise ProbeSocketError("socket_peer_unverifiable")
        return connection
    except BaseException:
        connection.close()
        raise


__all__ = ["CONNECT_TIMEOUT", "ProbeSocketError", "root_socket"]
