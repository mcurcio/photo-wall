"""HttpTransport and lookup over real sockets: peers that do not speak HTTP, address fallback,
and every wait bounded and named by what it was waiting for."""

import contextlib
import errno
import socket
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
def raw_peer(answer: bytes = b"", *, hold: bool = False) -> Iterator[int]:
    """A TCP peer on 127.0.0.1 that reads one request head, sends `answer`, then either holds
    the connection open until the test ends (`hold`) or closes its side cleanly. Yields its
    port."""
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


def closed_port() -> int:
    with socket.create_server(("127.0.0.1", 0)) as listener:
        return listener.getsockname()[1]


@pytest.fixture
def trust(tmp_path) -> Trust:
    return Trust.public(tls.write_bundle(tmp_path / "ca.pem", tls.CA))


def at(port: int, host: str = "127.0.0.1") -> Url:
    return Url(Origin("http", host, port), "/v1/locate")


def failure(transport: HttpTransport, url: Url, *, seconds: float = 5.0) -> UplinkError:
    with pytest.raises(UplinkError) as caught:
        transport.send(url, headers={}, deadline=time.monotonic() + seconds)
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


def test_each_hop_is_bounded_even_when_the_deadline_is_far(trust, monkeypatch):
    monkeypatch.setattr(transport_module, "HOP_TIMEOUT", 0.3)
    with raw_peer(hold=True) as port:
        started = time.monotonic()
        error = failure(HttpTransport(trust=trust), at(port), seconds=30)
    assert (error.cause, error.reason) == (Cause.CONNECT, "timeout")
    assert time.monotonic() - started < 3


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
