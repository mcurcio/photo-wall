"""The broker's probe socket: the listener half of the retired app link (decision 0034, S4c).

The Player connects to the broker's root-owned SOCK_SEQPACKET listener and speaks first. Its one
admitted first packet is `probe_open` from the app uid whose pid is the current run's; the
connection is handed to the probe thread (`ProbeChannels.adopt`). Anything else is refused locally
with one `result: refused` packet and the refusal code alone in the journal. No Central, no session,
no outbox: the channel carries no authority.
"""
from __future__ import annotations

import logging
import os
import re
import socket
import stat
from pathlib import Path
from typing import Protocol

from appliance.apps.broker import RunningApp
from appliance.apps.probe import AppRunKey
from appliance.kernel.app_facts import PLAYER_UID
from appliance.kernel.unix_credentials import receive_credential_packet
from contracts.node_app_link import (
    MAX_NODE_LINK_BYTES,
    encode_node_app_link_result,
    parse_node_probe_open,
)

LOG = logging.getLogger(__name__)
SOCKET_NAME = "app-link.sock"


class CurrentRun(Protocol):
    def current(self) -> RunningApp | None: ...


class ProbeChannels(Protocol):
    def adopt(self, connection: socket.socket, run: AppRunKey) -> None: ...


def proof_directory(path: Path, *, owner_uid: int = 0) -> tuple[int, int]:
    """Only a dedicated protected directory may expose the replaceable socket."""
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid
            or stat.S_IMODE(info.st_mode) != 0o755):
        raise ValueError("node_link_directory")
    if any(child.name != SOCKET_NAME for child in path.iterdir()):
        raise ValueError("node_link_directory_contents")
    return info.st_dev, info.st_ino


def remove_proof_socket(path: Path, *, owner_uid: int = 0) -> None:
    # UMask0077 bind, then chown, then chmod: these are the only recoverable
    # initialization states. The parent has already been validated separately.
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != owner_uid
            or (info.st_gid, stat.S_IMODE(info.st_mode))
            not in ((0, 0o700), (PLAYER_UID, 0o700), (PLAYER_UID, 0o660))):
        raise ValueError("node_link_socket_ownership")
    path.unlink()


class ProbeListener:
    """The broker's root-owned SOCK_SEQPACKET listener at PROBE_SOCKET (mode 0660, group 10004).
    Its one admitted first packet is `probe_open` from uid 10004 whose pid is the current run's;
    the connection is handed to `probes.adopt`. Anything else is refused locally with
    {"schema":2,"kind":"result","status":"refused"} and the refusal code alone in the journal.
    No Central, no session, no outbox."""

    def __init__(self, runs: CurrentRun, path: Path, *, probes: ProbeChannels) -> None:
        self.runs, self.path, self.probes = runs, path, probes
        self.directory_identity = proof_directory(path.parent)
        remove_proof_socket(path)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        self.listener.bind(str(path))
        os.chown(path, 0, PLAYER_UID)
        os.chmod(path, 0o660)
        if proof_directory(path.parent) != self.directory_identity:
            self.listener.close()
            raise ValueError("node_link_directory_replaced")
        info = path.lstat()
        self.socket_identity = info.st_dev, info.st_ino
        self.listener.listen(4)
        self.listener.settimeout(0.05)

    def close(self) -> None:
        self.listener.close()
        if proof_directory(self.path.parent) != self.directory_identity:
            raise ValueError("node_link_directory_replaced")
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        if (info.st_dev, info.st_ino) == self.socket_identity:
            remove_proof_socket(self.path)

    def serve_one(self) -> None:
        """Accept at most one connection (waiting at most the listener's 50 ms) and admit or refuse it."""
        try:
            connection, _ = self.listener.accept()
        except TimeoutError:
            return
        handed_over = False
        try:
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            connection.settimeout(2)
            try:
                self._admit(connection)
                handed_over = True
            except (OSError, ValueError, TypeError) as error:
                # The peer gets no detail; the journal gets the refusal code alone.
                code = str(error)
                LOG.warning("broker: probe channel refused locally (%s)", code if re.fullmatch(
                    r"[a-z0-9_]{1,64}", code) else type(error).__name__)
                try:
                    connection.send(encode_node_app_link_result("refused"))
                except OSError:
                    pass
        finally:
            if not handed_over:
                connection.close()

    def _admit(self, connection: socket.socket) -> None:
        """A `probe_open` from the app uid and the current run's own pid; an admitted channel gets
        no reply packet."""
        (pid, uid, _), raw = receive_credential_packet(connection, maximum=MAX_NODE_LINK_BYTES)
        if uid != PLAYER_UID:
            raise ValueError("node_link_peer")
        parse_node_probe_open(raw)
        running = self.runs.current()
        if running is None or running.process.pid != pid:
            raise ValueError("node_link_peer")
        self.probes.adopt(connection, AppRunKey.of(running))


__all__ = ["CurrentRun", "ProbeChannels", "ProbeListener", "proof_directory", "remove_proof_socket"]
