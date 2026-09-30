"""HttpTransport and lookup over real sockets: peers that do not speak HTTP, address fallback,
and every wait bounded and named by what it was waiting for."""

import contextlib
import errno
import socket
import ssl
import threading
import time
from collections.abc import Iterator

import pytest

from tests import tls_fixture as tls
from uplink import transport as transport_module
from uplink.causes import Cause, UplinkError
from uplink.lookup import LookupTimeout, lookup
from uplink.origin import Origin, Url
from uplink.transport import HttpTransport
from uplink.trust import Trust


@contextlib.contextmanager
def raw_peer(answer: bytes = b"", *, hold: bool = False, delay: float = 0) -> Iterator[int]:
    """A TCP peer on 127.0.0.1 that reads one request head, sends `answer` after `delay`
    seconds, then either holds the connection open until the test ends (`hold`) or closes its
    side cleanly. Yields its port."""
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.05)
    stop = threading.Event()

    def serve(connection: socket.socket) -> None:
        with connection, contextlib.suppress(OSError):
            connection.settimeout(10)
            head = b""
            while b"\r\n\r\n" not in head:
                data = connection.recv(4096)
                if not data:
                    return
                head += data
            stop.wait(delay)
            connection.sendall(answer)
            if hold:
                stop.wait(10)
                return
            connection.shutdown(socket.SHUT_WR)
            while connection.recv(4096):      # until the client closes: a FIN, never an RST
                pass

    def accept() -> None:
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            threading.Thread(target=serve, args=(connection,), daemon=True).start()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        stop.set()
        thread.join(5)
        listener.close()


# The absolute deadline of these tests, and how late past it a failure may come.
TRICKLE_DEADLINE = 1.0
TRICKLE_SLACK = 0.5
TRICKLE_LIMIT = 4.0


@contextlib.contextmanager
def trickle_peer(prefix: bytes, byte: bytes, *, every: float = 0.2,
                 tls_server: bool = False) -> Iterator[int]:
    """A TCP peer on 127.0.0.1 (TLS as tls.CENTRAL when `tls_server`) that reads one request
    head, sends `prefix`, then one `byte` every `every` seconds for TRICKLE_LIMIT seconds
    (then closes, so a regression fails its timing assertion instead of hanging): each recv()
    succeeds, so only an absolute deadline can end the exchange sooner. Yields its port."""
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.05)
    stop = threading.Event()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    if tls_server:
        with tls.CENTRAL.files() as files:
            context.load_cert_chain(*files)

    def serve(connection: socket.socket) -> None:
        with contextlib.suppress(OSError):
            connection.settimeout(10)
            if tls_server:
                connection = context.wrap_socket(connection, server_side=True)
        with connection, contextlib.suppress(OSError):
            connection.settimeout(10)
            head = b""
            while b"\r\n\r\n" not in head:
                data = connection.recv(4096)
                if not data:
                    return
                head += data
            connection.sendall(prefix)
            until = time.monotonic() + TRICKLE_LIMIT
            while time.monotonic() < until and not stop.wait(every):
                connection.sendall(byte)

    def accept() -> None:
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            threading.Thread(target=serve, args=(connection,), daemon=True).start()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        stop.set()
        thread.join(5)
        listener.close()


def closed_port() -> int:
    with socket.create_server(("127.0.0.1", 0)) as listener:
        return listener.getsockname()[1]


@pytest.fixture
def trust(tmp_path) -> Trust:
    return Trust.public(tls.write_bundle(tmp_path / "ca.pem", tls.CA))


def at(port: int, host: str = "127.0.0.1", scheme: str = "http") -> Url:
    return Url(Origin(scheme, host, port), "/v1/locate")


