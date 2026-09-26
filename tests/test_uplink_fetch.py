"""DirectFetch: a direct request to the located origin only, refusing every 3xx, with
AppFetcher's bounds unchanged (the whole-acquisition deadline, the per-read timeout, the exact
Content-Length bound, identity encoding, truncation), plus the TLS record row over a real
socket."""

import contextlib
import os
import socket
import ssl
import threading
from collections.abc import Iterator

import pytest

from tests import tls_fixture as tls
from tests.uplink_fakes import FakeReply, FakeTransport, located
from uplink.causes import Cause, UplinkError
from uplink.fetch import MAX_ERROR_BODY, MAX_FETCH_SECONDS, READ_TIMEOUT, DirectFetch
from uplink.transport import HttpTransport
from uplink.trust import Trust

ORIGIN = "https://photo-wall.example/"
BASE = "https://photo-wall.example/v1/netboot/base"
BODY = b"base squashfs bytes " * 50


class Clock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def fetch(reply: FakeReply | UplinkError, *, seconds: float = 300, clock=None,
          ) -> tuple[DirectFetch, FakeTransport]:
    transport = FakeTransport({BASE: reply})
    kwargs = {} if clock is None else {"monotonic": clock}
    return DirectFetch(located(ORIGIN), transport=transport, seconds=seconds, **kwargs), transport


def ok(body: bytes = BODY, **kwargs) -> FakeReply:
    headers = {"Content-Length": str(len(body)), **kwargs.pop("headers", {})}
    return FakeReply(200, body=body, headers=headers, **kwargs)


def refused(reply, *, maximum: int = 10_000, block: int = 64, **kwargs) -> UplinkError:
    fetcher, _ = fetch(reply, **kwargs)
    with pytest.raises(UplinkError) as caught:
        list(fetcher.chunks("/v1/netboot/base", maximum, block=block))
    return caught.value


def test_a_200_streams_the_body_in_bounded_non_empty_blocks():
    reply = ok(step=1000)
    fetcher, transport = fetch(reply)
    blocks = list(fetcher.chunks("/v1/netboot/base", 10_000, block=64,
                                 headers={"X-PhotoWall-Serial": "10000000abcd"}))
    assert b"".join(blocks) == BODY
    assert all(0 < len(block) <= 64 for block in blocks)
    assert transport.sent[0][:2] == (BASE, {"Accept-Encoding": "identity",
                                            "X-PhotoWall-Serial": "10000000abcd"})
    assert reply.closed


def test_on_response_sees_the_headers_before_the_body():
    seen = []
    fetcher, _ = fetch(ok(headers={"Digest": "sha-256=x"}))
    blocks = fetcher.chunks("/v1/netboot/base", 10_000, block=64,
                            on_response=lambda headers: seen.append(headers["Digest"]))
    next(blocks)
    assert seen == ["sha-256=x"]


def test_get_returns_the_whole_body():
    fetcher, _ = fetch(ok())
    assert fetcher.get("/v1/netboot/base", 10_000) == BODY


def test_only_a_located_central_is_accepted():
    with pytest.raises(TypeError):
        DirectFetch(ORIGIN, transport=FakeTransport({}), seconds=10)


@pytest.mark.parametrize("seconds", [0, -1, MAX_FETCH_SECONDS + 1])
def test_the_acquisition_deadline_is_bounded(seconds):
    with pytest.raises(ValueError):
        DirectFetch(located(ORIGIN), transport=FakeTransport({}), seconds=seconds)


@pytest.mark.parametrize(("maximum", "block"), [(0, 64), (10, 0), (True, 64), (10, 1.5)])
def test_the_bounds_are_positive_ints(maximum, block):
    fetcher, _ = fetch(ok())
    with pytest.raises(ValueError):
        list(fetcher.chunks("/v1/netboot/base", maximum, block=block))


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_every_redirect_on_a_direct_request_is_refused(status):
    fetcher, transport = fetch(FakeReply(status, location="https://elsewhere.example/v1/netboot/base"))
    with pytest.raises(UplinkError) as caught:
        list(fetcher.chunks("/v1/netboot/base", 10_000, block=64))
    error = caught.value
    assert (error.cause, error.reason, error.host) == (
        Cause.REDIRECT, "unexpected", "photo-wall.example")
    assert error.detail == f"status={status};location=elsewhere.example"
    assert [sent[0] for sent in transport.sent] == [BASE]   # nothing sent to the Location


def test_a_redirect_whose_location_does_not_parse_names_only_the_status():
    error = refused(FakeReply(307, location="ftp://elsewhere/"))
    assert (error.reason, error.detail) == ("unexpected", "status=307")


def test_central_own_error_is_named_with_its_code():
    error = refused(FakeReply(503, body=b'{"error":"base_artifact_pending"}'))
    assert (error.cause, error.reason, error.central_error, error.detail) == (
        Cause.CENTRAL, "error", "base_artifact_pending", "base_artifact_pending")


@pytest.mark.parametrize("body", [
    b"<html>503 Service Unavailable</html>",
    b'{"error":"Not A Code"}',
    b'{"detail":"Not Found"}',
    b'{"error":"a","error":"b"}',
    b'{"error":"base_unknown","pad":"' + b"x" * MAX_ERROR_BODY + b'"}',
])
def test_a_non_200_without_central_body_is_an_http_status(body):
    error = refused(FakeReply(503, body=body))
    assert (error.cause, error.reason, error.detail, error.central_error) == (
        Cause.HTTP, "status", "status=503", None)


