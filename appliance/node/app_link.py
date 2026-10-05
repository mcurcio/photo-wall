"""Base broker proof socket: kernel peer + exact running root + signed app receipt.

The broker accepts a proof locally (no Central round trip) and holds the signed link in a
one-slot outbox, the latest per app run; `deliver_app_link` delivers it to Central from
the main loop until Central acknowledges, and asks the Player to relink when Central can
never accept it.
"""
from __future__ import annotations

import hashlib
import http.client
import logging
import os
import secrets
import socket
import stat
from pathlib import Path
from typing import Protocol

from appliance.central_session.session import NodeSession
from appliance.clock import boottime_ms
from appliance.feed import Feed
from appliance.node.lifecycle_storage import primitive
from appliance.node.online_broker import refused_permanently
from appliance.node.probe import AppRunKey
from appliance.unix_credentials import receive_credential_packet
from contracts.node_app_link import (
    MAX_NODE_LINK_BYTES,
    NodeAppLinkChallengeV2,
    encode_node_app_link,
    encode_node_app_link_challenge,
    encode_node_app_link_result,
    parse_node_app_link,
    parse_node_app_link_begin,
    parse_node_probe_open,
)
from contracts.node_commands import encode_session_grant, parse_session_grant
from contracts.strict_json import loads_object

# One slot: the latest accepted app link, `{"run", "player_id", "link"}`; `{}` when empty.
OUTBOX = "app-link-outbox"
LOG = logging.getLogger(__name__)


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


class ProbeChannels(Protocol):
    def adopt(self, connection: socket.socket, run: AppRunKey) -> None: ...


class Relinks(Protocol):
    feed: Feed

    def send_relink(self, run: AppRunKey) -> None: ...


class BrokerLinkService:
    def __init__(self, driver, session: NodeSession, path: Path, *, probes: ProbeChannels | None = None,
                 feed: Feed | None = None):
        self.driver, self.session, self.path, self.probes = driver, session, path, probes
        self.feed = feed
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
        handed_over = False
        try:
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            connection.settimeout(2)
            try:
                # The Player speaks first; the first packet's kind selects the path.
                first = receive_credential_packet(connection, maximum=MAX_NODE_LINK_BYTES)
                if _kind(first[1]) == "probe_open":
                    self.open_probe(connection, first)
                    handed_over = True
                else:
                    self.handle(connection, first=first)
            except (OSError, ValueError, TypeError):
                # Refusal does not expose configuration, bearer or exception details.
                try:
                    connection.send(b'{"schema":2,"kind":"result","status":"refused"}')
                except OSError:
                    pass
        finally:
            if not handed_over:
                connection.close()

    def open_probe(self, connection: socket.socket, first: tuple[tuple[int, int, int], bytes]) -> None:
        """Admit a progress-probe channel: the app uid and the running app's own pid; no grant.

        The channel carries no authority (probes and answers only), so it needs no Central
        session; an admitted `probe_open` gets no reply packet.
        """
        (pid, uid, _), raw = first
        if uid != 10004 or self.probes is None:
            raise ValueError("node_link_peer")
        parse_node_probe_open(raw)
        running = self.driver.current()
        if running is None or running.process.pid != pid:
            raise ValueError("node_link_peer")
        self.probes.adopt(connection, AppRunKey.of(running))

    def remember_grant(self):
        grant = self.session.grant
        if grant is not None:
            document = {"grant": encode_session_grant(grant).decode()}
            if self.session.store.read("local-proof-grant") != document:
                self.session.store.write("local-proof-grant", document)

    def handle(self, connection: socket.socket, *, first=None) -> None:
        credentials, raw = first or receive_credential_packet(connection, maximum=MAX_NODE_LINK_BYTES)
        pid, uid, _ = credentials
        if uid != 10004:
            raise ValueError("node_link_peer")
        running = self.driver.current()
        # A retained challenge identity can prove local control while offline.
        # Central independently decides whether its carrier is still authorized.
        grant = self.session.grant
        if grant is not None:
            self.remember_grant()
        else:
            retained = self.session.store.read("local-proof-grant")
            grant = parse_session_grant(retained["grant"].encode()) if retained else None
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
                or boottime_ms() > challenge.sampled_boottime_ms + 2000):
            raise ValueError("node_link_changed_or_expired")
        # Kernel-authenticated exact-process challenge/response is local control
        # evidence, not Central signature/receipt acceptance or visible output.
        self.session.store.write("local-app-control", {"operation_id": str(running.operation_id),
            "progress": {"kind": "controlled", "process": primitive(running.process),
                         "app_epoch": running.app_epoch, "environment": running.environment.environment_sha256,
                         "sampled_ms": challenge.sampled_boottime_ms,
                         "challenge_sha256": hashlib.sha256(encoded).hexdigest()}})
        # Accepted here, with no Central round trip: the latest proof per app run waits in the
        # outbox until Central (which verifies the signature and the exact current
        # ControlApplied receipt) acknowledges it; see deliver_app_link.
        run = AppRunKey.of(running).document()
        self.session.store.write(OUTBOX, {"run": run, "player_id": begin["player_id"],
                                          "link": encode_node_app_link(link).decode()})
        if self.feed is not None:
            self.feed.append("app_link_accepted", {"run": run, "player_id": begin["player_id"]})
        connection.send(encode_node_app_link_result("accepted"))


def deliver_app_link(store, session: NodeSession, probes: Relinks, *, current: AppRunKey | None) -> None:
    """Deliver the held app link to Central; called once per main-loop turn with a grant.

    200 clears the slot. A refusal Central repeats for this link (a permanent 4xx, or a link
    proved under a session other than the current one, which Central refuses as a scope
    mismatch, central/fleet/node_app_links.py:47-49) clears it and asks that run's Player
    to relink. Anything else (transport error, 408, 429, 5xx, 401/403) keeps it for the
    next turn: the slot is never dropped on a transient failure. A slot whose run is no
    longer current is cleared.
    """
    slot = store.read(OUTBOX)
    if not slot:
        return
    run = slot["run"]
    if current is None or run != current.document():
        store.write(OUTBOX, {})
        return
    grant = session.grant
    if grant is None:
        return
    raw = slot["link"].encode()
    challenge = parse_node_app_link(raw).challenge
    if challenge.producer != grant.producer or challenge.command_session_id != grant.session_id:
        _refused(store, probes, current, status=None, reason="session_changed")
        return
    try:
        status, _ = session.request("POST", "/v2/node/app-links", raw)
    except (OSError, http.client.HTTPException):
        return  # Central unreachable: held for the next turn
    if status == 200:
        store.write(OUTBOX, {})
        probes.feed.append("app_link_recorded", {"run": run})
    elif refused_permanently(status):
        _refused(store, probes, current, status=status, reason="central_refused")


def _refused(store, probes: Relinks, run: AppRunKey, *, status: int | None, reason: str) -> None:
    store.write(OUTBOX, {})
    probes.feed.append("app_link_refused", {"run": run.document(), "status": status, "reason": reason})
    LOG.warning("broker: app link not recordable (%s, %s); asking the Player to relink", reason, status)
    probes.send_relink(run)


def _kind(raw: bytes) -> object:
    value = loads_object(raw, max_bytes=MAX_NODE_LINK_BYTES)
    return value.get("kind") if value is not None else None
