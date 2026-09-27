"""player/central_link.py (decision 0014, R3/R7/R8/R9): the one Trust builds the httpx client,
every URL comes from the LocatedCentral, DirectWebsocket refuses a handshake redirect (the
bearer never leaves the located origin), and Exchange names every network failure."""

import asyncio
import http.server
import ssl

import httpx
import pytest
from websockets.asyncio.client import connect as library_connect
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidHandshake, InvalidStatus
from websockets.http11 import Response as Http11Response

from player.central_link import (
    CentralLink,
    DirectWebsocket,
    Exchange,
    Session,
    central_http_client,
    central_url,
    read_refusal,
    websocket_url,
)
from tests import tls_fixture as tls
from tests.uplink_fakes import located
from uplink.causes import Cause, UplinkError
from uplink.trust import Trust


def _trust(tmp_path) -> Trust:
    return Trust.public(tls.write_bundle(tmp_path / "ca.pem", tls.CA))


def test_the_client_is_built_from_the_one_trust(tmp_path):
    async def check():
        trust = _trust(tmp_path)
        client = central_http_client(trust, connections=4, keepalive=2)
        try:
            pool = client._transport._pool
            assert pool._ssl_context is trust.context
            assert client.trust_env is False
            assert client.follow_redirects is False
            assert client.timeout == httpx.Timeout(15.0)
            assert pool._max_connections == 4
            assert pool._max_keepalive_connections == 2
        finally:
            await client.aclose()
    asyncio.run(check())


@pytest.mark.parametrize("origin,http_url,ws_url", [
    ("http://central", "http://central/v1/player/state", "ws://central/v1/player/session"),
    ("https://central.example:8443", "https://central.example:8443/v1/player/state",
     "wss://central.example:8443/v1/player/session"),
])
def test_urls_come_from_the_located_origin(origin, http_url, ws_url):
    central = located(origin)
    assert central_url(central, "/v1/player/state") == http_url
    assert websocket_url(central, "/v1/player/session") == ws_url


def test_an_exchange_is_in_connect_until_answered():
    central = located("http://central")
    exchange = Exchange(central, "/v1/player/state")
    assert exchange.phase == "connect"
    assert exchange.url == central.origin.url("/v1/player/state")
    exchange.answered()
    assert exchange.phase == "transfer"


def _connect_error_from_refused() -> httpx.ConnectError:
    try:
        raise httpx.ConnectError("x") from ConnectionRefusedError()
    except httpx.ConnectError as error:
        return error


def _verify_error(code: int) -> ssl.SSLCertVerificationError:
    error = ssl.SSLCertVerificationError(1, "x")
    error.verify_code = code
    return error


_FAILURE_TABLE = [
    (ConnectionRefusedError(), "connect", (Cause.CONNECT, "refused")),
    (TimeoutError(), "connect", (Cause.CONNECT, "timeout")),
    (TimeoutError(), "transfer", (Cause.TRANSFER, "deadline")),
    (httpx.RemoteProtocolError("x"), "connect", (Cause.CONNECT, "protocol")),
    (httpx.RemoteProtocolError("x"), "transfer", (Cause.TRANSFER, "short")),
    (_connect_error_from_refused(), "connect", (Cause.CONNECT, "refused")),
    (_verify_error(20), "connect", (Cause.TLS, "untrusted")),
    (_verify_error(9), "connect", (Cause.TIME, "not_yet_valid")),
    (ConnectionClosedError(None, None), "transfer", (Cause.TRANSFER, "short")),
    (InvalidHandshake("x"), "connect", (Cause.CONNECT, "protocol")),
]


@pytest.mark.parametrize("error,phase,expected", _FAILURE_TABLE)
def test_exchange_names_network_failures_by_phase(error, phase, expected):
    central = located("http://central.example")
    exchange = Exchange(central, "/v1/player/state")
    if phase == "transfer":
        exchange.answered()
    named = exchange.name(error)
    assert (named.cause, named.reason) == expected
    assert named.host == "central.example"


