"""Post-ACK node process proof over the root-owned base broker socket."""
from __future__ import annotations

import json
import os
import socket
import stat
import struct
from collections.abc import Callable
from pathlib import Path

from contracts.node_app_link import (
    MAX_NODE_LINK_BYTES,
    NodeAppLinkV2,
    encode_node_app_link,
    encode_node_app_link_begin,
    parse_node_app_link_challenge,
    parse_node_app_link_result,
)
from contracts.player_control import ControlAppliedReceipt
from contracts.strict_json import loads_object
from player.identity import Identity

CONNECT_TIMEOUT = 1.0


class NodeLinkError(ValueError):
    """One bounded local diagnostic; never contains key, bearer or wire bytes."""


def _send(connection: socket.socket, value: dict[str, object], *, limit: int) -> None:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > limit:
        raise NodeLinkError("packet_limit")
    if connection.send(raw) != len(raw):
        raise NodeLinkError("short_packet")


def _receive(connection: socket.socket, *, limit: int) -> dict[str, object]:
    raw, ancillary, flags, _ = connection.recvmsg(
        limit + 1, socket.CMSG_SPACE(256),
        getattr(socket, "MSG_CMSG_CLOEXEC", 0))
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            width = struct.calcsize("=i")
            for offset in range(0, len(data) - len(data) % width, width):
                descriptor = struct.unpack_from("=i", data, offset)[0]
                try:
                    os.close(descriptor)
                except OSError:
                    pass
    if (not raw or len(raw) > limit or ancillary
            or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)):
        raise NodeLinkError("packet_limit")
    value = loads_object(raw, max_bytes=limit)
    if value is None:
        raise NodeLinkError("invalid_packet")
    return value


def root_socket(path: Path) -> socket.socket:
    """Require a protected directory and the actual connected peer to be root.

    The socket-path owner/type check gives useful diagnostics. The protected
    parent prevents a non-root path swap; SO_PEERCRED/getpeereid checks the
    actual connected peer before any challenge can be signed.
    """
    if not path.is_absolute():
        raise NodeLinkError("socket_path")
    parent = path.parent
    while True:
        try:
            info = parent.lstat()
        except OSError as error:
            raise NodeLinkError("socket_directory") from error
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise NodeLinkError("socket_directory")
        if parent == parent.parent:
            break
        parent = parent.parent
    try:
        info = path.lstat()
    except OSError as error:
        raise NodeLinkError("socket_unavailable") from error
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0:
        raise NodeLinkError("socket_owner")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    connection.settimeout(CONNECT_TIMEOUT)
    try:
        connection.connect(str(path))
        if hasattr(socket, "SO_PEERCRED"):
            raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                        struct.calcsize("=iII"))
            pid, uid, _ = struct.unpack("=iII", raw)
            if pid <= 0 or uid != 0:
                raise NodeLinkError("socket_peer")
        elif hasattr(connection, "getpeereid"):
            uid, _ = connection.getpeereid()
            if uid != 0:
                raise NodeLinkError("socket_peer")
        else:
            raise NodeLinkError("socket_peer_unverifiable")
        return connection
    except BaseException:
        connection.close()
        raise


DEFAULT_NODE_LINK_SOCKET = Path("/run/photo-wall-client/app-link.sock")


class NodeAppLinkClient:
    def __init__(self, path: Path = DEFAULT_NODE_LINK_SOCKET, *, connector=root_socket):
        self.path, self.connector = path, connector

    def exchange_applied(self, *, identity: Identity, player_id: str, authority_epoch: int,
                         device_id: str, kernel_boot_id: str, receipt: ControlAppliedReceipt,
                         enrollment_current: Callable[[], bool]) -> str:
        if not enrollment_current():
            return "stale"
        receipt_json = json.dumps(receipt.model_dump(mode="json", by_alias=True),
                                  sort_keys=True, separators=(",", ":"))
        with self.connector(self.path) as connection:
            connection.settimeout(2.0)
            _send(connection, json.loads(encode_node_app_link_begin(player_id=player_id,
                  authority_epoch=authority_epoch, control_receipt=receipt_json)), limit=MAX_NODE_LINK_BYTES)
            packet = _receive(connection, limit=MAX_NODE_LINK_BYTES)
            if packet.get("kind") == "error":
                return "rejected"
            challenge = parse_node_app_link_challenge(json.dumps(packet).encode())
            if (challenge.player_id != player_id or challenge.authority_epoch != authority_epoch
                    or challenge.control_receipt != receipt_json
                    or challenge.producer.device_id != device_id
                    or str(challenge.producer.kernel_boot_id) != kernel_boot_id
                    or challenge.process.pid != os.getpid()):
                raise NodeLinkError("node_link_context_mismatch")
            if not enrollment_current():
                return "stale"
            proof: NodeAppLinkV2 = identity.sign_node_app_link(challenge)
            if not enrollment_current():
                return "stale"
            _send(connection, json.loads(encode_node_app_link(proof)), limit=MAX_NODE_LINK_BYTES)
            result = _receive(connection, limit=MAX_NODE_LINK_BYTES)
            try:
                status = parse_node_app_link_result(json.dumps(result).encode())
            except ValueError:
                return "rejected"
            # `accepted` (held by the broker) and `recorded` (by Central) both link this run.
            if status == "refused":
                return "rejected"
            return "recorded" if enrollment_current() else "stale"