def test_bounded_post_uses_same_direct_transport_without_redirect(trust):
    body = b'{"schema":1}'
    received = []
    with socket.create_server(("127.0.0.1", 0)) as listener:
        port = listener.getsockname()[1]

        def serve():
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(3)
                data = bytearray()
                while b"\r\n\r\n" not in data:
                    data.extend(connection.recv(4096))
                head, _, tail = bytes(data).partition(b"\r\n\r\n")
                while len(tail) < len(body):
                    tail += connection.recv(4096)
                received.append((head.split(b"\r\n", 1)[0], tail[:len(body)]))
                connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        reply = HttpTransport(trust=trust).send(at(port), headers={"Content-Type": "application/json"},
                                                 deadline=time.monotonic() + 3,
                                                 method="POST", body=body)
        try:
            assert reply.read(2, timeout=1) == b"ok"
        finally:
            reply.close()
        thread.join(3)
    assert received == [(b"POST /v1/locate HTTP/1.1", body)]
    with pytest.raises(ValueError, match="too large"):
        HttpTransport(trust=trust).send(at(1), headers={}, deadline=time.monotonic() + 1,
                                        method="POST", body=b"x" * 8193)


def failure(transport: HttpTransport, url: Url, *, seconds: float = 5.0,
            **bounds: float) -> UplinkError:
    with pytest.raises(UplinkError) as caught:
        transport.send(url, headers={}, deadline=time.monotonic() + seconds, **bounds)
    return caught.value


