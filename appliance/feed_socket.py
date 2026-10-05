"""One node feed socket: a reader allowlist by kernel credential, one request per accept.

Every node feed publisher (the app broker's probe feed, the display controller's feed) serves
its readers through this listener. The publisher names its socket path, owner, group, reader
uids, answer (request document -> reply body) and reply bound; this module knows no context.
A peer outside the readers is closed with its request unread and no reply. Stdlib plus the
shared strict JSON reader (`contracts.strict_json`, as `appliance.boot_store`).
"""

from __future__ import annotations

import json
import os
import socket
import stat
import struct
from collections.abc import Callable
from pathlib import Path

from contracts.strict_json import loads_object

# The node feed policy shared by every publisher (sysusers in the node base .deb).
FEEDS_GROUP = 10007  # pw-node-feeds
FEED_READERS = frozenset({0, 10006})  # root, pw-health
MAX_REQUEST = 4096
READ_WAIT_SECONDS = 0.05
READS_PER_TURN = 8
SEQPACKET = getattr(socket, "SOCK_SEQPACKET", socket.SOCK_STREAM)
RESPONSE_BOUND = b'{"accepted":false,"reason":"response_bound"}'


def peer_uid(connection: socket.socket) -> int:
    """The connecting process's uid as the kernel recorded it (SO_PEERCRED)."""
    size = struct.calcsize("=iII")
    _pid, uid, _gid = struct.unpack("=iII", connection.getsockopt(
        socket.SOL_SOCKET, getattr(socket, "SO_PEERCRED", 17), size))
    return uid


class FeedListener:
    """Serves a feed: one request per accept, readers by SO_PEERCRED uid.

    Never blocks the caller's turn beyond a short bounded wait per accepted reader. The reply
    is `{"accepted": true, **answer(request)}`, or `{"accepted": false, "reason"}` when the
    request is malformed or `answer` refuses it (ValueError); an encoded reply over
    `max_reply` bytes is replaced by `response_bound`.
    """

    def __init__(self, path: Path, *, owner_uid: int, group: int, readers: frozenset[int],
                 answer: Callable[[dict], dict], max_reply: int, peer=peer_uid,
                 kind: int = SEQPACKET):
        self.path, self.readers, self.answer, self.peer = path, frozenset(readers), answer, peer
        self.owner_uid, self.max_reply = owner_uid, max_reply
        directory = path.parent.lstat()
        if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != owner_uid
                or directory.st_mode & 0o022):
            raise ValueError("feed_directory")
        self._remove_stale()
        self.listener = socket.socket(socket.AF_UNIX, kind)
        try:
            self.listener.bind(str(path))
            os.chown(path, owner_uid, group)
            os.chmod(path, 0o660)
            info = path.lstat()
            self.identity = info.st_dev, info.st_ino
            self.listener.listen(8)
            self.listener.setblocking(False)
        except BaseException:
            self.listener.close()
            raise

    def _remove_stale(self) -> None:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.owner_uid:
            raise ValueError("feed_socket_ownership")
        self.path.unlink()

    def serve(self, budget: int = READS_PER_TURN) -> None:
        for _ in range(budget):
            try:
                connection, _ = self.listener.accept()
            except (BlockingIOError, InterruptedError):
                return
            with connection:
                try:
                    if self.peer(connection) not in self.readers:
                        continue  # not a reader: closed without a reply
                    connection.settimeout(READ_WAIT_SECONDS)
                    raw = connection.recv(MAX_REQUEST + 1)
                except OSError:
                    continue
                try:
                    value = loads_object(raw, max_bytes=MAX_REQUEST)
                    if value is None:
                        raise ValueError("feed_read_request")
                    reply = {"accepted": True, **self.answer(value)}
                except ValueError as error:
                    reply = {"accepted": False, "reason": str(error)}
                wire = json.dumps(reply, default=str, separators=(",", ":")).encode()
                if len(wire) > self.max_reply:
                    wire = RESPONSE_BOUND
                try:
                    connection.sendall(wire)
                except OSError:
                    pass

    def __enter__(self) -> FeedListener:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        self.listener.close()
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        if (info.st_dev, info.st_ino) == self.identity:
            self.path.unlink()
