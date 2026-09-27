"""The Player's link to Central (decision 0014, R3/R7/R8/R9): its httpx and websockets clients
are built from the process's one Trust, every URL comes from the LocatedCentral, no request
follows a redirect (locate is the only redirect follower), and every network failure of a direct
request is named as an UplinkError."""

from __future__ import annotations

from typing import Final

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedError, InvalidHandshake, InvalidStatus

from uplink.causes import Cause, Phase, UplinkError, classify
from uplink.fetch import MAX_ERROR_BODY, refusal
from uplink.locate import LocatedCentral
from uplink.origin import Url
from uplink.trust import Trust

REQUEST_TIMEOUT: Final = 15.0


def central_http_client(trust: Trust, *, connections: int, keepalive: int) -> httpx.AsyncClient:
    """verify=trust.context, follow_redirects=False, trust_env=False (no proxy, netrc or
    SSL_CERT_FILE from the environment), timeout=REQUEST_TIMEOUT,
    limits=httpx.Limits(max_connections=connections, max_keepalive_connections=keepalive).
    It has no base URL: every request names a URL built by central_url."""
    return httpx.AsyncClient(
        verify=trust.context, follow_redirects=False, trust_env=False, timeout=REQUEST_TIMEOUT,
        limits=httpx.Limits(max_connections=connections, max_keepalive_connections=keepalive))


def central_url(central: LocatedCentral, target: str) -> str:
    """str(central.origin.url(target)): the only way the Player builds an http(s) URL."""
    return str(central.origin.url(target))


def websocket_url(central: LocatedCentral, target: str) -> str:
    """ws:// for an http origin, wss:// for https; host and port from the origin."""
    scheme = "wss" if central.origin.scheme == "https" else "ws"
    return scheme + central_url(central, target)[len(central.origin.scheme):]


class DirectWebsocket(connect):
    """websockets' connect with redirects refused. Used with ssl=trust.context (wss) and
    proxy=None. A 3xx handshake answer is never followed: process_redirect hands websockets its
    own InvalidStatus back, so it is raised as is and Exchange.name turns it into
    refusal(...) (REDIRECT/unexpected). The bearer in additional_headers never leaves the
    located origin and locate stays the one redirect follower (R3)."""

    def process_redirect(self, exc: Exception) -> Exception | str:
        """Always `exc` itself, never a URI: websockets then raises it unchanged."""
        return exc


class Exchange:
    """Names the failures of one direct request (R9)."""

    __slots__ = ("_url", "_answered")

    def __init__(self, central: LocatedCentral, target: str) -> None:
        self._url = central.origin.url(target)
        self._answered = False

    @property
    def url(self) -> Url:
        """The request's URL: central.origin.url(target)."""
        return self._url

    @property
    def phase(self) -> Phase:
        """"connect" until answered(), then "transfer"."""
        return "transfer" if self._answered else "connect"

    def answered(self) -> None:
        """Call once the status line (httpx) or the handshake response (websocket) is in."""
        self._answered = True

    def name(self, error: BaseException) -> UplinkError | None:
        """UplinkError: unchanged. Else classify(error, phase=self.phase, host=<origin host>)
        when it names it. Else: httpx.TransportError -> CONNECT/protocol or TRANSFER/short by
        phase; websockets InvalidStatus -> refusal(url, response.status_code,
        location=response.headers.get("Location"), body=(response.body or b"")[:MAX_ERROR_BODY
        + 1]) (a 3xx is REDIRECT/unexpected); other websockets InvalidHandshake ->
        CONNECT/protocol; websockets ConnectionClosedError -> TRANSFER/short. None for anything
        else (the caller re-raises it unchanged: a programming error is never a network
        cause)."""
        if isinstance(error, UplinkError):
            return error
        host = self._url.origin.host
        named = classify(error, phase=self.phase, host=host)
        if named is not None:
            return named
        if isinstance(error, InvalidStatus):
            response = error.response
            return refusal(self._url, response.status_code,
                            location=response.headers.get("Location"),
                            body=(response.body or b"")[:MAX_ERROR_BODY + 1])
        if isinstance(error, InvalidHandshake):
            return UplinkError(Cause.CONNECT, "protocol", host=host, detail=type(error).__name__)
        if isinstance(error, ConnectionClosedError):
            return UplinkError(Cause.TRANSFER, "short", host=host, detail=type(error).__name__)
        if isinstance(error, httpx.TransportError):
            cause, reason = ((Cause.CONNECT, "protocol") if self.phase == "connect"
                             else (Cause.TRANSFER, "short"))
            return UplinkError(cause, reason, host=host, detail=type(error).__name__)
        return None


async def read_refusal(response: httpx.Response, url: Url) -> UplinkError:
    """Reads at most MAX_ERROR_BODY + 1 bytes of a non-200 (streamed) body, then
    refusal(url, response.status_code, location=response.headers.get("location"), body=...)."""
    body = bytearray()
    try:
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) >= MAX_ERROR_BODY + 1:
                break
    except (httpx.HTTPError, OSError):
        body = bytearray()
    return refusal(url, response.status_code, location=response.headers.get("location"),
                    body=bytes(body[:MAX_ERROR_BODY + 1]))