@pytest.mark.parametrize(("answer", "reason"), [
    (b"SSH-2.0-OpenSSH_9.6\r\n", "protocol"),                       # BadStatusLine
    (b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n", "protocol"),
    (b"HTTP/1.1 200 OK" + b"x" * 70_000 + b"\r\n\r\n", "protocol"),  # LineTooLong
    (b"HTTP/1.1 200 OK\r\n" + b"X-Header: 1\r\n" * 101 + b"\r\n", "protocol"),
    (b"", "closed"),                                                  # RemoteDisconnected
], ids=["ssh-banner", "101", "line-too-long", "too-many-headers", "closed"])
def test_a_peer_that_does_not_answer_in_http_is_a_connect_failure(trust, answer, reason):
    with raw_peer(answer) as port:
        error = failure(HttpTransport(trust=trust), at(port))
    assert (error.cause, error.reason, error.host) == (Cause.CONNECT, reason, "127.0.0.1")


def test_a_peer_that_stalls_before_the_status_line_is_a_connect_timeout(trust):
    with raw_peer(hold=True) as port:
        started = time.monotonic()
        error = failure(HttpTransport(trust=trust), at(port), seconds=0.4)
    assert (error.cause, error.reason) == (Cause.CONNECT, "timeout")
    assert time.monotonic() - started < 3


def test_each_hop_is_bounded_even_when_the_deadline_is_far(trust):
    with raw_peer(hold=True) as port:
        started = time.monotonic()
        error = failure(HttpTransport(trust=trust), at(port), seconds=30, status_timeout=0.3)
    assert (error.cause, error.reason) == (Cause.CONNECT, "timeout")
    assert time.monotonic() - started < 3


def test_a_status_line_later_than_the_hop_is_received_within_a_longer_bound(trust):
    answer = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
    with raw_peer(answer, delay=0.6) as port:
        error = failure(HttpTransport(trust=trust), at(port), status_timeout=0.3)
        reply = HttpTransport(trust=trust).send(at(port), headers={},
                                                deadline=time.monotonic() + 5, status_timeout=2)
        reply.close()
    assert (error.cause, error.reason, reply.status) == (Cause.CONNECT, "timeout", 200)


def test_a_longer_status_bound_leaves_the_tls_handshake_within_the_hop(trust, monkeypatch):
    # A dead address still falls through to the next one as fast, whatever the caller's bound.
    monkeypatch.setattr(transport_module, "HOP_TIMEOUT", 0.3)
    with raw_peer(hold=True) as port:        # accepts TCP, never answers the ClientHello
        started = time.monotonic()
        error = failure(HttpTransport(trust=trust), at(port, scheme="https"), seconds=30,
                        status_timeout=30)
    assert (error.cause, error.reason) == (Cause.CONNECT, "timeout")
    assert time.monotonic() - started < 3


def test_the_tls_handshake_gets_only_what_the_tcp_connect_left(trust, monkeypatch):
    skew = [0.0]
    real_connect = socket.socket.connect

    def slow_connect(sock: socket.socket, address) -> None:
        real_connect(sock, address)
        skew[0] += transport_module.HOP_TIMEOUT - 0.2    # the connect used all but 0.2 s

    monkeypatch.setattr(socket.socket, "connect", slow_connect)
    transport = HttpTransport(trust=trust, monotonic=lambda: time.monotonic() + skew[0])
    with raw_peer(hold=True) as port:        # accepts TCP, never answers the ClientHello
        started = time.monotonic()
        error = failure(transport, at(port, scheme="https"), seconds=30)
    assert (error.cause, error.reason) == (Cause.CONNECT, "timeout")
    assert time.monotonic() - started < 2    # 0.2 s, not a fresh HOP_TIMEOUT


class Addresses(list):
    """A lookup answer that records how many addresses the transport took from it."""

    taken = 0

    def __iter__(self):
        for self.taken, address in enumerate(super().__iter__(), start=1):
            yield address


def fixed(*addresses: tuple[int, str]) -> tuple[Addresses, object]:
    answer = Addresses(addresses)
    return answer, lambda host, port, timeout: answer


V6_LOOPBACK, V4_LOOPBACK = (socket.AF_INET6, "::1"), (socket.AF_INET, "127.0.0.1")


def test_a_refused_first_address_falls_back_to_the_next(trust):
    _, lookup_ = fixed(V6_LOOPBACK, V4_LOOPBACK)   # the stub listens on 127.0.0.1 only
    with tls.serve_stub(tls.central_stub()) as stub:
        reply = HttpTransport(trust=trust, lookup=lookup_).send(
            at(stub.port, "central.example"), headers={}, deadline=time.monotonic() + 5)
        assert (reply.status, reply.peer) == (200, "127.0.0.1")
        reply.close()


def test_the_first_address_that_answers_wins_and_the_rest_are_never_tried(trust):
    answer, lookup_ = fixed(V4_LOOPBACK, V6_LOOPBACK)
    with tls.serve_stub(tls.central_stub()) as stub:
        reply = HttpTransport(trust=trust, lookup=lookup_).send(
            at(stub.port, "central.example"), headers={}, deadline=time.monotonic() + 5)
        reply.close()
    assert reply.peer == "127.0.0.1" and answer.taken == 1 and len(stub.requests) == 1


def test_when_every_address_fails_the_last_error_is_named(trust):
    _, lookup_ = fixed(V6_LOOPBACK, V4_LOOPBACK)
    error = failure(HttpTransport(trust=trust, lookup=lookup_), at(closed_port(), "c.example"))
    assert (error.cause, error.reason, error.host) == (Cause.CONNECT, "refused", "c.example")


def test_no_address_at_all_is_a_dns_failure(trust):
    _, lookup_ = fixed()
    error = failure(HttpTransport(trust=trust, lookup=lookup_), at(80, "c.example"))
    assert (error.cause, error.reason) == (Cause.DNS, "failed")


@pytest.fixture
def slow_resolver(monkeypatch):
    release = threading.Event()

    def getaddrinfo(*args, **kwargs):
        release.wait(10)
        raise socket.gaierror(socket.EAI_AGAIN, "released")

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield
    release.set()


def test_a_lookup_that_outlasts_the_time_left_is_a_dns_timeout(trust, slow_resolver):
    started = time.monotonic()
    error = failure(HttpTransport(trust=trust), at(80, "slow.example"), seconds=0.3)
    assert (error.cause, error.reason, error.host) == (Cause.DNS, "timeout", "slow.example")
    assert time.monotonic() - started < 2


def test_lookup_raises_lookup_timeout_when_the_resolver_hangs(slow_resolver):
    with pytest.raises(LookupTimeout):
        lookup("slow.example", 443, 0.1)


def test_lookup_answers_an_ip_literal_without_the_resolver(monkeypatch):
    calls = []
    real = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *args, **kwargs: calls.append(kwargs) or real(*args, **kwargs))
    assert lookup("192.0.2.1", 80, 0) == [(socket.AF_INET, "192.0.2.1")]
    assert calls == [{"type": socket.SOCK_STREAM, "flags": socket.AI_NUMERICHOST}]


