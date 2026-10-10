"""DirectFetch: a direct request to the located origin only, refusing every 3xx, with
AppFetcher's bounds unchanged (the whole-acquisition deadline, the per-read timeout, the exact
Content-Length bound, identity encoding, truncation), plus the TLS record row over a real
socket."""

import contextlib
import http.server
import json
import os
import socket
import ssl
import threading
import time
from collections.abc import Iterator

import pytest
import tls_fixture as tls
from test_uplink_transport import TRICKLE_DEADLINE, TRICKLE_SLACK, trickle_peer
from uplink_fakes import FakeReply, FakeTransport, located

from contracts.read_through import READ_THROUGH_WAIT_SECONDS
from uplink.causes import Cause, UplinkError
from uplink.fetch import (
    MAX_ERROR_BODY,
    MAX_FETCH_SECONDS,
    READ_TIMEOUT,
    STATUS_TIMEOUT,
    DirectFetch,
    Refused,
    central_error_code,
    refusal,
    retry_after_seconds,
    stream,
)
from uplink.origin import Origin
from uplink.transport import HOP_TIMEOUT, HttpTransport
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


MANIFEST = Origin.parse_root("http://central").url("/v1/app/manifest")


def test_refusal_names_a_redirect_and_its_location_host():
    error = refusal(MANIFEST, 301, location="https://other.example/v1/app/manifest", body=b"")
    assert (error.cause, error.reason, error.host, error.detail) == (
        Cause.REDIRECT, "unexpected", "central", "status=301;location=other.example")


def test_refusal_resolves_a_relative_location_against_the_request():
    error = refusal(MANIFEST, 302, location="/elsewhere", body=b"")
    assert error.detail == "status=302;location=central"


def test_refusal_leaves_out_a_location_that_does_not_parse():
    # None and "" are not exercised here: parse_url(location or "", base=url) resolves an
    # absent/empty Location against the request itself, so it parses back to the same host
    # rather than failing to parse (see specWrong in the delivering report).
    error = refusal(MANIFEST, 307, location="ftp://x/", body=b"")
    assert error.detail == "status=307"


def test_refusal_names_central_own_error():
    error = refusal(MANIFEST, 503, location=None, body=b'{"error":"app_unconfigured"}')
    assert (error.cause, error.reason, error.central_error, error.detail) == (
        Cause.CENTRAL, "error", "app_unconfigured", "app_unconfigured")


@pytest.mark.parametrize("body", [
    b"<html>bad gateway</html>",
    b'{"error":"Not Valid"}',
    b'{"error":1}',
    b"[]",
    b"",
])
def test_refusal_names_any_other_answer_an_http_status(body):
    error = refusal(MANIFEST, 502, location=None, body=body)
    assert (error.cause, error.reason, error.detail, error.central_error) == (
        Cause.HTTP, "status", "status=502", None)


def test_central_error_code_refuses_an_oversized_body():
    padded = b'{"error":"app_unconfigured","pad":"' + b" " * MAX_ERROR_BODY + b'"}'
    assert central_error_code(padded) is None
    assert len(padded) > MAX_ERROR_BODY
    fits = b'{"error":"app_unconfigured"}'
    assert len(fits) <= MAX_ERROR_BODY
    assert central_error_code(fits) == "app_unconfigured"


def test_directfetch_refusal_is_the_shared_mapping():
    body = b'{"error":"app_unconfigured"}'
    error = refused(FakeReply(503, body=body))
    url = Origin.parse_root(ORIGIN).url("/v1/netboot/base")
    assert error.console() == refusal(url, 503, location=None, body=body).console()


@pytest.mark.parametrize(("value", "seconds"), [
    ("5", 5), (" 30 ", 30), ("0", 0), ("999999999", 999_999_999),
    (None, None), ("", None), ("\u00b2", None), ("\u0665", None), ("9" * 10, None),
    ("9" * 5000, None), ("-1", None), ("1.5", None), ("Wed, 21 Oct 2026 07:28:00 GMT", None),
])
def test_retry_after_is_one_to_nine_ascii_digits_else_absent(value, seconds):
    assert retry_after_seconds(value) == seconds


