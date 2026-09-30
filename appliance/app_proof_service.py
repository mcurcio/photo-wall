"""Root-owned, app-local key proof exchange over AF_UNIX SOCK_SEQPACKET.

Composition supplies a protected current T1/T2 attempt and an atomic
record-if-current sink. This service neither owns Central credentials nor
authorizes an app update or acceptance. The listener is supplied by a root
composition root (normally a systemd socket at /run/photo-wall/app-proof.sock).
"""

from __future__ import annotations

import os
import socket
import stat
import struct
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import BoundedSemaphore
from typing import Protocol

from appliance.app_process_proof import (
    CurrentAttemptContext,
    LocalAppProofVerifier,
    LocalProofError,
    PeerCredentials,
    linux_boottime,
)
from appliance.linux_app_proof import LinuxAppProofSamplers
from contracts.app_process_proof import (
    MAX_PROOF_PACKET_BYTES,
    AppProofChallengePacket,
    AppProofErrorPacket,
    AppProofResultPacket,
    LocalAppProof,
    parse_app_proof_begin,
    parse_app_proof_response_packet,
)

_UCRED_FORMAT = "=iII"
_UCRED_BYTES = struct.calcsize(_UCRED_FORMAT)
_SO_PASSCRED = getattr(socket, "SO_PASSCRED", 16)
_SCM_CREDENTIALS = getattr(socket, "SCM_CREDENTIALS", 2)
_RECV_FLAGS = socket.MSG_TRUNC | socket.MSG_CTRUNC
_INT_BYTES = struct.calcsize("=i")
_MAX_CONNECTIONS = 4
_FIRST_PACKET_SECONDS = 2.0
_MAIN_PROCESS_CACHE_SECONDS = 0.5
_ERROR_CODES = frozenset({
    "invalid_packet", "invalid_peer", "app_process_unavailable", "app_process_mismatch",
    "attempt_context_unavailable", "attempt_context_untrusted", "app_proof_nonce_unavailable",
    "app_proof_context_invalid", "app_proof_context_changed", "app_proof_busy",
    "app_proof_response_invalid", "app_proof_expired_or_used", "app_proof_peer_changed",
    "app_proof_signature_invalid", "record_rejected", "service_unavailable",
})


class LocalAppProofSink(Protocol):
    """Atomically compare the whole protected context before storing proof.

    False means the context was revoked or replaced. A successful return
    stores only volatile OS-local evidence; it grants no Central acceptance.
    """

    def record_if_current(self, proof: LocalAppProof,
                          expected: CurrentAttemptContext) -> bool: ...


