#!/usr/bin/env python3
"""Small isolated NTP responder for generic boot tests.

This implements the fixed 48-byte header from RFC 5905 (and the compatible
RFC 1305 NTPv3 header).  It is a test fixture: it has no discipline loop,
authentication, leap-state tracking, or persistence, and must not be used as
an appliance time service.  It only serves the host UTC clock (or an injected
clock in tests), never sets the host clock, and ignores malformed datagrams.
"""

from __future__ import annotations

import argparse
import math
import signal
import socket
import struct
import threading
import time
from collections.abc import Callable

NTP_EPOCH = 2_208_988_800
HEADER_SIZE = 48


def ntp_timestamp(seconds: float) -> bytes:
    """Encode Unix seconds as an NTP 64-bit seconds/fraction timestamp."""
    floor = math.floor(seconds)
    whole = floor + NTP_EPOCH
    fraction = int((seconds - floor) * (1 << 32)) & 0xFFFFFFFF
    return struct.pack("!II", whole & 0xFFFFFFFF, fraction)


class NTPFixture:
    """Bounded UDP NTP responder intended for an isolated generic VM."""

    def __init__(self, bind: str = "127.0.0.1", port: int = 123, *, clock: Callable[[], float] = time.time):
        self.bind = bind
        self.port = port
        self.clock = clock
        self._stop = threading.Event()

    def response(self, packet: bytes) -> bytes | None:
        """Return an RFC 5905 server response, or None for an ignored packet."""
        if not 48 <= len(packet) <= 128:
            return None
        first = packet[0]
        version = (first >> 3) & 0x7
        mode = first & 0x7
        if version not in (3, 4) or mode != 3:
            return None
        now_reference = ntp_timestamp(self.clock())
        now_receive = ntp_timestamp(self.clock())
        now_transmit = ntp_timestamp(self.clock())
        out = bytearray(HEADER_SIZE)
        out[0] = (version << 3) | 4  # fixture clock available, same version, server mode
        out[1] = 2  # synthetic reference clock; not a production discipline claim
        out[2] = packet[2]  # poll interval
        out[3] = 0xEC  # precision -20
        struct.pack_into("!II", out, 4, 0, 1 << 16)  # zero delay, 1 second dispersion
        out[12:16] = b"LOCL"
        out[16:24] = now_reference
        out[24:32] = packet[40:48]  # originate = client's transmit timestamp
        out[32:40] = now_receive
        out[40:48] = now_transmit
        return bytes(out)

    def stop(self, *_: object) -> None:
        self._stop.set()

    def serve_forever(self) -> None:
        """Serve UDP with bounded waits until stop() or SIGTERM."""
        with socket.socket(socket.AF_INET6 if ":" in self.bind else socket.AF_INET, socket.SOCK_DGRAM) as sock:
            if sock.family == socket.AF_INET6:
                sock.bind((self.bind, self.port, 0, 0))
            else:
                sock.bind((self.bind, self.port))
            sock.settimeout(0.2)
            while not self._stop.is_set():
                try:
                    # One excess byte distinguishes oversized/truncated datagrams.
                    packet, address = sock.recvfrom(129)
                except socket.timeout:
                    continue
                reply = self.response(packet)
                if reply is not None:
                    try:
                        sock.sendto(reply, address)
                    except OSError:
                        if not self._stop.is_set():
                            raise


def main() -> int:
    parser = argparse.ArgumentParser(description="isolated NTP fixture for generic boot tests")
    parser.add_argument("--bind", required=True, help="address to bind")
    parser.add_argument("--port", required=True, type=int, help="UDP port")
    args = parser.parse_args()
    fixture = NTPFixture(args.bind, args.port)
    signal.signal(signal.SIGTERM, fixture.stop)
    signal.signal(signal.SIGINT, fixture.stop)
    fixture.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
