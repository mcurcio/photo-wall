"""The root-local exchange checks each sender and records only through CAS."""

import json
import os
import socket
import struct
import sys
import threading
import time
from dataclasses import replace
from uuid import UUID

import pytest

from appliance.app_process_proof import CurrentAttemptContext, LocalProofError, PeerCredentials
from appliance.app_proof_service import LocalAppProofService
from contracts.app_process_proof import (
    AppProofChallenge,
    AppProofResponsePacket,
    ProcessIdentity,
)
from player.identity import load_identity

PLAYER = "p-" + "a" * 32
DEVICE = "device-" + "b" * 64
PEER = PeerCredentials(123, 10001, 10001)
PROCESS = ProcessIdentity(pid=123, start_ticks=456, invocation_id="c" * 32,
                          cgroup_unit="photo-wall-player.service")
CONTEXT = CurrentAttemptContext(
    "installation-one", DEVICE, 2, UUID(int=1), UUID(int=2), UUID(int=3),
    UUID(int=4), UUID(int=5), "t2",
)
CREDS = [(socket.SOL_SOCKET, getattr(socket, "SCM_CREDENTIALS", 2),
          struct.pack("=iII", PEER.pid, PEER.uid, PEER.gid))]


class FakeConnection:
    family = socket.AF_UNIX

    def __init__(self, *, begin_creds=CREDS, begin_flags=0, begin_raw=None,
                 second_creds=CREDS, second_flags=0, second_raw=None):
        self.begin_creds = begin_creds
        self.begin_flags = begin_flags
        self.begin_raw = begin_raw
        self.second_creds = second_creds
        self.second_flags = second_flags
        self.second_raw = second_raw
        self.sent = []
        self.receives = 0
        self.closed = False

    def getsockopt(self, _level, _option):
        return socket.SOCK_SEQPACKET

    def setsockopt(self, *_):
        pass

    def settimeout(self, _):
        pass

    def recvmsg(self, *_):
        self.receives += 1
        if self.receives == 1:
            raw = self.begin_raw if self.begin_raw is not None else json.dumps({
                "schema": 1, "kind": "begin", "claimed_player_id": PLAYER,
                "claimed_authority_epoch": 7}).encode()
            return raw, self.begin_creds, self.begin_flags, None
        assert self.receives == 2
        if self.second_raw is None:
            challenge = AppProofChallenge.model_validate_json(
                json.dumps(json.loads(self.sent[0])["challenge"]).encode())
            response = load_identity().sign_app_proof(challenge)
            raw = AppProofResponsePacket(kind="response", response=response).model_dump_json(
                by_alias=True).encode()
        else:
            raw = self.second_raw
        return raw, self.second_creds, self.second_flags, None

    def send(self, data):
        self.sent.append(data)
        return len(data)

    def close(self):
        self.closed = True


class FakeSamplers:
    def __init__(self):
        self.peer = PEER
        self.main = PROCESS
        self.process = PROCESS

    def peer_credentials(self, _connection):
        return self.peer

    def main_process(self):
        return self.main

    def peer_process(self, _pid):
        return self.process


class CasSink:
    def __init__(self, state):
        self.state = state
        self.recorded = []

    def record_if_current(self, proof, expected):
        if self.state["context"] != expected:
            return False
        self.recorded.append(proof)
        return True


def service_state():
    state = {"context": CONTEXT, "now": 10.0}
    samplers = FakeSamplers()
    sink = CasSink(state)
    service = LocalAppProofService(
        expected_app_uid=PEER.uid, current_attempt=lambda: state["context"],
        sink=sink, samplers=samplers, boottime=lambda: state["now"])
    return service, state, samplers, sink


def terminal(connection):
    return json.loads(connection.sent[-1])


def test_one_exchange_records_bound_proof_through_injected_cas_sink():
    service, _, _, sink = service_state()
    connection = FakeConnection()
    service.handle_connection(connection)
    assert connection.closed
    assert [json.loads(packet)["kind"] for packet in connection.sent] == ["challenge", "result"]
    assert terminal(connection) == {"schema": 1, "kind": "result", "status": "recorded"}
    proof = sink.recorded[0]
    assert proof.challenge.command_session_id == CONTEXT.command_session_id
    assert proof.challenge.trust_mode == "t2"
    assert proof.challenge.process == PROCESS