def test_lookup_names_an_unencodable_name_as_a_failed_lookup():
    with pytest.raises(socket.gaierror):
        lookup("a" * 64 + ".example", 80, 5)


def reply_from(trust: Trust, answer: bytes, *, hold: bool = False):
    stack = contextlib.ExitStack()
    port = stack.enter_context(raw_peer(answer, hold=hold))
    reply = HttpTransport(trust=trust).send(at(port), headers={},
                                            deadline=time.monotonic() + 5)
    stack.callback(reply.close)
    return stack, reply


def test_a_body_is_read_to_its_end_and_then_reads_empty(trust):
    stack, reply = reply_from(trust, b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
    with stack:
        assert reply.read(10, timeout=5) == b"ok"
        assert reply.read(10, timeout=5) == b""
        assert reply.read(10, timeout=5) == b""


def test_a_body_cut_before_its_length_is_a_short_transfer(trust):
    stack, reply = reply_from(trust, b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nhalf")
    with stack, pytest.raises(UplinkError) as caught:
        while reply.read(10, timeout=5):
            pass
    assert (caught.value.cause, caught.value.reason) == (Cause.TRANSFER, "short")


def test_a_body_that_stalls_is_a_transfer_deadline(trust):
    stack, reply = reply_from(trust, b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n", hold=True)
    with stack:
        for timeout in (0.2, 0.0):          # a stalled read, and no time left at all
            with pytest.raises(UplinkError) as caught:
                reply.read(10, timeout=timeout)
            assert (caught.value.cause, caught.value.reason) == (Cause.TRANSFER, "deadline")


def test_a_programming_error_in_the_lookup_is_not_a_network_cause(trust):
    def broken(host, port, timeout):
        raise KeyError("a bug")

    with pytest.raises(KeyError):
        HttpTransport(trust=trust, lookup=broken).send(at(80), headers={},
                                                       deadline=time.monotonic() + 1)


def test_a_lookup_os_error_is_classified_too(trust):
    def unreachable(host, port, timeout):
        raise OSError(errno.ENETUNREACH, "network is unreachable")

    error = failure(HttpTransport(trust=trust, lookup=unreachable), at(80, "c.example"))
    assert (error.cause, error.reason) == (Cause.CONNECT, "unreachable")


def test_a_trickled_header_fails_at_the_exchange_deadline_not_per_read(trust):
    # Every recv() gets a byte within 0.2 s; a per-read timeout would wait forever.
    with trickle_peer(b"HTTP/1.1 200 OK\r\nX-Slow: ", b"a") as port:
        started = time.monotonic()
        error = failure(HttpTransport(trust=trust), at(port), seconds=TRICKLE_DEADLINE)
        elapsed = time.monotonic() - started
    assert (error.cause, error.reason) == (Cause.CONNECT, "timeout")
    assert TRICKLE_DEADLINE - 0.05 <= elapsed < TRICKLE_DEADLINE + TRICKLE_SLACK


def test_a_trickled_chunked_body_read_fails_at_its_timeout(trust):
    # A chunk-size line that never ends: read1 -> readline makes one recv() per byte.
    head = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
    with trickle_peer(head, b"0") as port:
        reply = HttpTransport(trust=trust).send(at(port), headers={},
                                                deadline=time.monotonic() + 5)
        try:
            started = time.monotonic()
            with pytest.raises(UplinkError) as caught:
                reply.read(10, timeout=TRICKLE_DEADLINE)
            elapsed = time.monotonic() - started
        finally:
            reply.close()
    assert (caught.value.cause, caught.value.reason) == (Cause.TRANSFER, "deadline")
    assert TRICKLE_DEADLINE - 0.05 <= elapsed < TRICKLE_DEADLINE + TRICKLE_SLACK


def test_the_exchange_deadline_holds_over_tls(trust):
    # The bound wraps the TLS socket, so it counts records, and TLS still verifies.
    with trickle_peer(b"HTTP/1.1 200 OK\r\nX-Slow: ", b"a", tls_server=True) as port:
        started = time.monotonic()
        error = failure(HttpTransport(trust=trust), at(port, scheme="https"),
                        seconds=TRICKLE_DEADLINE)
        elapsed = time.monotonic() - started
    assert (error.cause, error.reason) == (Cause.CONNECT, "timeout")
    assert TRICKLE_DEADLINE - 0.05 <= elapsed < TRICKLE_DEADLINE + TRICKLE_SLACK


# --- _DeadlineSocket: the zero-time-left guard and the close deferral -------------------------

@pytest.mark.parametrize("left", [0.0, -1.0], ids=["none-left", "overdue"])
def test_a_bounded_socket_with_no_time_left_raises_timeout_not_value_error(left):
    """With no time left, each send and receive is a TimeoutError before the socket is
    touched: a zero timeout would make the socket non-blocking (BlockingIOError) and a negative
    one is settimeout's ValueError, which no classify rule names."""
    ours, theirs = socket.socketpair()
    with ours, theirs:
        bounded = transport_module._DeadlineSocket(ours, deadline=100.0 + left,
                                                   monotonic=lambda: 100.0)
        for call in (lambda: bounded.recv(1), lambda: bounded.recv_into(bytearray(1)),
                     lambda: bounded.sendall(b"x")):
            with pytest.raises(TimeoutError):
                call()
        assert ours.gettimeout() is None            # never set: the guard came first


def test_a_deadline_that_passes_mid_exchange_is_a_named_timeout(trust, monkeypatch):
    """The whole exchange runs out between the TCP connect and the request: the bounded
    socket's first send finds no time left. That is CONNECT/timeout, never a ValueError that
    escapes classification."""
    skew = [0.0]
    real_connect = socket.socket.connect

    def slow_connect(sock: socket.socket, address) -> None:
        real_connect(sock, address)
        skew[0] += 60                               # far past this address's deadline

    monkeypatch.setattr(socket.socket, "connect", slow_connect)
    transport = HttpTransport(trust=trust, monotonic=lambda: time.monotonic() + skew[0])
    with raw_peer(hold=True) as port:
        error = failure(transport, at(port), seconds=30)
    assert (error.cause, error.reason) == (Cause.CONNECT, "timeout")


class _Closes:
    """A stand-in wrapped socket that counts close() calls."""

    def __init__(self) -> None:
        self.closes = 0

    def close(self) -> None:
        self.closes += 1


def _bounded(raw: _Closes) -> "transport_module._DeadlineSocket":
    return transport_module._DeadlineSocket(raw, deadline=float("inf"),  # type: ignore[arg-type]
                                            monotonic=time.monotonic)


def test_the_bounded_socket_closes_only_once_its_last_file_is_closed():
    raw = _Closes()
    bounded = _bounded(raw)
    first, second = bounded.makefile("rb"), bounded.makefile("rb")
    bounded.close()                                 # http.client hands the socket over...
    assert raw.closes == 0                          # ...while the response still reads it
    first.close()
    assert raw.closes == 0
    second.close()
    assert raw.closes == 1


def test_the_bounded_socket_closes_at_once_without_an_open_file():
    raw = _Closes()
    _bounded(raw).close()
    assert raw.closes == 1
    raw = _Closes()
    bounded = _bounded(raw)
    bounded.makefile("rb").close()                  # a file closed while the socket is open
    assert raw.closes == 0
    bounded.close()
    assert raw.closes == 1


def test_a_body_is_read_after_http_client_closes_its_connection(trust):
    """Connection: close makes http.client close the connection (so the bounded socket)
    before the response is read: the response's file keeps the socket open until it ends.
    The body trickles in after the head, so each read must reach the socket."""
    head = b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 2\r\n\r\n"
    with trickle_peer(head, b"o", every=0.05) as port:
        reply = HttpTransport(trust=trust).send(at(port), headers={},
                                                deadline=time.monotonic() + 5)
        try:
            body = b""
            while chunk := reply.read(10, timeout=5):
                body += chunk
        finally:
            reply.close()
    assert body == b"oo"
