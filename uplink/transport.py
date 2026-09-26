"""One GET at a time, over http.client: never follows a redirect, never uses a proxy, bounds
every wait, and raises only UplinkError, classified where the error happens."""

import http.client
import socket
import ssl
import time
from collections.abc import Callable, Mapping
from typing import NoReturn, Protocol

from uplink.causes import Cause, Phase, UplinkError, classify
from uplink.lookup import lookup as default_lookup
from uplink.origin import DEFAULT_PORTS, Url
from uplink.trust import Trust

LOOKUP_TIMEOUT = 7.0            # per lookup: lets a second nameserver answer after glibc's 5 s
HOP_TIMEOUT = 5.0               # per request: connect, TLS, request write, status line

# (host, port, timeout) -> [(family, address), ...] in the order to try them
Lookup = Callable[[str, int, float], list[tuple[int, str]]]


class Reply(Protocol):
    @property
    def status(self) -> int: ...
    @property
    def headers(self) -> Mapping[str, str]: ...      # case-insensitive
    @property
    def peer(self) -> str: ...                       # the address connected to, for the log
    def read(self, amount: int, *, timeout: float) -> bytes:
        """At most `amount` bytes; b"" at the end; waits at most `timeout` seconds.
        Raises only UplinkError(TRANSFER, ...) (classify rules 14-16)."""
    def close(self) -> None: ...


class Transport(Protocol):
    def send(self, url: Url, *, headers: Mapping[str, str], deadline: float) -> Reply:
        """Exactly one GET. Never follows a redirect; never uses a proxy. `deadline` is absolute
        monotonic time. Raises only UplinkError (DNS, CONNECT, TLS or TIME), classified at the
        source (classify rules 1-13)."""


def _raise_classified(error: BaseException, phase: Phase, host: str) -> NoReturn:
    classified = classify(error, phase=phase, host=host)
    if classified is None:
        raise error                 # not a network error: a programming error, unchanged
    raise classified from error


class _Connection(http.client.HTTPConnection):
    """One connection to one looked-up address, TLS-wrapped when `context` is given. SNI, the
    certificate name check and the Host header use the URL's host, never the address. Every
    wait up to the status line is bounded by what is left of the attempt's `deadline`."""

    def __init__(self, url: Url, *, family: int, address: str,
                 context: ssl.SSLContext | None, deadline: float,
                 monotonic: Callable[[], float]) -> None:
        super().__init__(url.origin.host, url.origin.port)
        self.default_port = DEFAULT_PORTS[url.origin.scheme]  # Host omits the default port
        self._family, self._address, self._context = family, address, context
        self._deadline, self._monotonic = deadline, monotonic
        self.raw: socket.socket | None = None

    def left(self) -> float:
        left = self._deadline - self._monotonic()
        if left <= 0:
            raise TimeoutError("hop deadline passed")
        return left

    def connect(self) -> None:
        sock = socket.socket(self._family, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.left())
            sock.connect((self._address, self.port))
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if self._context is not None:
                sock = self._context.wrap_socket(sock, server_hostname=self.host)
                sock.settimeout(self.left())
        except BaseException:
            sock.close()
            raise
        self.sock = self.raw = sock


class _HttpReply:
    __slots__ = ("_connection", "_response", "_peer", "_host")

    def __init__(self, connection: _Connection, response: http.client.HTTPResponse, *,
                 peer: str, host: str) -> None:
        self._connection, self._response, self._peer, self._host = (
            connection, response, peer, host)

    @property
    def status(self) -> int:
        return self._response.status

    @property
    def headers(self) -> Mapping[str, str]:
        return self._response.headers

    @property
    def peer(self) -> str:
        return self._peer

    def read(self, amount: int, *, timeout: float) -> bytes:
        if self._response.isclosed():   # the body ended; its socket may be gone with it
            return b""
        if timeout <= 0:
            raise UplinkError(Cause.TRANSFER, "deadline", host=self._host)
        try:
            # The connection may already have handed its socket to the response; the raw
            # socket stays open under the response's file until close().
            self._connection.raw.settimeout(timeout)
            remaining = self._response.length       # None unless Content-Length was sent
            chunk = self._response.read1(amount)    # one read from the socket at most
            if not chunk and amount and remaining:
                # read1 reports a peer that closed before Content-Length as a quiet end.
                raise http.client.IncompleteRead(b"", remaining)
            return chunk
        except (OSError, http.client.HTTPException) as error:
            _raise_classified(error, "transfer", self._host)

    def close(self) -> None:
        self._response.close()
        self._connection.close()


class HttpTransport:
    """The real Transport, over http.client. https uses trust.context only. The host is looked
    up with `lookup`, bounded by min(LOOKUP_TIMEOUT, time left). Every returned address is tried
    in order, as socket.create_connection does, each within min(HOP_TIMEOUT, time left); the
    first that completes the exchange up to the status line wins and becomes Reply.peer; when
    all fail, the LAST address's error is classified. SNI and the hostname check always use
    the URL's host, never the address."""

    def __init__(self, *, trust: Trust, lookup: Lookup = default_lookup,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._trust, self._lookup, self._monotonic = trust, lookup, monotonic

    def send(self, url: Url, *, headers: Mapping[str, str], deadline: float) -> Reply:
        host = url.origin.host
        try:
            addresses = self._lookup(host, url.origin.port,
                                     min(LOOKUP_TIMEOUT, deadline - self._monotonic()))
        except Exception as error:
            _raise_classified(error, "connect", host)
        if not addresses:
            raise UplinkError(Cause.DNS, "failed", host=host, detail="no_address")
        context = self._trust.context if url.origin.scheme == "https" else None
        last: BaseException | None = None
        for family, address in addresses:
            if deadline - self._monotonic() <= 0:
                break
            connection = _Connection(
                url, family=family, address=address, context=context, monotonic=self._monotonic,
                deadline=min(self._monotonic() + HOP_TIMEOUT, deadline))
            try:
                connection.request("GET", url.target, headers=dict(headers))
                connection.raw.settimeout(connection.left())
                response = connection.getresponse()
                if response.status < 200:
                    # A 1xx that is not 100 Continue (a 101 upgrade): no request/response HTTP.
                    raise http.client.HTTPException(f"informational status {response.status}")
            except (OSError, http.client.HTTPException) as error:
                connection.close()
                last = error
                continue
            except BaseException:
                connection.close()
                raise
            return _HttpReply(connection, response, peer=address, host=host)
        if last is None:
            raise UplinkError(Cause.CONNECT, "timeout", host=host, detail="deadline")
        _raise_classified(last, "connect", host)
