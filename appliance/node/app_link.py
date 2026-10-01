"""Base broker proof socket: kernel peer + exact running root + signed app receipt."""
from __future__ import annotations

import json
import os
import secrets
import socket
import stat
from pathlib import Path

from appliance.node.clock import boottime_ms
from appliance.node.session import NodeSession
from appliance.unix_credentials import receive_credential_packet
from contracts.node_app_link import (
    MAX_NODE_LINK_BYTES,
    NodeAppLinkChallengeV2,
    encode_node_app_link,
    encode_node_app_link_challenge,
    parse_node_app_link,
    parse_node_app_link_begin,
)


def proof_directory(path: Path, *, owner_uid: int = 0) -> tuple[int, int]:
    """Only a dedicated protected directory may expose the replaceable socket."""
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid
            or stat.S_IMODE(info.st_mode) != 0o755):
        raise ValueError("node_link_directory")
    if any(child.name != "app-link.sock" for child in path.iterdir()):
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
            or (info.st_gid, stat.S_IMODE(info.st_mode)) not in ((0, 0o700), (10004, 0o700), (10004, 0o660))):
        raise ValueError("node_link_socket_ownership")
    path.unlink()


class BrokerLinkService:
    def __init__(self, driver, session: NodeSession, path: Path):
        self.driver, self.session, self.path = driver, session, path
        self.directory_identity = proof_directory(path.parent)
        remove_proof_socket(path)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        self.listener.bind(str(path))
        os.chown(path, 0, 10004)
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
        try:
            connection, _ = self.listener.accept()
        except TimeoutError:
            return
        with connection:
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            connection.settimeout(2)
            try:
                self.handle(connection)
            except (OSError, ValueError, TypeError):
                # Refusal does not expose configuration, bearer or exception details.
                try:
                    connection.send(b'{"schema":2,"kind":"result","status":"refused"}')
                except OSError:
                    pass

    def handle(self, connection: socket.socket) -> None:
        credentials, raw = receive_credential_packet(connection, maximum=MAX_NODE_LINK_BYTES)
        pid, uid, _ = credentials
        if uid != 10004:
            raise ValueError("node_link_peer")
        running = self.driver.current()
        grant = self.session.ensure()
        if uid != 10004 or running is None or running.process.pid != pid or grant is None:
            raise ValueError("node_link_peer")
        begin = parse_node_app_link_begin(raw)
        challenge = NodeAppLinkChallengeV2(grant.producer, grant.session_id, running.process,
            running.app_epoch, running.environment.environment_sha256, **begin,
            nonce=secrets.token_hex(32), sampled_boottime_ms=boottime_ms())
        encoded = encode_node_app_link_challenge(challenge)
        if connection.send(encoded) != len(encoded):
            raise ValueError("node_link_send")
        response_peer, raw = receive_credential_packet(connection, maximum=MAX_NODE_LINK_BYTES)
        link = parse_node_app_link(raw)
        if (response_peer != credentials or link.challenge != challenge
                or self.driver.current() != running
                or boottime_ms() > min(challenge.sampled_boottime_ms + 2000, grant.expires_boottime_ms)):
            raise ValueError("node_link_changed_or_expired")
        # Central verifies app signature and exact current ControlApplied receipt.
        status, _ = self.session.transport.request("POST", "/v2/node/app-links",
                                                   encode_node_app_link(link), self.session.claim)
        if status == 200:
            self.session.store.write("latest-app-link", {"link": encode_node_app_link(link).decode()})
        result = "recorded" if status == 200 else "refused"
        packet = json.dumps({"schema": 2, "kind": "result", "status": result}).encode()
        connection.send(packet)