def test_exchange_passes_an_uplink_error_through_and_leaves_other_errors():
    central = located("http://central")
    exchange = Exchange(central, "/v1/player/state")
    original = UplinkError(Cause.HTTP, "status", host="central")
    assert exchange.name(original) is original
    assert exchange.name(ValueError("x")) is None


def test_a_websocket_handshake_status_is_the_shared_refusal():
    central = located("http://central")
    exchange = Exchange(central, "/v1/player/session")

    redirect = InvalidStatus(Http11Response(
        301, "Moved", Headers({"Location": "https://other.example/v1/player/session"}), b""))
    named = exchange.name(redirect)
    assert (named.cause, named.reason) == (Cause.REDIRECT, "unexpected")
    assert named.detail == "status=301;location=other.example"

    refused = InvalidStatus(Http11Response(
        503, "Service Unavailable", Headers({}), b'{"error":"app_unconfigured"}'))
    named = exchange.name(refused)
    assert (named.cause, named.reason) == (Cause.CENTRAL, "error")
    assert named.central_error == "app_unconfigured"


class _NotFound(http.server.BaseHTTPRequestHandler):
    # websockets' own HTTP/1.1 response parser (unlike uplink's) refuses an HTTP/1.0 status
    # line outright, before a status is even seen: BaseHTTPRequestHandler's own default would
    # make every case below fail the same way, hiding the one this test is about.
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


def _redirect_handler(location: str, *,
                      status: int = 301) -> type[http.server.BaseHTTPRequestHandler]:
    """A stand-in gateway that answers every GET with `status` and `Location: location`, over
    HTTP/1.1 (tests.tls_fixture.redirect_stub's stand-in is HTTP/1.0 only, which websockets'
    strict response parser refuses outright -- see _NotFound above). `location` is
    protocol-relative ("//host:port/path") so it resolves against a "ws://" base the same as it
    resolves against this gateway's "http://" origin: one Location, meaningful to both the
    websocket client's own redirect-follower and to uplink.fetch.refusal's Location parser."""

    class Redirect(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self.send_response(status)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args) -> None:
            pass

    return Redirect


def test_a_websocket_redirect_is_refused_and_the_bearer_never_leaves():
    async def check():
        with tls.serve_stub(_NotFound) as target:
            location = f"//127.0.0.1:{target.port}/v1/player/session"
            with tls.serve_stub(_redirect_handler(location)) as gateway:
                central = located(f"http://127.0.0.1:{gateway.port}")
                exchange = Exchange(central, "/v1/player/session")
                url = websocket_url(central, "/v1/player/session")
                with pytest.raises(Exception) as excinfo:
                    async with DirectWebsocket(
                            url, additional_headers={"Authorization": "Bearer " + "x" * 32},
                            proxy=None, open_timeout=5):
                        pass
                named = exchange.name(excinfo.value)
                assert (named.cause, named.reason) == (Cause.REDIRECT, "unexpected")
                assert ";location=127.0.0.1" in named.detail
                assert gateway.requests == ["/v1/player/session"]
                assert target.requests == []
    asyncio.run(check())


def test_the_library_default_would_follow_that_redirect():
    async def check():
        with tls.serve_stub(_NotFound) as target:
            location = f"//127.0.0.1:{target.port}/v1/player/session"
            with tls.serve_stub(_redirect_handler(location)) as gateway:
                central = located(f"http://127.0.0.1:{gateway.port}")
                url = websocket_url(central, "/v1/player/session")
                with pytest.raises(Exception):
                    async with library_connect(
                            url, additional_headers={"Authorization": "Bearer " + "x" * 32},
                            proxy=None, open_timeout=5):
                        pass
                assert target.requests == ["/v1/player/session"]
    asyncio.run(check())


async def _read(handler, url):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    try:
        async with client.stream("GET", str(url)) as response:
            return await read_refusal(response, url)
    finally:
        await client.aclose()


