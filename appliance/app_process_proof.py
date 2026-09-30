"""Local Player-key proof under a base-owned peer and process sampler.

This is a socket-independent primitive. Only a future root-owned service may
compose it with SO_PEERCRED, PID1, /proc and the protected command inbox. No OS
credential or command effect passes to the Player through this protocol.
"""

from __future__ import annotations

import base64
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from contracts.app_process_proof import (
    AppProofChallenge,
    AppProofResponse,
    LocalAppProof,
    ProcessIdentity,
    app_proof_message,
)


class LocalProofError(ValueError):
    """The local proof failed without creating trusted evidence."""


@dataclass(frozen=True, slots=True)
class PeerCredentials:
    pid: int
    uid: int
    gid: int


@dataclass(frozen=True, slots=True)
class CurrentAttemptContext:
    """References from the root-owned session/inbox, not from the app caller."""

    installation_audience: str
    device_id: str
    device_generation: int
    kernel_boot_id: UUID
    offer_id: UUID
    attempt_id: UUID
    command_id: UUID


class PeerSampler(Protocol):
    def __call__(self, peer_handle: object) -> PeerCredentials: ...


class MainProcessSampler(Protocol):
    def __call__(self) -> ProcessIdentity | None: ...


class PeerProcessSampler(Protocol):
    def __call__(self, pid: int) -> ProcessIdentity | None: ...


@dataclass(frozen=True, slots=True)
class _Pending:
    challenge: AppProofChallenge
    peer_handle: object
    peer: PeerCredentials
    context: CurrentAttemptContext
    expires_at: float


class LocalAppProofVerifier:
    """One-use challenge verifier; OS identity comes from injected root readers."""

    TTL_SECONDS = 15.0
    MAX_PENDING = 16

    def __init__(self, *, expected_app_uid: int, peer_sampler: PeerSampler,
                 main_process_sampler: MainProcessSampler,
                 peer_process_sampler: PeerProcessSampler,
                 current_attempt: Callable[[], CurrentAttemptContext],
                 boottime: Callable[[], float] = time.monotonic,
                 nonce_bytes: Callable[[int], bytes] = secrets.token_bytes) -> None:
        if type(expected_app_uid) is not int or expected_app_uid < 1:
            raise ValueError("app_uid_invalid")
        self.expected_app_uid = expected_app_uid
        self.peer_sampler = peer_sampler
        self.main_process_sampler = main_process_sampler
        self.peer_process_sampler = peer_process_sampler
        self.current_attempt = current_attempt
        self.boottime = boottime
        self.nonce_bytes = nonce_bytes
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()

    def _sample(self, peer_handle: object) -> tuple[PeerCredentials, ProcessIdentity]:
        try:
            peer = self.peer_sampler(peer_handle)
            if type(peer) is not PeerCredentials or type(peer.pid) is not int:
                raise TypeError("invalid peer sample")
            main = self.main_process_sampler()
            process = self.peer_process_sampler(peer.pid)
        except (OSError, ValueError, TypeError) as exc:
            raise LocalProofError("app_process_unavailable") from exc
        if (type(peer.uid) is not int or peer.uid != self.expected_app_uid
                or type(peer.gid) is not int or peer.pid <= 0
                or type(main) is not ProcessIdentity
                or type(process) is not ProcessIdentity
                or main != process or process.pid != peer.pid
                or process.cgroup_unit != "photo-wall-player.service"):
            raise LocalProofError("app_process_mismatch")
        return peer, process

    def _context(self) -> CurrentAttemptContext:
        try:
            context = self.current_attempt()
        except (OSError, ValueError) as exc:
            raise LocalProofError("attempt_context_unavailable") from exc
        if type(context) is not CurrentAttemptContext:
            raise LocalProofError("attempt_context_unavailable")
        return context

    def begin(self, peer_handle: object, claimed_player_id: str,
              claimed_authority_epoch: int) -> AppProofChallenge:
        peer, process = self._sample(peer_handle)
        context = self._context()
        nonce = self.nonce_bytes(32)
        if type(nonce) is not bytes or len(nonce) != 32:
            raise LocalProofError("app_proof_nonce_unavailable")
        try:
            challenge = AppProofChallenge(
                nonce=nonce.hex(), installation_audience=context.installation_audience,
                device_id=context.device_id, device_generation=context.device_generation,
                kernel_boot_id=context.kernel_boot_id, offer_id=context.offer_id,
                attempt_id=context.attempt_id, command_id=context.command_id,
                claimed_player_id=claimed_player_id,
                claimed_authority_epoch=claimed_authority_epoch, process=process,
            )
        except ValueError as exc:
            raise LocalProofError("app_proof_context_invalid") from exc
        peer_after, process_after = self._sample(peer_handle)
        if (peer_after != peer or process_after != process
                or self._context() != context):
            raise LocalProofError("app_proof_context_changed")
        now = self.boottime()
        with self._lock:
            self._pending = {key: item for key, item in self._pending.items()
                             if item.expires_at > now}
            if len(self._pending) >= self.MAX_PENDING or challenge.nonce in self._pending:
                raise LocalProofError("app_proof_busy")
            self._pending[challenge.nonce] = _Pending(
                challenge, peer_handle, peer, context, now + self.TTL_SECONDS)
        return challenge

    def verify(self, peer_handle: object, response: AppProofResponse) -> LocalAppProof:
        if type(response) is not AppProofResponse:
            raise LocalProofError("app_proof_response_invalid")
        with self._lock:
            pending = self._pending.pop(response.nonce, None)
        if pending is None or self.boottime() >= pending.expires_at:
            raise LocalProofError("app_proof_expired_or_used")
        if peer_handle is not pending.peer_handle:
            raise LocalProofError("app_proof_peer_changed")
        peer, process = self._sample(peer_handle)
        if (peer != pending.peer or process != pending.challenge.process
                or self._context() != pending.context):
            raise LocalProofError("app_proof_context_changed")
        try:
            public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(response.public_key))
            signature = base64.b64decode(response.signature, validate=True)
            public_key.verify(signature, app_proof_message(pending.challenge))
        except (ValueError, InvalidSignature) as exc:
            raise LocalProofError("app_proof_signature_invalid") from exc
        peer_after, process_after = self._sample(peer_handle)
        if (peer_after != pending.peer or process_after != pending.challenge.process
                or self._context() != pending.context):
            raise LocalProofError("app_proof_context_changed")
        return LocalAppProof(challenge=pending.challenge, response=response,
                             verified_boottime_ms=int(self.boottime() * 1000))
