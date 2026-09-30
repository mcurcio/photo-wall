"""Best-effort Player-key proof to a root-owned local OS service.

This channel carries neither a Central bearer nor command authority. Its only
effect is to let the base OS record that the currently enrolled Player process
signed a challenge on the exact connected Unix socket.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from contracts.app_process_proof import (
    MAX_PROOF_PACKET_BYTES,
    AppProofBegin,
    AppProofChallengePacket,
    AppProofErrorPacket,
    AppProofResponsePacket,
    AppProofResultPacket,
)
from contracts.strict_json import loads_object
from player.identity import Identity

DEFAULT_PROOF_SOCKET = Path("/run/photo-wall/app-proof.sock")
PROOF_SOCKET_TIMEOUT = 8.0
PROOF_CONNECT_TIMEOUT = 1.0
ProofOutcome = Literal["recorded", "rejected", "stale"]


class LocalProofError(ValueError):
    """One bounded local diagnostic; never contains key, bearer or wire bytes."""


def _packet(value: dict[str, object]) -> bytes:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_PROOF_PACKET_BYTES:
        raise LocalProofError("packet_limit")
    return raw


def _receive(connection: socket.socket) -> dict[str, object]:
    raw, ancillary, flags, _ = connection.recvmsg(
        MAX_PROOF_PACKET_BYTES + 1, socket.CMSG_SPACE(256),
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
    if (not raw or len(raw) > MAX_PROOF_PACKET_BYTES or ancillary
            or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)):
        raise LocalProofError("packet_limit")
    value = loads_object(raw, max_bytes=MAX_PROOF_PACKET_BYTES)
    if value is None:
        raise LocalProofError("invalid_packet")
    return value


def _send(connection: socket.socket, value: dict[str, object]) -> None:
    raw = _packet(value)
    if connection.send(raw) != len(raw):
        raise LocalProofError("short_packet")


def _root_socket(path: Path) -> socket.socket:
    """Require a protected directory and the actual connected peer to be root.

    The socket-path owner/type check gives useful diagnostics. The protected
    parent prevents a non-root path swap; SO_PEERCRED/getpeereid checks the
    actual connected peer before any challenge can be signed.
    """
    if not path.is_absolute():
        raise LocalProofError("socket_path")
    parent = path.parent
    while True:
        try:
            info = parent.lstat()
        except OSError as error:
            raise LocalProofError("socket_directory") from error
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise LocalProofError("socket_directory")
        if parent == parent.parent:
            break
        parent = parent.parent
    try:
        info = path.lstat()
    except OSError as error:
        raise LocalProofError("socket_unavailable") from error
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0:
        raise LocalProofError("socket_owner")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    connection.settimeout(PROOF_CONNECT_TIMEOUT)
    try:
        connection.connect(str(path))
        if hasattr(socket, "SO_PEERCRED"):
            raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                        struct.calcsize("=iII"))
            pid, uid, _ = struct.unpack("=iII", raw)
            if pid <= 0 or uid != 0:
                raise LocalProofError("socket_peer")
        elif hasattr(connection, "getpeereid"):
            uid, _ = connection.getpeereid()
            if uid != 0:
                raise LocalProofError("socket_peer")
        else:
            raise LocalProofError("socket_peer_unverifiable")
        return connection
    except BaseException:
        connection.close()
        raise


class LocalAppProofClient:
    """One bounded exchange; the caller owns retries and enrollment lifetime."""

    def __init__(self, path: Path = DEFAULT_PROOF_SOCKET,
                 *, connector: Callable[[Path], socket.socket] = _root_socket) -> None:
        self.path = path
        self.connector = connector

    def exchange(self, *, identity: Identity, player_id: str, authority_epoch: int,
                 device_id: str, kernel_boot_id: str,
                 enrollment_current: Callable[[], bool]) -> ProofOutcome:
        if (not enrollment_current() or not player_id.startswith("p-")
                or authority_epoch < 1):
            return "stale"
        try:
            connection = self.connector(self.path)
        except OSError as error:
            raise LocalProofError("socket_unavailable") from error
        with connection:
            connection.settimeout(PROOF_SOCKET_TIMEOUT)
            begin = AppProofBegin(kind="begin", claimed_player_id=player_id,
                                  claimed_authority_epoch=authority_epoch)
            _send(connection, begin.model_dump(mode="json", by_alias=True))
            packet = _receive(connection)
            if packet.get("kind") == "error":
                self._error_packet(packet)
                return "rejected"
            try:
                challenge = AppProofChallengePacket.model_validate_json(_packet(packet)).challenge
            except (ValidationError, ValueError, TypeError) as error:
                raise LocalProofError("challenge_invalid") from error
            if (challenge.trust_mode not in ("t1", "t2")
                    or challenge.device_id != device_id
                    or str(challenge.kernel_boot_id) != kernel_boot_id
                    or challenge.claimed_player_id != player_id
                    or challenge.claimed_authority_epoch != authority_epoch
                    or challenge.process.pid != os.getpid()):
                raise LocalProofError("challenge_context")
            if not enrollment_current():
                return "stale"
            response = identity.sign_app_proof(challenge)
            if not enrollment_current():
                return "stale"
            response_packet = AppProofResponsePacket(kind="response", response=response)
            _send(connection, response_packet.model_dump(mode="json", by_alias=True))
            terminal = _receive(connection)
            if terminal.get("kind") == "error":
                self._error_packet(terminal)
                return "rejected"
            try:
                AppProofResultPacket.model_validate(terminal)
            except (ValidationError, ValueError, TypeError) as error:
                raise LocalProofError("result_invalid") from error
            if not enrollment_current():
                return "stale"
            return "recorded"

    @staticmethod
    def _error_packet(packet: dict[str, object]) -> None:
        try:
            AppProofErrorPacket.model_validate(packet)
        except (ValidationError, ValueError, TypeError) as error:
            raise LocalProofError("error_packet") from error


__all__ = ["DEFAULT_PROOF_SOCKET", "LocalAppProofClient", "LocalProofError", "ProofOutcome"]