def test_read_refusal_reads_a_bounded_body():
    async def check():
        central = located("http://central")
        url = central.origin.url("/v1/player/state")

        def redirect(request):
            return httpx.Response(301, headers={"Location": "https://other.example/x"})
        named = await _read(redirect, url)
        assert (named.cause, named.reason) == (Cause.REDIRECT, "unexpected")

        def central_error(request):
            return httpx.Response(503, json={"error": "app_unconfigured"})
        named = await _read(central_error, url)
        assert (named.cause, named.reason) == (Cause.CENTRAL, "error")
        assert named.central_error == "app_unconfigured"

        def html(request):
            return httpx.Response(502, headers={"Content-Type": "text/html"},
                                  content=b"<html></html>")
        named = await _read(html, url)
        assert (named.cause, named.reason) == (Cause.HTTP, "status")
        assert named.detail == "status=502"

        def oversized(request):
            body = b'{"error":"x"' + b"a" * (10 * 1024)
            return httpx.Response(503, content=body)
        named = await _read(oversized, url)
        assert (named.cause, named.reason) == (Cause.HTTP, "status")
        assert named.detail == "status=503"
    asyncio.run(check())


# --- R3: the redirect guard, for every 3xx and on every client --------------------------------

class _Refused(Exception):
    pass


def _moving(status: int, calls: list[str]):
    """Central answers `status` with a Location elsewhere; anywhere else answers 200."""
    def handle(request):
        calls.append(str(request.url))
        if request.url.host == "central":
            return httpx.Response(status, headers={"Location": "http://upstream.example/copy"})
        return httpx.Response(200, json={})
    return handle


async def _get(link: CentralLink, *, time: bool = False) -> int:
    async with link.stream(Session(located("http://central")), "GET", "/v1/player/time",
                           authenticated=False, time=time) as response:
        return response.status_code


@pytest.mark.parametrize("status", [300, 301, 302, 303, 307, 308, 399])
def test_every_3xx_is_refused_before_a_client_that_follows_redirects_could(tmp_path, status):
    async def check():
        calls = []
        client = httpx.AsyncClient(follow_redirects=True,
                                   transport=httpx.MockTransport(_moving(status, calls)))
        link = CentralLink(_trust(tmp_path), unauthorized=_Refused, client=client)
        try:
            with pytest.raises(UplinkError) as excinfo:
                await _get(link)
        finally:
            await client.aclose()
        assert (excinfo.value.cause, excinfo.value.reason) == (Cause.REDIRECT, "unexpected")
        assert excinfo.value.detail == f"status={status};location=upstream.example"
        assert calls == ["http://central/v1/player/time"]
    asyncio.run(check())


@pytest.mark.parametrize("status", [200, 299, 400, 503])
def test_a_status_outside_3xx_is_left_to_the_caller(tmp_path, status):
    async def check():
        calls = []
        client = httpx.AsyncClient(transport=httpx.MockTransport(_moving(status, calls)))
        link = CentralLink(_trust(tmp_path), unauthorized=_Refused, client=client)
        try:
            assert await _get(link) == status
        finally:
            await client.aclose()
    asyncio.run(check())


@pytest.mark.parametrize("how", ["constructor", "setter"])
def test_an_injected_time_client_is_guarded_too(tmp_path, how):
    """The clock probe's own client (distinct from the request client) never follows a
    redirect either, whether it is injected at construction or set later."""
    async def check():
        calls = []
        client = httpx.AsyncClient(transport=httpx.MockTransport(_moving(200, [])))
        time_client = httpx.AsyncClient(follow_redirects=True,
                                        transport=httpx.MockTransport(_moving(302, calls)))
        link = CentralLink(_trust(tmp_path), unauthorized=_Refused, client=client,
                           time_client=time_client if how == "constructor" else None)
        if how == "setter":
            link.time_client = time_client
        try:
            assert link.time_client is time_client
            with pytest.raises(UplinkError) as excinfo:
                await _get(link, time=True)
        finally:
            await client.aclose()
            await time_client.aclose()
        assert (excinfo.value.cause, excinfo.value.reason) == (Cause.REDIRECT, "unexpected")
        assert calls == ["http://central/v1/player/time"]
    asyncio.run(check())
