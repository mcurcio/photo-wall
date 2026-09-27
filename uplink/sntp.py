"""A one-shot SNTP client (RFC 5905 §8 and §14): a pure packet codec with the client-side reply
checks, and a query that asks several servers at once and keeps the first valid answer. No
daemon can run in the initramfs and the stdlib has no SNTP client, so this is the whole of it."""

import errno
import secrets
import selectors
import socket
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol

from contracts.time import Clock, SystemClock

NTP_PORT: Final = 123
Endpoint = tuple[str, int]      # (IP literal, UDP port)

PACKET_BYTES: Final = 48
NTP_EPOCH: Final = 2_208_988_800            # Unix seconds of 1900-01-01, the NTP epoch
ERA_SECONDS: Final = 1 << 32
MAX_ROOT_DISTANCE: Final = 5.0
CEILING_SECONDS: Final = 20 * 365 * 86400   # a server time past floor + 20 years is refused
RETRANSMIT_AFTER: Final = 1.0
_CLIENT_REQUEST: Final = (0 << 6) | (4 << 3) | 3        # LI 0, VN 4, mode 3 (client)
_HEADER = struct.Struct("!BBbbiI4sQQQQ")


@dataclass(frozen=True, slots=True)
class TimeAnswer:
    server: str
    offset: float        # seconds to ADD to the local clock (θ, RFC 5905 §8)
    delay: float         # round trip (δ)


Reason = Literal["nonce", "mode", "version", "unsynchronised", "kiss", "stratum",
                 "zero_transmit", "distance", "below_floor", "above_ceiling"]


@dataclass(frozen=True, slots=True)
class Rejected:
    server: str
    reason: Reason


def build_request(nonce: bytes) -> bytes:
    """48-byte SNTPv4 client packet (LI 0, VN 4, mode 3); transmit timestamp = the 8-byte nonce,
    which the server echoes as the originate timestamp (the only anti-spoofing SNTP has)."""
    if len(nonce) != 8:
        raise ValueError("the nonce is the 8-byte transmit timestamp")
    return bytes([_CLIENT_REQUEST]) + bytes(PACKET_BYTES - 9) + nonce


def _unix(timestamp: int, floor: float) -> float:
    """An NTP 64-bit timestamp as Unix seconds in the era nearest the floor, so every time
    within 68 years of the floor reads right and below/above are both detectable."""
    seconds = (timestamp >> 32) - NTP_EPOCH + (timestamp & 0xFFFFFFFF) / ERA_SECONDS
    return seconds + round((floor - seconds) / ERA_SECONDS) * ERA_SECONDS


def read_reply(packet: bytes, *, server: str, nonce: bytes, sent: float, received: float,
               floor: float) -> TimeAnswer | Rejected:
    """PURE. RFC 5905 client checks, in order: originate == nonce; mode 4; VN 3 or 4; LI != 3;
    stratum 1-15 (0 is kiss-o'-death); transmit != 0; root distance <= 5 s; server time >= floor
    and <= floor + 20 years (the floor also picks the NTP era). `sent` and `received` are the
    local wall clock at send and receive (T1, T4); θ = ((T2-T1)+(T3-T4))/2, δ = (T4-T1)-(T3-T2)."""
    def rejected(reason: Reason) -> Rejected:
        return Rejected(server, reason)

    if len(packet) < PACKET_BYTES or packet[24:32] != nonce:
        return rejected("nonce")
    (first, stratum, _poll, _precision, root_delay, root_dispersion, _reference, _reference_time,
     _originate, receive, transmit) = _HEADER.unpack_from(packet)
    leap, version, mode = first >> 6, (first >> 3) & 0x7, first & 0x7
    if mode != 4:
        return rejected("mode")
    if version not in (3, 4):
        return rejected("version")
    if leap == 3:
        return rejected("unsynchronised")
    if stratum == 0:
        return rejected("kiss")
    if stratum > 15:
        return rejected("stratum")
    if transmit == 0:
        return rejected("zero_transmit")
    t2, t3 = _unix(receive, floor), _unix(transmit, floor)
    delay = (received - sent) - (t3 - t2)
    if (max(root_delay, 0) / 65536 + max(delay, 0)) / 2 + root_dispersion / 65536 \
            > MAX_ROOT_DISTANCE:
        return rejected("distance")
    if t3 < floor:
        return rejected("below_floor")
    if t3 > floor + CEILING_SECONDS:
        return rejected("above_ceiling")
    return TimeAnswer(server, ((t2 - sent) + (t3 - received)) / 2, delay)


