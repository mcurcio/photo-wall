"""The Player links its process to an applied control receipt over the node's root-owned
app-link socket, apart from Central liveness and rendering."""

import asyncio
import json
import os
import socket
import struct
import threading
import time
from queue import Queue

import httpx
import pytest
from test_player_service import Server, close, rig

from contracts.player_control import ControlAppliedReceipt, ControlSelection
from contracts.time import ManualClock
from player.node_app_link import NodeLinkError, _receive

BOOT_ID = "12345678-1234-1234-1234-123456789abc"
RECEIPT = ControlAppliedReceipt(authority_epoch=7, delivery_id="e" * 32,
                                delivery_sequence=5, state_digest="f" * 64,
                                ack_nonce="b" * 64)


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


@pytest.mark.parametrize("ancillary,flags", [
    ([(socket.SOL_SOCKET, 1, b"x")], 0), ([], socket.MSG_CTRUNC),
])
def test_peer_packets_refuse_ancillary_data_and_truncation(ancillary, flags):
    class Packet:
        def recvmsg(self, *_):
            return b"{}", ancillary, flags, None

    with pytest.raises(NodeLinkError, match="packet_limit"):
        _receive(Packet(), limit=1024)


def test_rejected_root_packet_closes_received_descriptor():
    read_fd, write_fd = os.pipe()
    try:
        class Packet:
            def recvmsg(self, *_):
                return b"{}", [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                  struct.pack("=i", read_fd))], 0, None

        with pytest.raises(NodeLinkError, match="packet_limit"):
            _receive(Packet(), limit=1024)
        with pytest.raises(OSError):
            os.fstat(read_fd)
    finally:
        os.close(write_fd)


def test_duplicate_packet_key_is_rejected():
    class Packet:
        def recvmsg(self, *_):
            return b'{"kind":"challenge","kind":"challenge"}', [], 0, None

    with pytest.raises(NodeLinkError, match="invalid_packet"):
        _receive(Packet(), limit=1024)


class ReceiptServer(Server):
    """Central issuing one sequenced delivery and a receipt for its applied ACK."""

    def __call__(self, request):
        if request.url.path == "/v1/player/control-acks":
            self.requests.append(request)
            body = json.loads(request.content)
            receipt = RECEIPT.model_copy(update={"authority_epoch": body["authority_epoch"],
                                                 "delivery_id": body["delivery_id"],
                                                 "delivery_sequence": 1})
            return httpx.Response(200, json={
                "accepted": True, "receipt": receipt.model_dump(mode="json", by_alias=True)})
        return super().__call__(request)

    def offer(self, **kwargs):
        state = super().offer(**kwargs)
        self.state = state.model_copy(update={"delivery_id": "1" * 32, "delivery_sequence": 1})
        return self.state


def test_link_failure_does_not_stall_control_or_mark_player_offline(tmp_path):
    class UnavailableLink:
        def __init__(self):
            self.started = threading.Event()

        def exchange_applied(self, **_kwargs):
            self.started.set()
            time.sleep(.05)
            raise NodeLinkError("socket_unavailable")

    async def run():
        service, _ = await rig(tmp_path, server=ReceiptServer(ManualClock(100)), negotiate=True)
        link = UnavailableLink()
        service.node_link_client = link
        service.boot_id = BOOT_ID
        service._proof_active = True
        assert service._applied_proof_receipt is not None
        task = asyncio.create_task(service._app_link_loop())
        try:
            for _ in range(100):
                if link.started.is_set():
                    break
                await asyncio.sleep(.01)
            assert link.started.is_set()
            await asyncio.sleep(.1)
            assert service.last_fault is None
            assert not task.done()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await close(service)

    asyncio.run(run())


def test_service_exit_invalidates_in_flight_socket_worker(tmp_path):
    class WaitingLink:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.current_at_release = None

        def exchange_applied(self, **kwargs):
            self.started.set()
            self.release.wait(timeout=2)
            self.current_at_release = kwargs["enrollment_current"]()
            return "stale"

    async def run():
        service, _ = await rig(tmp_path, server=ReceiptServer(ManualClock(100)), negotiate=True)
        link = WaitingLink()
        service.node_link_client = link
        service.boot_id = BOOT_ID
        task = asyncio.create_task(service.run())
        try:
            for _ in range(1000):
                if link.started.is_set():
                    break
                await asyncio.sleep(.001)
            assert link.started.is_set()
            task.cancel()
            await asyncio.wait_for(task, 3)
            assert not service._proof_active
            link.release.set()
            for _ in range(1000):
                if link.current_at_release is not None:
                    break
                await asyncio.sleep(.001)
            assert link.current_at_release is False
        finally:
            link.release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await close(service)

    asyncio.run(run())


@pytest.mark.parametrize("invalidate", ["new_delivery", "rejected"])
def test_link_worker_cannot_record_after_control_invalidation(
        tmp_path, monkeypatch, invalidate):
    class WaitingLink:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.current_at_release = None

        def exchange_applied(self, **kwargs):
            self.started.set()
            self.release.wait(timeout=2)
            self.current_at_release = kwargs["enrollment_current"]()
            return "recorded" if self.current_at_release else "stale"

    async def run():
        service, server = await rig(tmp_path)
        link = WaitingLink()
        service.node_link_client = link
        service.boot_id = BOOT_ID
        service._proof_active = True
        registration = service.registration
        service._control_selection = ControlSelection(
            authority_epoch=registration.authority_epoch, schema=2)
        state = server.state.model_copy(update={"delivery_id": "e" * 32,
                                        "delivery_sequence": 5})
        service._highest_control_sequence = 5
        service._highest_control_delivery_id = "e" * 32
        receipt = RECEIPT.model_copy(update={"authority_epoch": registration.authority_epoch})

        async def ack_request(_method, _path, *, body):
            return ({"accepted": True,
                     "receipt": receipt.model_dump(mode="json", by_alias=True)}
                    if body["result"] == "applied" else {"accepted": True})

        service.request = ack_request
        await service._ack_control(state, "applied")
        assert service._applied_proof_receipt == (service._session, receipt)
        monkeypatch.setattr("player.service.APP_LINK_RETRY", .01)
        task = asyncio.create_task(service._app_link_loop())
        try:
            for _ in range(1000):
                if link.started.is_set():
                    break
                await asyncio.sleep(.001)
            assert link.started.is_set()
            if invalidate == "new_delivery":
                newer = state.model_copy(update={"delivery_id": "d" * 32,
                                                 "delivery_sequence": 6})
                assert service._apply_state(newer) == "applied"
            else:
                await service._ack_control(state, "rejected")
            assert service._applied_proof_receipt is None
            link.release.set()
            for _ in range(1000):
                if link.current_at_release is not None:
                    break
                await asyncio.sleep(.001)
            assert link.current_at_release is False
        finally:
            link.release.set()
            service._proof_active = False
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await close(service)

    asyncio.run(run())