class LocalAppProofService:
    """Serve one challenge/response per connection with message credentials."""

    def __init__(self, *, expected_app_uid: int,
                 current_attempt: Callable[[], CurrentAttemptContext],
                 sink: LocalAppProofSink,
                 samplers: LinuxAppProofSamplers | None = None,
                 boottime: Callable[[], float] = linux_boottime) -> None:
        if type(expected_app_uid) is not int or expected_app_uid < 1:
            raise ValueError("app_uid_invalid")
        if not callable(current_attempt) or not callable(boottime):
            raise ValueError("app_proof_dependencies_required")
        if not callable(getattr(sink, "record_if_current", None)):
            raise ValueError("app_proof_sink_required")
        self.expected_app_uid = expected_app_uid
        self.current_attempt = current_attempt
        self.sink = sink
        self.samplers = samplers if samplers is not None else LinuxAppProofSamplers()
        self.boottime = boottime

    def _recv(self, connection: socket.socket) -> tuple[PeerCredentials, bytes]:
        """Return credentials only for one complete, exclusive data packet."""
        raw, ancillary, flags, _ = connection.recvmsg(
            MAX_PROOF_PACKET_BYTES + 1, socket.CMSG_SPACE(_UCRED_BYTES),
            getattr(socket, "MSG_CMSG_CLOEXEC", 0))
        # recvmsg installs SCM_RIGHTS descriptors before returning, even when
        # the packet is invalid or its control data was truncated. Close every
        # descriptor that fit in our control buffer before rejecting it.
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                for offset in range(0, len(data) - len(data) % _INT_BYTES, _INT_BYTES):
                    descriptor = struct.unpack_from("=i", data, offset)[0]
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
        if not raw or len(raw) > MAX_PROOF_PACKET_BYTES or flags & _RECV_FLAGS:
            raise LocalProofError("invalid_packet")
        if (len(ancillary) != 1 or ancillary[0][0] != socket.SOL_SOCKET
                or ancillary[0][1] != _SCM_CREDENTIALS
                or len(ancillary[0][2]) != _UCRED_BYTES):
            raise LocalProofError("invalid_peer")
        pid, uid, gid = struct.unpack(_UCRED_FORMAT, ancillary[0][2])
        if pid <= 0 or uid < 0 or gid < 0:
            raise LocalProofError("invalid_peer")
        peer = PeerCredentials(pid, uid, gid)
        if self.samplers.peer_credentials(connection) != peer:
            raise LocalProofError("invalid_peer")
        return peer, raw

    @staticmethod
    def _send(connection: socket.socket, payload: bytes) -> None:
        if len(payload) > MAX_PROOF_PACKET_BYTES or connection.send(payload) != len(payload):
            raise LocalProofError("service_unavailable")

    def handle_connection(self, connection: socket.socket) -> None:
        """Close the exchange after one terminal result or bounded error."""
        nonce: str | None = None
        verifier: LocalAppProofVerifier | None = None
        try:
            if (connection.family != socket.AF_UNIX
                    or connection.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
                    != socket.SOCK_SEQPACKET):
                raise LocalProofError("invalid_peer")
            connection.setsockopt(socket.SOL_SOCKET, _SO_PASSCRED, 1)
            connection.settimeout(_FIRST_PACKET_SECONDS)
            message_peer, raw = self._recv(connection)

            def peer_sampler(handle: object) -> PeerCredentials:
                if handle is not connection:
                    raise ValueError("app_proof_peer_changed")
                sampled = self.samplers.peer_credentials(connection)
                if sampled != message_peer:
                    raise ValueError("app_proof_message_peer_changed")
                return sampled

            verifier = LocalAppProofVerifier(
                expected_app_uid=self.expected_app_uid,
                peer_sampler=peer_sampler,
                main_process_sampler=self.samplers.main_process,
                peer_process_sampler=self.samplers.peer_process,
                current_attempt=self.current_attempt, boottime=self.boottime)
            begin = parse_app_proof_begin(raw)
            challenge = verifier.begin(connection, begin.claimed_player_id,
                                       begin.claimed_authority_epoch)
            nonce = challenge.nonce
            packet = AppProofChallengePacket(kind="challenge", challenge=challenge)
            self._send(connection, packet.model_dump_json(by_alias=True).encode("utf-8"))
            connection.settimeout(LocalAppProofVerifier.TTL_SECONDS)
            message_peer, raw = self._recv(connection)
            response = parse_app_proof_response_packet(raw)
            proof = verifier.verify(connection, response)
            expected = CurrentAttemptContext.from_challenge(proof.challenge)
            verifier.ensure_current_context(expected)
            try:
                recorded = self.sink.record_if_current(proof, expected)
            except Exception as exc:
                raise LocalProofError("service_unavailable") from exc
            if type(recorded) is not bool or not recorded:
                raise LocalProofError("record_rejected")
            result = AppProofResultPacket(kind="result", status="recorded")
            self._send(connection, result.model_dump_json(by_alias=True).encode("utf-8"))
        except (LocalProofError, OSError, ValueError) as exc:
            code = str(exc) if str(exc) in _ERROR_CODES else "invalid_packet"
            try:
                packet = AppProofErrorPacket(kind="error", code=code)
                self._send(connection, packet.model_dump_json(by_alias=True).encode("utf-8"))
            except (OSError, LocalProofError):
                pass
        finally:
            if nonce is not None and verifier is not None:
                verifier.cancel(nonce)
            connection.close()

    def serve_forever(self, listener: socket.socket,
                      stopping: Callable[[], bool]) -> None:
        """Serve a prebound root-owned socket; socket activation owns its path."""
        if sys.platform != "linux" or os.geteuid() != 0:
            raise RuntimeError("app_proof_root_linux_required")
        if (listener.family != socket.AF_UNIX
                or listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
                != socket.SOCK_SEQPACKET):
            raise ValueError("app_proof_unix_seqpacket_required")
        path = listener.getsockname()
        if type(path) is not str or not path.startswith("/"):
            raise ValueError("app_proof_named_socket_required")
        socket_stat = os.stat(path, follow_symlinks=False)
        parent_stat = Path(path).parent.stat()
        if (not stat.S_ISSOCK(socket_stat.st_mode) or socket_stat.st_uid != 0
                or socket_stat.st_mode & 0o007
                or parent_stat.st_uid != 0 or parent_stat.st_mode & 0o022):
            raise ValueError("app_proof_socket_ownership_invalid")
        self._serve_connections(listener, stopping)

    def _serve_connections(self, listener: socket.socket,
                           stopping: Callable[[], bool]) -> None:
        """Admit current MainPID peers without serial reads or a socket queue."""
        listener.setsockopt(socket.SOL_SOCKET, _SO_PASSCRED, 1)
        listener.settimeout(0.5)
        slots = BoundedSemaphore(_MAX_CONNECTIONS)
        cached_main = None
        sampled_at = float("-inf")

        def handle_admitted(connection: socket.socket) -> None:
            try:
                self.handle_connection(connection)
            finally:
                slots.release()

        # Admission has no executor queue: rejected connections are closed
        # immediately, and only the current MainPID can occupy a worker.
        # The cached PID is only a cheap admission hint; the verifier reads
        # current PID1 and /proc facts again before storing any proof.
        with ThreadPoolExecutor(max_workers=_MAX_CONNECTIONS) as workers:
            while not stopping():
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                try:
                    peer = self.samplers.peer_credentials(connection)
                    if peer.uid != self.expected_app_uid:
                        continue
                    now = time.monotonic()
                    if now - sampled_at >= _MAIN_PROCESS_CACHE_SECONDS:
                        cached_main = self.samplers.main_process()
                        sampled_at = now
                    if cached_main is None or peer.pid != cached_main.pid:
                        continue
                    if not slots.acquire(blocking=False):
                        continue
                    try:
                        workers.submit(handle_admitted, connection)
                    except RuntimeError:
                        slots.release()
                        raise
                    connection = None
                except (OSError, TypeError, ValueError):
                    pass
                finally:
                    if connection is not None:
                        connection.close()