def test_an_error_body_that_cannot_be_read_leaves_the_status():
    error = refused(FakeReply(502, fail=UplinkError(Cause.TRANSFER, "deadline")))
    assert (error.cause, error.reason) == (Cause.HTTP, "status")


@pytest.mark.parametrize(("reply", "maximum", "reason"), [
    (ok(), len(BODY) - 1, "limit"),                                   # declared over the bound
    (FakeReply(200, body=BODY), len(BODY) - 1, "limit"),              # streamed over the bound
    (ok(headers={"Content-Length": "12x"}), 10_000, "limit"),
    (ok(headers={"Content-Length": str(len(BODY) + 1)}), 10_000, "short"),
    (ok(headers={"Content-Length": "0"}), 10_000, "short"),
    (FakeReply(200), 10_000, "short"),                                # nothing at all
    (ok(headers={"Content-Encoding": "gzip"}), 10_000, "encoding"),
    (ok(fail=UplinkError(Cause.TRANSFER, "tls", detail="BAD_RECORD_MAC"),
        headers={"Content-Length": str(len(BODY) + 1)}), 10_000, "tls"),
    (ok(fail=UplinkError(Cause.TRANSFER, "short"),
        headers={"Content-Length": str(len(BODY) + 1)}), 10_000, "short"),
])
def test_transfer_failures_are_named(reply, maximum, reason):
    error = refused(reply, maximum=maximum)
    assert (error.cause, error.reason) == (Cause.TRANSFER, reason)
    assert reply.closed


def test_the_body_stops_at_its_declared_length():
    reply = ok()
    reply.body += b"trailing"
    fetcher, _ = fetch(reply)
    assert fetcher.get("/v1/netboot/base", 10_000) == BODY


def test_each_read_waits_at_most_ten_seconds_or_what_is_left():
    clock = Clock(100.0)
    reply = ok(step=len(BODY) // 2 + 1)
    fetcher, _ = fetch(reply, seconds=15, clock=clock)
    blocks = fetcher.chunks("/v1/netboot/base", 10_000, block=len(BODY))
    next(blocks)
    clock.now += 9.0
    list(blocks)
    assert [timeout for _, timeout in reply.reads] == [READ_TIMEOUT, pytest.approx(6.0)]


def test_one_deadline_bounds_the_whole_acquisition():
    clock = Clock(100.0)
    fetcher, transport = fetch(ok(step=10), seconds=30, clock=clock)
    blocks = fetcher.chunks("/v1/netboot/base", 10_000, block=10)
    next(blocks)
    assert transport.sent[0][2] == 130.0            # the send shares the deadline
    clock.now = 130.0
    with pytest.raises(UplinkError) as caught:
        list(blocks)
    assert (caught.value.cause, caught.value.reason) == (Cause.TRANSFER, "deadline")


def test_a_deadline_already_past_sends_nothing():
    clock = Clock(100.0)
    fetcher, transport = fetch(ok(), seconds=1, clock=clock)
    clock.now = 101.0
    with pytest.raises(UplinkError) as caught:
        list(fetcher.chunks("/v1/netboot/base", 10_000, block=64))
    assert caught.value.reason == "deadline" and transport.sent == []


def test_a_transport_failure_propagates_unchanged():
    failure = UplinkError(Cause.CONNECT, "refused", host="photo-wall.example")
    fetcher, _ = fetch(failure)
    with pytest.raises(UplinkError) as caught:
        list(fetcher.chunks("/v1/netboot/base", 10_000, block=64))
    assert caught.value is failure


# --- a TLS record error mid-body, over a real socket -----------------------------------------

@contextlib.contextmanager
def broken_tls_body(head: bytes) -> Iterator[int]:
    """A TLS server that sends `head` (headers and part of a body), then a forged application
    data record the client cannot decrypt."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    listener = socket.create_server(("127.0.0.1", 0))

    def serve() -> None:
        with contextlib.suppress(OSError), tls.CENTRAL.files() as files:
            context.load_cert_chain(*files)
            connection, _ = listener.accept()
            with context.wrap_socket(connection, server_side=True) as stream:
                stream.recv(65536)
                stream.sendall(head)
                # A record header for 32 bytes of application data, then noise under it.
                os.write(stream.fileno(), b"\x17\x03\x03\x00\x20" + b"\x00" * 32)
                stream.recv(1)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()
        thread.join(timeout=10)


def test_a_tls_record_error_mid_body_is_a_transfer_tls_failure(tmp_path):
    transport = HttpTransport(trust=Trust.public(tls.write_bundle(tmp_path / "ca.pem", tls.CA)))
    head = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n" + b"x" * 10
    with broken_tls_body(head) as port:
        fetcher = DirectFetch(located(f"https://127.0.0.1:{port}/"), transport=transport,
                              seconds=10)
        blocks = []
        with pytest.raises(UplinkError) as caught:
            for block in fetcher.chunks("/v1/netboot/base", 1000, block=64):
                blocks.append(block)
    assert (caught.value.cause, caught.value.reason) == (Cause.TRANSFER, "tls")
    assert b"".join(blocks) == b"x" * 10
