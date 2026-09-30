"""The app proves its enrollment key only to a root-owned local OS peer."""

import asyncio
import base64
import json
import os
import socket
import struct
import threading
import time
from queue import Queue
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from contracts.app_process_proof import AppProofChallenge, AppProofResponse, app_proof_message
from player.identity import load_identity
from player.local_app_proof import LocalAppProofClient, LocalProofError, _receive
from tests.test_player_service import close, rig

BOOT_ID = "12345678-1234-1234-1234-123456789abc"
DEVICE_ID = "device-" + "b" * 64
PLAYER_ID = "p-" + "c" * 32


def challenge(*, trust_mode="t1", device_id=DEVICE_ID, boot_id=BOOT_ID,
              player_id=PLAYER_ID, epoch=7):
    return {
        "schema": 1,
        "nonce": "a" * 64,
        "installation_audience": "photo-wall-installation-test",
        "device_id": device_id,
        "device_generation": 1,
        "kernel_boot_id": boot_id,
        "offer_id": str(UUID(int=1)),
        "command_session_id": str(UUID(int=2)),
        "attempt_id": str(UUID(int=3)),
        "command_id": str(UUID(int=4)),
        "trust_mode": trust_mode,
        "claimed_player_id": player_id,
        "claimed_authority_epoch": epoch,
        "process": {"pid": os.getpid(), "start_ticks": 1,
                    "invocation_id": "d" * 32,
                    "cgroup_unit": "photo-wall-player.service"},
    }


def _send(sock, value):
    sock.send(json.dumps(value, separators=(",", ":")).encode())


class _PacketSocket:
    """Portable SOCK_SEQPACKET behavior for macOS, which lacks that socket type."""

    def __init__(self):
        self.incoming = Queue()
        self.peer = None

    def settimeout(self, _timeout):
        pass

    def send(self, raw):
        self.peer.incoming.put(raw)
        return len(raw)

    def recv(self, size):
        return self.incoming.get(timeout=2)[:size]

    def recvmsg(self, size, _ancillary_size=0, _flags=0):
        raw = self.incoming.get(timeout=2)
        return raw[:size], [], socket.MSG_TRUNC if len(raw) > size else 0, None

    def close(self):
        self.peer.incoming.put(b"")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _packet_pair():
    try:
        return socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    except OSError:
        app, os_peer = _PacketSocket(), _PacketSocket()
        app.peer, os_peer.peer = os_peer, app
        return app, os_peer


def _exchange(server_fn, *, current=lambda: True):
    app, os_peer = _packet_pair()
    app.settimeout(1)
    os_peer.settimeout(1)
    client = LocalAppProofClient(connector=lambda _: app)
    identity = load_identity()
    errors = []

    def serve():
        try:
            with os_peer:
                server_fn(os_peer, identity)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=serve)
    thread.start()
    failure = None
    try:
        outcome = client.exchange(identity=identity, player_id=PLAYER_ID,
                                  authority_epoch=7, device_id=DEVICE_ID,
                                  kernel_boot_id=BOOT_ID, enrollment_current=current)
    except BaseException as error:
        failure = error
    finally:
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert not errors
    if failure is not None:
        raise failure
    return outcome


@pytest.mark.parametrize("trust_mode", ["t1", "t2"])
def test_player_signs_only_the_exact_root_peer_challenge(trust_mode):
    def serve(sock, identity):
        begin = json.loads(sock.recv(256))
        assert begin == {"schema": 1, "kind": "begin", "claimed_player_id": PLAYER_ID,
                         "claimed_authority_epoch": 7}
        value = challenge(trust_mode=trust_mode)
        _send(sock, {"schema": 1, "kind": "challenge", "challenge": value})
        packet = json.loads(sock.recv(1024))
        assert set(packet) == {"schema", "kind", "response"}
        assert packet["kind"] == "response"
        response = AppProofResponse.model_validate(packet["response"])
        signed = AppProofChallenge.model_validate_json(json.dumps(value))
        assert response.public_key == identity.public_key
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(response.public_key)).verify(
            base64.b64decode(response.signature), app_proof_message(signed))
        _send(sock, {"schema": 1, "kind": "result", "status": "recorded"})

    assert _exchange(serve) == "recorded"


@pytest.mark.parametrize("changed", [
    {"trust_mode": "t0"}, {"device_id": "device-" + "e" * 64},
    {"boot_id": str(UUID(int=5))}, {"player_id": "p-" + "e" * 32},
    {"epoch": 8},
])
def test_mismatched_or_t0_challenge_never_receives_a_signature(changed):
    def serve(sock, _identity):
        sock.recv(256)
        _send(sock, {"schema": 1, "kind": "challenge", "challenge": challenge(**changed)})
        assert sock.recv(1024) == b""

    with pytest.raises(LocalProofError) as failure:
        _exchange(serve)
    assert str(failure.value) in {"challenge_invalid", "challenge_context"}