@pytest.mark.parametrize("second_creds,second_flags,second_raw", [
    ([(socket.SOL_SOCKET, getattr(socket, "SCM_CREDENTIALS", 2),
       struct.pack("=iII", 124, PEER.uid, PEER.gid))], 0, None),
    (CREDS + [(socket.SOL_SOCKET, socket.SCM_RIGHTS, b"fd")], 0, None),
    (CREDS, socket.MSG_CTRUNC, None),
    (CREDS, socket.MSG_TRUNC, None),
    (CREDS, 0, b'not-json'),
])
def test_response_packet_must_be_complete_and_from_original_process(
        second_creds, second_flags, second_raw):
    service, _, _, sink = service_state()
    connection = FakeConnection(second_creds=second_creds, second_flags=second_flags,
                                second_raw=second_raw)
    service.handle_connection(connection)
    assert connection.closed and not sink.recorded
    assert terminal(connection)["kind"] == "error"


@pytest.mark.parametrize("connection", [
    FakeConnection(begin_creds=[]),
    FakeConnection(begin_creds=CREDS + [(socket.SOL_SOCKET, socket.SCM_RIGHTS, b"fd")]),
    FakeConnection(begin_flags=socket.MSG_TRUNC),
    FakeConnection(begin_raw=b"{" + b"x" * 2305),
    FakeConnection(begin_raw=b'{"schema":1,"schema":1,"kind":"begin"}'),
    FakeConnection(begin_raw=json.dumps({"kind": "begin", "claimed_player_id": PLAYER,
                                         "claimed_authority_epoch": 7}).encode()),
])
def test_begin_requires_one_bounded_packet_and_credentials(connection):
    service, _, _, sink = service_state()
    service.handle_connection(connection)
    assert connection.closed and not sink.recorded
    assert [json.loads(packet)["kind"] for packet in connection.sent] == ["error"]


def test_rejected_rights_packet_closes_received_descriptor():
    read_fd, write_fd = os.pipe()
    try:
        service, _, _, _ = service_state()
        connection = FakeConnection(begin_creds=CREDS + [
            (socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("=i", read_fd))])
        service.handle_connection(connection)
        with pytest.raises(OSError):
            os.fstat(read_fd)
        assert terminal(connection)["kind"] == "error"
    finally:
        os.close(write_fd)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux SCM_RIGHTS delivery required")
def test_kernel_rights_packet_does_not_leak_a_received_descriptor():
    service, _, _, _ = service_state()
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    read_fd, write_fd = os.pipe()
    try:
        client.sendmsg([b"{}"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                  struct.pack("=i", read_fd))])
        before = len(os.listdir("/proc/self/fd"))
        with pytest.raises(LocalProofError, match="invalid_peer"):
            service._recv(server)
        assert len(os.listdir("/proc/self/fd")) == before
    finally:
        server.close()
        client.close()
        os.close(read_fd)
        os.close(write_fd)


def test_idle_connection_cannot_serially_block_current_player_proof():
    service, _, _, sink = service_state()
    recorded_at = []
    original_record = sink.record_if_current

    def record_timed(proof, expected):
        accepted = original_record(proof, expected)
        if accepted:
            recorded_at.append(time.monotonic())
        return accepted

    sink.record_if_current = record_timed

    class IdleConnection(FakeConnection):
        def recvmsg(self, *args):
            time.sleep(1.5)
            return super().recvmsg(*args)

    class Listener:
        def __init__(self):
            self.connections = [IdleConnection(begin_raw=b""), FakeConnection()]

        def setsockopt(self, *_args):
            pass

        def settimeout(self, _seconds):
            pass

        def accept(self):
            if self.connections:
                return self.connections.pop(0), None
            time.sleep(0.005)
            raise TimeoutError

    listener = Listener()
    started = time.monotonic()
    service._serve_connections(listener, lambda: bool(sink.recorded))
    assert recorded_at[0] - started < 1.0
    assert len(sink.recorded) == 1