@pytest.mark.parametrize(("reply", "status", "retry_after", "code"), [
    (FakeReply(503, body=b'{"error":"app_timeout"}', headers={"Retry-After": "5"}),
     503, 5, "app_timeout"),
    (FakeReply(503, body=b'{"error":"content_unavailable"}'), 503, None, "content_unavailable"),
    (FakeReply(502, body=b"<html>bad gateway</html>", headers={"Retry-After": "\u00b2"}),
     502, None, None),
    (FakeReply(301, location="https://elsewhere.example/", headers={"Retry-After": "2"}),
     301, 2, None),
])
def test_a_refusal_carries_status_retry_after_and_central_code(reply, status, retry_after, code):
    error = refused(reply)
    assert isinstance(error, Refused)
    assert (error.status, error.retry_after, error.central_error) == (status, retry_after, code)


def test_stream_takes_a_deadline_beyond_the_directfetch_ceiling():
    clock = Clock(100.0)
    transport = FakeTransport({BASE: ok()})
    url = Origin.parse_root(ORIGIN).url("/v1/netboot/base")
    deadline = clock.now + MAX_FETCH_SECONDS + 200
    blocks = stream(transport, url, 10_000, block=64, deadline=deadline, monotonic=clock)
    first = next(blocks)
    clock.now += MAX_FETCH_SECONDS + 100  # past DirectFetch's ceiling, inside this deadline
    assert first + b"".join(blocks) == BODY
    assert transport.sent[0][0] == BASE


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
    # ...and may wait for Central's read-through wait before the status line (the transport
    # still caps it by the deadline)
    assert transport.sent[0][3] == STATUS_TIMEOUT > READ_THROUGH_WAIT_SECONDS
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


# --- Central holds a miss before its status line, over a real socket --------------------------

def test_central_answering_a_miss_after_more_than_a_hop_is_named_as_central(tmp_path):
    """Central answers a cache miss only after its read-through wait: the direct request waits
    for that answer instead of timing the status line out at one hop's bound."""
    body = json.dumps({"error": "base_timeout"}).encode()

    class SlowMiss(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            time.sleep(HOP_TIMEOUT + 0.5)
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    transport = HttpTransport(trust=Trust.public(tls.write_bundle(tmp_path / "ca.pem", tls.CA)))
    with tls.serve_stub(tls.central_stub(SlowMiss)) as stub:
        fetcher = DirectFetch(located(f"http://127.0.0.1:{stub.port}/"), transport=transport,
                              seconds=60)
        with pytest.raises(UplinkError) as caught:
            fetcher.get("/v1/netboot/base", 10_000)
    error = caught.value
    assert (error.cause, error.reason, error.central_error) == (
        Cause.CENTRAL, "error", "base_timeout")


# --- a trickling server, over a real socket: the acquisition deadline is absolute ------------

@pytest.mark.parametrize(("head", "byte", "expected"), [
    (b"HTTP/1.1 200 OK\r\nX-Slow: ", b"a", (Cause.CONNECT, "timeout")),     # inside the
    (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n", b"0",       # status wait
     (Cause.TRANSFER, "deadline")),                                            # chunked body
], ids=["headers", "chunked-body"])
def test_a_trickling_server_fails_at_the_acquisition_deadline(tmp_path, head, byte, expected):
    transport = HttpTransport(trust=Trust.public(tls.write_bundle(tmp_path / "ca.pem", tls.CA)))
    with trickle_peer(head, byte) as port:
        fetcher = DirectFetch(located(f"http://127.0.0.1:{port}/"), transport=transport,
                              seconds=TRICKLE_DEADLINE)
        started = time.monotonic()
        with pytest.raises(UplinkError) as caught:
            fetcher.get("/v1/netboot/base", 10_000)
        elapsed = time.monotonic() - started
    assert (caught.value.cause, caught.value.reason) == expected
    assert elapsed < TRICKLE_DEADLINE + TRICKLE_SLACK