def test_enrollment_change_after_challenge_aborts_before_signing():
    current = [True]

    def serve(sock, _identity):
        sock.recv(256)
        current[0] = False
        _send(sock, {"schema": 1, "kind": "challenge", "challenge": challenge()})
        assert sock.recv(1024) == b""

    assert _exchange(serve, current=lambda: current[0]) == "stale"


def test_duplicate_challenge_key_is_rejected_without_signing():
    def serve(sock, _identity):
        sock.recv(256)
        raw = json.dumps({"schema": 1, "kind": "challenge", "challenge": challenge()})
        sock.send(raw[:-1].encode() + b',"kind":"challenge"}')
        assert sock.recv(1024) == b""

    with pytest.raises(LocalProofError, match="invalid_packet"):
        _exchange(serve)


@pytest.mark.parametrize("ancillary,flags", [
    ([(socket.SOL_SOCKET, 1, b"x")], 0), ([], socket.MSG_CTRUNC),
])
def test_peer_packets_refuse_ancillary_data_and_truncation(ancillary, flags):
    class Packet:
        def recvmsg(self, *_):
            return b"{}", ancillary, flags, None

    with pytest.raises(LocalProofError, match="packet_limit"):
        _receive(Packet())


def test_rejected_root_packet_closes_received_descriptor():
    read_fd, write_fd = os.pipe()
    try:
        class Packet:
            def recvmsg(self, *_):
                return b"{}", [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                  struct.pack("=i", read_fd))], 0, None

        with pytest.raises(LocalProofError, match="packet_limit"):
            _receive(Packet())
        with pytest.raises(OSError):
            os.fstat(read_fd)
    finally:
        os.close(write_fd)


def test_local_proof_failure_does_not_stall_control_or_mark_player_offline(tmp_path):
    class UnavailableProof:
        def __init__(self):
            self.started = threading.Event()

        def exchange(self, **kwargs):
            self.started.set()
            time.sleep(.05)
            raise LocalProofError("socket_unavailable")

    async def run():
        service, _ = await rig(tmp_path)
        proof = UnavailableProof()
        service.app_proof_client = proof
        service.boot_id = BOOT_ID
        service._proof_active = True
        task = asyncio.create_task(service._local_app_proof_loop())
        try:
            for _ in range(100):
                if proof.started.is_set():
                    break
                await asyncio.sleep(.001)
            assert proof.started.is_set()
            await asyncio.sleep(.005)
            assert service.last_fault is None
            assert not task.done()
            await asyncio.sleep(.1)
            assert service.last_fault is None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await close(service)

    asyncio.run(run())


def test_recorded_proof_is_once_per_enrollment_and_reenrollment_retries(tmp_path, monkeypatch):
    class RecordingProof:
        def __init__(self):
            self.calls = 0

        def exchange(self, **kwargs):
            self.calls += 1
            return "recorded" if kwargs["enrollment_current"]() else "stale"

    async def run():
        service, _ = await rig(tmp_path)
        proof = RecordingProof()
        service.app_proof_client = proof
        service.boot_id = BOOT_ID
        service._proof_active = True
        monkeypatch.setattr("player.service.LOCAL_PROOF_RETRY", .01)
        task = asyncio.create_task(service._local_app_proof_loop())
        try:
            await asyncio.sleep(.08)
            assert proof.calls == 1
            old = service.registration
            service._session = service._session.enrolled(
                old.model_copy(update={"authority_epoch": old.authority_epoch + 1}))
            await asyncio.sleep(.08)
            assert proof.calls == 2
        finally:
            service._proof_active = False
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await close(service)

    asyncio.run(run())


def test_service_exit_invalidates_in_flight_socket_worker(tmp_path):
    class WaitingProof:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.current_at_release = None

        def exchange(self, **kwargs):
            self.started.set()
            self.release.wait(timeout=2)
            self.current_at_release = kwargs["enrollment_current"]()
            return "stale"

    async def run():
        service, _ = await rig(tmp_path)
        proof = WaitingProof()
        service.app_proof_client = proof
        service.boot_id = BOOT_ID
        task = asyncio.create_task(service.run())
        try:
            for _ in range(1000):
                if proof.started.is_set():
                    break
                await asyncio.sleep(.001)
            assert proof.started.is_set()
            task.cancel()
            await asyncio.wait_for(task, 3)
            assert not service._proof_active
            proof.release.set()
            for _ in range(1000):
                if proof.current_at_release is not None:
                    break
                await asyncio.sleep(.001)
            assert proof.current_at_release is False
        finally:
            proof.release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await close(service)

    asyncio.run(run())