def test_context_revoke_and_t0_fail_without_recording():
    service, state, _, sink = service_state()
    state["context"] = replace(CONTEXT, trust_mode="t0")
    connection = FakeConnection()
    service.handle_connection(connection)
    assert connection.closed and not sink.recorded
    assert terminal(connection)["code"] == "attempt_context_untrusted"


def test_sink_cas_closes_context_switch_after_signature():
    service, state, _, sink = service_state()

    def revoke_before_store(proof, expected):
        state["context"] = replace(CONTEXT, command_session_id=UUID(int=99))
        return CasSink.record_if_current(sink, proof, expected)

    sink.record_if_current = revoke_before_store
    connection = FakeConnection()
    service.handle_connection(connection)
    assert not sink.recorded
    assert terminal(connection)["code"] == "record_rejected"


def test_context_switch_after_verification_cannot_be_adopted_as_sink_expected():
    service, state, _, sink = service_state()
    calls = 0

    def switch_after_verify():
        nonlocal calls
        calls += 1
        if calls == 5:
            state["context"] = replace(CONTEXT, command_session_id=UUID(int=99))
        return state["context"]

    service.current_attempt = switch_after_verify
    connection = FakeConnection()
    service.handle_connection(connection)
    assert not sink.recorded
    assert terminal(connection)["code"] == "app_proof_context_changed"


def test_sink_failure_is_bounded_and_not_recorded():
    service, _, _, sink = service_state()

    def unavailable(_proof, _expected):
        raise RuntimeError("private backend details")

    sink.record_if_current = unavailable
    connection = FakeConnection()
    service.handle_connection(connection)
    assert not sink.recorded
    assert terminal(connection)["code"] == "service_unavailable"
    assert b"private backend details" not in connection.sent[-1]


def test_expired_challenge_cannot_be_stored():
    service, state, _, sink = service_state()
    connection = FakeConnection()
    original_send = connection.send

    def send_then_advance(data):
        sent = original_send(data)
        if json.loads(data)["kind"] == "challenge":
            state["now"] = 25.0
        return sent

    connection.send = send_then_advance
    service.handle_connection(connection)
    assert not sink.recorded
    assert terminal(connection)["code"] == "app_proof_expired_or_used"


@pytest.mark.skipif(sys.platform != "linux" or os.getuid() == 0,
                    reason="Linux unprivileged SOCK_SEQPACKET/SCM_CREDENTIALS required")
def test_linux_kernel_attaches_credentials_to_both_packets():
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    state = {"context": CONTEXT}
    sink = CasSink(state)
    peer = PeerCredentials(os.getpid(), os.getuid(), os.getgid())
    process = PROCESS.model_copy(update={"pid": os.getpid()})
    samplers = FakeSamplers()
    samplers.peer = peer
    samplers.main = process
    samplers.process = process
    service = LocalAppProofService(
        expected_app_uid=os.getuid(), current_attempt=lambda: state["context"],
        sink=sink, samplers=samplers, boottime=lambda: 10.0)
    thread = threading.Thread(target=service.handle_connection, args=(server,))
    thread.start()
    try:
        client.settimeout(3)
        client.send(json.dumps({"schema": 1, "kind": "begin", "claimed_player_id": PLAYER,
                                "claimed_authority_epoch": 7}).encode())
        challenge_packet = json.loads(client.recv(2305))
        assert challenge_packet["kind"] == "challenge"
        challenge = AppProofChallenge.model_validate_json(
            json.dumps(challenge_packet["challenge"]).encode())
        response = load_identity().sign_app_proof(challenge)
        packet = AppProofResponsePacket(kind="response", response=response)
        client.send(packet.model_dump_json(by_alias=True).encode())
        assert json.loads(client.recv(2305))["status"] == "recorded"
    finally:
        client.close()
        thread.join(timeout=3)
    assert not thread.is_alive() and len(sink.recorded) == 1
