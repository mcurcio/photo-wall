"""The SNTP codec (pure, RFC 5905 client checks with worked θ and δ) and the first-answer query
against the repo's NTP fixture on an ephemeral UDP port."""

import contextlib
import socket
import struct
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from scripts.boot_time_fixture import NTPFixture, ntp_timestamp
from uplink.sntp import (
    ERA_SECONDS,
    NTP_EPOCH,
    NTP_PORT,
    Rejected,
    TimeAnswer,
    build_request,
    query_first,
    read_reply,
)

FLOOR = 1790380800            # 2026-09-26
NONCE = bytes(range(1, 9))


def packet(*, receive: float, transmit: float, nonce: bytes = NONCE, leap: int = 0,
           version: int = 4, mode: int = 4, stratum: int = 2, root_delay: float = 0.0,
           root_dispersion: float = 0.01) -> bytes:
    first = (leap << 6) | (version << 3) | mode
    head = struct.pack("!BBbbiI4s", first, stratum, 6, -20, int(root_delay * 65536),
                       int(root_dispersion * 65536), b"GPS\x00")
    return (head + ntp_timestamp(transmit - 1) + nonce + ntp_timestamp(receive)
            + (bytes(8) if transmit == 0 else ntp_timestamp(transmit)))


def read(data: bytes, *, sent: float = FLOOR + 0.0, received: float = FLOOR + 0.5,
         floor: float = FLOOR) -> TimeAnswer | Rejected:
    return read_reply(data, server="192.0.2.1", nonce=NONCE, sent=sent, received=received,
                      floor=floor)


def test_the_request_is_a_48_byte_v4_client_packet_carrying_the_nonce():
    request = build_request(NONCE)
    assert len(request) == 48 and request[0] == 0b00_100_011 and request[40:48] == NONCE
    with pytest.raises(ValueError):
        build_request(b"short")


def test_a_valid_reply_gives_the_rfc_5905_offset_and_delay():
    # T1 = F, T2 = F + 3600.1, T3 = F + 3600.2, T4 = F + 0.5:
    # θ = ((T2 - T1) + (T3 - T4)) / 2 = (3600.1 + 3599.7) / 2 = 3599.9
    # δ = (T4 - T1) - (T3 - T2) = 0.5 - 0.1 = 0.4
    answer = read(packet(receive=FLOOR + 3600.1, transmit=FLOOR + 3600.2))
    assert isinstance(answer, TimeAnswer) and answer.server == "192.0.2.1"
    assert answer.offset == pytest.approx(3599.9, abs=1e-6)
    assert answer.delay == pytest.approx(0.4, abs=1e-6)


def test_a_version_3_server_is_accepted():
    assert isinstance(read(packet(receive=FLOOR + 1, transmit=FLOOR + 1, version=3)), TimeAnswer)


@pytest.mark.parametrize(("data", "reason"), [
    (packet(receive=FLOOR, transmit=FLOOR, nonce=bytes(8)), "nonce"),
    (packet(receive=FLOOR, transmit=FLOOR)[:47], "nonce"),
    (packet(receive=FLOOR, transmit=FLOOR, mode=3), "mode"),
    (packet(receive=FLOOR, transmit=FLOOR, version=2), "version"),
    (packet(receive=FLOOR, transmit=FLOOR, leap=3), "unsynchronised"),
    (packet(receive=FLOOR, transmit=FLOOR, stratum=0), "kiss"),
    (packet(receive=FLOOR, transmit=FLOOR, stratum=16), "stratum"),
    (packet(receive=FLOOR, transmit=0), "zero_transmit"),
    (packet(receive=FLOOR, transmit=FLOOR, root_dispersion=5.5), "distance"),
    (packet(receive=FLOOR, transmit=FLOOR, root_delay=11.0), "distance"),
    (packet(receive=FLOOR - 60, transmit=FLOOR - 60), "below_floor"),
    (packet(receive=FLOOR + 21 * 365 * 86400, transmit=FLOOR + 21 * 365 * 86400),
     "above_ceiling"),
], ids=["nonce", "short", "mode", "version", "leap", "kiss", "stratum", "zero-transmit",
        "dispersion", "root-delay", "below-floor", "above-ceiling"])
