from __future__ import annotations

import importlib.util
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

MODULE = Path(__file__).parents[1] / "scripts" / "boot_time_fixture.py"
spec = importlib.util.spec_from_file_location("boot_time_fixture", MODULE)
assert spec and spec.loader
fixture_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture_module)
NTPFixture = fixture_module.NTPFixture


def request(version: int = 4, transmit: bytes = b"CLIENTTX") -> bytes:
    packet = bytearray(48)
    packet[0] = (version << 3) | 3
    packet[2] = 6
    packet[40:48] = transmit
    return bytes(packet)


def test_response_fields_and_injected_timestamps() -> None:
    clock = iter((1_700_000_000.0, 1_700_000_001.0, 1_700_000_002.0)).__next__
    response = NTPFixture(clock=clock).response(request(3))
    assert response and len(response) == 48
    assert response[0] & 7 == 4
    assert (response[0] >> 3) & 7 == 3
    assert response[1] == 2
    assert response[24:32] == b"CLIENTTX"
    assert struct.unpack("!II", response[16:24])[0] == 1_700_000_000 + fixture_module.NTP_EPOCH
    assert struct.unpack("!II", response[32:40])[0] == 1_700_000_001 + fixture_module.NTP_EPOCH


@pytest.mark.parametrize("packet", [b"", b"x" * 47, b"x" * 129, request(2), bytes([0x1C]) + b"x" * 47])
def test_invalid_packets_are_ignored(packet: bytes) -> None:
    assert NTPFixture().response(packet) is None


def test_client_unsynchronized_flag_does_not_invalidate_server_clock() -> None:
    packet = bytes([request()[0] | 0xC0]) + request()[1:]
    reply = NTPFixture(clock=lambda: 0.25).response(packet)
    assert reply is not None and reply[0] >> 6 == 0
    assert struct.unpack("!II", reply[40:48]) == (fixture_module.NTP_EPOCH, 1 << 30)
    assert struct.unpack("!II", fixture_module.ntp_timestamp(-0.25)) == (
        fixture_module.NTP_EPOCH - 1, 3 << 30)


def test_loopback_exchange_and_sigterm() -> None:
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(MODULE), "--bind", "127.0.0.1", "--port", str(port)])
    try:
        deadline = time.monotonic() + 3
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(0.2)
            while True:
                try:
                    client.sendto(request(), ("127.0.0.1", port))
                    reply, _ = client.recvfrom(128)
                    break
                except TimeoutError:
                    if time.monotonic() >= deadline:
                        raise
            client.sendto(request() + b"x" * 256, ("127.0.0.1", port))
            with pytest.raises(TimeoutError):
                client.recvfrom(128)
        assert len(reply) == 48
        assert reply[24:32] == b"CLIENTTX"
    finally:
        proc.terminate()
        proc.wait(timeout=3)
    assert proc.returncode == 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
