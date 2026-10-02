"""One direct HTTP exchange at a time, over http.client: never follows a redirect or proxy, bounds
every wait, and raises only UplinkError, classified where the error happens."""

import http.client
import io
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
HOP_TIMEOUT = 5.0               # per address: connect and TLS; by default also the status line

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
    def send(self, url: Url, *, headers: Mapping[str, str], deadline: float,
             status_timeout: float = HOP_TIMEOUT, method: str = "GET",
             body: bytes | None = None) -> Reply:
        """Exactly one GET or bounded POST. Never follows a redirect or proxy. `deadline` is absolute
        monotonic time. `status_timeout` bounds each address's exchange up to and including the
        status line; a caller whose server may hold the answer longer passes a longer one.
        Raises only UplinkError (DNS, CONNECT, TLS or TIME), classified at the source (classify
        rules 1-13)."""


def _raise_classified(error: BaseException, phase: Phase, host: str) -> NoReturn:
    classified = classify(error, phase=phase, host=host)
    if classified is None:
        raise error                 # not a network error: a programming error, unchanged
    raise classified from error


class _DeadlineSocket:
    """The only socket http.client sees. Every send and receive waits at most what is left
    before `deadline` (absolute monotonic time) and raises TimeoutError once none is left, so
    no http.client path (status line, each header line, chunked body, trailers) can outlive
    it, however many reads it makes. It wraps the socket AFTER TLS, so the deadline bounds
    records, not raw bytes under them. Cost: one settimeout per call. Closing follows
    socket.socket: the wrapped socket closes when this is closed and no file made by
    makefile() is still open (http.client closes the connection before its response)."""

    def __init__(self, sock: socket.socket, *, deadline: float,
                 monotonic: Callable[[], float]) -> None:
        self._sock, self.deadline, self._monotonic = sock, deadline, monotonic
        self._io_refs, self._closed = 0, False

    def _bound(self) -> None:
        left = self.deadline - self._monotonic()
        if left <= 0:
            raise TimeoutError("exchange deadline passed")
        self._sock.settimeout(left)

    def recv_into(self, buffer, nbytes: int = 0, flags: int = 0) -> int:
        self._bound()
        return self._sock.recv_into(buffer, nbytes, flags)

    def recv(self, bufsize: int, flags: int = 0) -> bytes:
        self._bound()
        return self._sock.recv(bufsize, flags)

    def sendall(self, data) -> None:
        with memoryview(data) as view, view.cast("B") as octets:
            sent = 0
            while sent < len(octets):
                self._bound()
                sent += self._sock.send(octets[sent:])

    def makefile(self, mode: str = "rb", buffering: int = -1, **_: object) -> io.BufferedReader:
        if mode != "rb":
            raise ValueError("only a binary read file is supported")
        self._io_refs += 1
        return io.BufferedReader(socket.SocketIO(self, "rb"),  # type: ignore[arg-type]
                                 buffering if buffering > 0 else io.DEFAULT_BUFFER_SIZE)

    def fileno(self) -> int:
        return self._sock.fileno()

    def _decref_socketios(self) -> None:      # called by SocketIO.close
        self._io_refs -= 1
        if self._closed and self._io_refs <= 0:
            self._sock.close()

    def close(self) -> None:
        self._closed = True
        if self._io_refs <= 0:
            self._sock.close()


class _Connection(http.client.HTTPConnection):
    """One connection to one looked-up address, TLS-wrapped when `context` is given. SNI, the
    certificate name check and the Host header use the URL's host, never the address. The TCP
    connect and the TLS handshake are bounded by what is left of `connect_deadline`; after
    that every send and receive is bounded by `deadline` through a _DeadlineSocket, whose
    deadline the reply moves for each body read."""

    def __init__(self, url: Url, *, family: int, address: str,
                 context: ssl.SSLContext | None, connect_deadline: float, deadline: float,
                 monotonic: Callable[[], float]) -> None:
        super().__init__(url.origin.host, url.origin.port)
        self.default_port = DEFAULT_PORTS[url.origin.scheme]  # Host omits the default port
        self._family, self._address, self._context = family, address, context
        self._connect_deadline, self._deadline = connect_deadline, deadline
        self._monotonic = monotonic
        self.bounded: _DeadlineSocket | None = None

    def _left(self, deadline: float) -> float:
        left = deadline - self._monotonic()
        if left <= 0:
            raise TimeoutError("hop deadline passed")
        return left

    def connect(self) -> None:
        sock = socket.socket(self._family, socket.SOCK_STREAM)
        try:
            sock.settimeout(self._left(self._connect_deadline))
            sock.connect((self._address, self.port))
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if self._context is not None:
                sock.settimeout(self._left(self._connect_deadline))  # what the connect left
                sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise
        self.sock = self.bounded = _DeadlineSocket(sock, deadline=self._deadline,
                                                   monotonic=self._monotonic)


class _HttpReply:
    __slots__ = ("_connection", "_response", "_peer", "_host", "_monotonic")

    def __init__(self, connection: _Connection, response: http.client.HTTPResponse, *,
                 peer: str, host: str, monotonic: Callable[[], float]) -> None:
        self._connection, self._response, self._peer, self._host, self._monotonic = (
            connection, response, peer, host, monotonic)

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
            # The connection may already have handed its socket to the response; the bounded
            # socket stays open under the response's file until close(). The whole read (a
            # chunk-size line, the chunk, the trailers) ends by now + timeout.
            self._connection.bounded.deadline = self._monotonic() + timeout
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
    in order, as socket.create_connection does, each within min(status_timeout, time left),
    of which the TCP connect and the TLS handshake get at most HOP_TIMEOUT, so a dead address
    falls through to the next as fast whatever the caller's bound; the first that completes
    the exchange up to the status line wins and becomes Reply.peer; when all fail, the LAST
    address's error is classified. SNI and the hostname check always use the URL's host, never
    the address."""

    def __init__(self, *, trust: Trust, lookup: Lookup = default_lookup,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._trust, self._lookup, self._monotonic = trust, lookup, monotonic

    def send(self, url: Url, *, headers: Mapping[str, str], deadline: float,
             status_timeout: float = HOP_TIMEOUT, method: str = "GET",
             body: bytes | None = None) -> Reply:
        if method not in ("GET", "POST") or (method == "GET" and body is not None):
            raise ValueError("unsupported request")
        if body is not None and len(body) > 8192:
            raise ValueError("request body too large")
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
            start = self._monotonic()
            if deadline - start <= 0:
                break
            answer_by = min(start + status_timeout, deadline)
            connection = _Connection(
                url, family=family, address=address, context=context, monotonic=self._monotonic,
                connect_deadline=min(start + HOP_TIMEOUT, answer_by), deadline=answer_by)
            try:
                connection.request(method, url.target, body=body, headers=dict(headers))
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
            return _HttpReply(connection, response, peer=address, host=host,
                              monotonic=self._monotonic)
        if last is None:
            raise UplinkError(Cause.CONNECT, "timeout", host=host, detail="deadline")
        _raise_classified(last, "connect", host)