def test_each_rfc_5905_refusal_is_named(data, reason):
    assert read(data) == Rejected("192.0.2.1", reason)


def test_the_floor_picks_the_ntp_era_after_the_2036_wrap():
    floor = ERA_SECONDS - NTP_EPOCH + 1000          # past 2036-02-07, the era-1 rollover
    answer = read(packet(receive=floor + 10, transmit=floor + 10), floor=floor,
                  sent=floor + 10, received=floor + 10)
    assert isinstance(answer, TimeAnswer) and answer.offset == pytest.approx(0, abs=1e-6)


# --- query_first against the NTP fixture on an ephemeral port --------------------------------

@contextlib.contextmanager
def udp_server(answer: Callable[[bytes, int], bytes | None]) -> Iterator[int]:
    """A UDP peer on 127.0.0.1:0 that sends `answer(request, index)` for each request."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.05)
    stop = threading.Event()

    def serve() -> None:
        index = 0
        while not stop.is_set():
            try:
                request, peer = sock.recvfrom(1024)
            except OSError:
                continue
            reply = answer(request, index)
            index += 1
            if reply is not None:
                for datagram in reply if isinstance(reply, list) else [reply]:
                    sock.sendto(datagram, peer)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield sock.getsockname()[1]
    finally:
        stop.set()
        thread.join(timeout=5)
        sock.close()


def fixture_answer(request: bytes, _index: int) -> bytes | None:
    return NTPFixture().response(request)


def test_the_fixture_answer_matches_the_local_clock_within_50_ms():
    with udp_server(fixture_answer) as port:
        answer, notes = query_first([("127.0.0.1", port)], deadline=time.monotonic() + 3,
                                    floor=FLOOR)
    assert answer is not None and abs(answer.offset) < 0.05
    assert notes == ("127.0.0.1:ok",)


def test_a_silent_endpoint_returns_nothing_by_the_deadline():
    with udp_server(lambda request, index: None) as port:
        start = time.monotonic()
        answer, notes = query_first([("127.0.0.1", port)], deadline=start + 1.5, floor=FLOOR)
    assert answer is None and notes == ("127.0.0.1:timeout",)
    assert 1.5 <= time.monotonic() - start < 2.0


def test_one_retransmit_follows_after_a_second():
    with udp_server(lambda request, index: None if index == 0 else
                    NTPFixture().response(request)) as port:
        start = time.monotonic()
        answer, _ = query_first([("127.0.0.1", port)], deadline=start + 3, floor=FLOOR)
    assert answer is not None and time.monotonic() - start >= 1.0


def test_a_spoofed_nonce_is_ignored_and_the_real_answer_kept():
    def spoof_first(request: bytes, _index: int) -> list[bytes]:
        real = NTPFixture().response(request)
        forged = real[:24] + bytes(8) + real[32:40] + ntp_timestamp(time.time() + 86400)
        return [forged, real]

    with udp_server(spoof_first) as port:
        answer, notes = query_first([("127.0.0.1", port)], deadline=time.monotonic() + 3,
                                    floor=FLOOR)
    assert answer is not None and abs(answer.offset) < 0.05 and notes == ("127.0.0.1:ok",)


def test_a_kiss_of_death_ends_that_server_at_once():
    def kiss(request: bytes, _index: int) -> bytes:
        reply = bytearray(NTPFixture().response(request))
        reply[1] = 0
        return bytes(reply)

    with udp_server(kiss) as port:
        start = time.monotonic()
        answer, notes = query_first([("127.0.0.1", port)], deadline=start + 5, floor=FLOOR)
    assert answer is None and notes == ("127.0.0.1:kiss",)
    assert time.monotonic() - start < 1.0


def test_every_server_is_asked_at_once_and_the_first_answer_wins():
    with udp_server(lambda request, index: None) as silent, udp_server(fixture_answer) as port:
        answer, notes = query_first([("127.0.0.1", silent), ("127.0.0.1", port)],
                                    deadline=time.monotonic() + 3, floor=FLOOR)
    assert answer is not None
    assert notes == ("127.0.0.1:timeout", "127.0.0.1:ok")


def test_no_servers_is_no_answer():
    assert query_first([], deadline=time.monotonic() + 1, floor=FLOOR) == (None, ())


def test_ntp_port_is_123():
    assert NTP_PORT == 123