class TimeQuery(Protocol):
    def __call__(self, servers: Sequence[Endpoint], *, deadline: float, floor: int
                 ) -> tuple[TimeAnswer | None, tuple[str, ...]]: ...


class _Asked:
    """One server being asked: its connected socket, the nonces sent (with their T1), and its
    outcome once it has one. A server whose socket cannot be opened starts with an outcome."""

    def __init__(self, endpoint: Endpoint) -> None:
        self.server = endpoint[0]
        self.sent: dict[bytes, float] = {}
        self.outcome: str | None = None
        self.sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_INET6 if ":" in endpoint[0] else socket.AF_INET,
                                 socket.SOCK_DGRAM)
        except OSError as error:
            self.outcome = _errno_name(error)
            return
        try:
            sock.setblocking(False)
            sock.connect(endpoint)      # the kernel then drops datagrams from other sources
        except OSError as error:
            sock.close()
            self.outcome = _errno_name(error)
            return
        self.sock = sock

    def send(self, clock: Clock) -> None:
        nonce = secrets.token_bytes(8)
        self.sent[nonce] = clock.utc()
        try:
            self.sock.send(build_request(nonce))
        except OSError as error:
            self.outcome = _errno_name(error)

    def receive(self, clock: Clock, floor: int) -> TimeAnswer | None:
        """The answer in the datagram waiting, if it is a valid one. A reply to no nonce we
        sent is ignored; any refused reply (a kiss-o'-death included) ends this server."""
        try:
            packet = self.sock.recv(PACKET_BYTES * 4)
        except BlockingIOError:
            return None
        except OSError as error:        # ECONNREFUSED: an ICMP port unreachable came back
            self.outcome = _errno_name(error)
            return None
        received = clock.utc()
        nonce = packet[24:32]
        if nonce not in self.sent:
            return None
        result = read_reply(packet, server=self.server, nonce=nonce, sent=self.sent[nonce],
                            received=received, floor=floor)
        if isinstance(result, Rejected):
            self.outcome = result.reason
            return None
        self.outcome = "ok"
        return result


def _errno_name(error: OSError) -> str:
    return errno.errorcode.get(error.errno, type(error).__name__)


def query_first(servers: Sequence[Endpoint], *, deadline: float, floor: int,
                clock: Clock = SystemClock()) -> tuple[TimeAnswer | None, tuple[str, ...]]:
    """The real TimeQuery. Ask every endpoint at once, retransmit once after 1 s, and return
    the first valid answer before `deadline` (monotonic), plus one 'server:outcome' note per
    endpoint. `clock` must have a moving monotonic time; tests drive it against the NTP
    fixture on an ephemeral port. Never raises for a network failure."""
    asked = [_Asked(endpoint) for endpoint in servers]
    answer: TimeAnswer | None = None
    try:
        with selectors.DefaultSelector() as selector:
            def ask(server: _Asked) -> None:
                server.send(clock)
                if server.outcome is not None and server.sock in selector.get_map():
                    selector.unregister(server.sock)

            for server in asked:
                if server.sock is not None:
                    selector.register(server.sock, selectors.EVENT_READ, server)
                    ask(server)
            retransmit_at: float | None = clock.monotonic() + RETRANSMIT_AFTER
            while answer is None and any(server.outcome is None for server in asked):
                now = clock.monotonic()
                if now >= deadline:
                    break
                if retransmit_at is not None and now >= retransmit_at:
                    for server in asked:
                        if server.outcome is None:
                            ask(server)
                    retransmit_at = None
                wake = deadline if retransmit_at is None else min(deadline, retransmit_at)
                for key, _ in selector.select(max(wake - now, 0.0)):
                    server = key.data
                    answer = server.receive(clock, floor)
                    if server.outcome is not None:
                        selector.unregister(server.sock)    # done: never woken by it again
                    if answer is not None:
                        break
    finally:
        for server in asked:
            if server.sock is not None:
                server.sock.close()
    return answer, tuple(f"{server.server}:{server.outcome or 'timeout'}" for server in asked)
