"""The Player's link to Central (decision 0014, R3/R7/R8/R9, U8): its httpx and websockets
clients are built from the process's one Trust, every URL comes from the LocatedCentral, no
request follows a redirect (locate is the only redirect follower), and every network failure of
a direct request is named as an UplinkError.

CentralLink is the only thing in the Player that opens a request to Central, and a Session is
the only source of its bearer: the bearer goes to the origin that issued it or nowhere."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedError, InvalidHandshake, InvalidStatus

from contracts.liveness import REQUEST_TIMEOUT
from uplink.causes import Cause, Phase, UplinkError, classify
from uplink.fetch import MAX_ERROR_BODY, refusal
from uplink.locate import LocatedCentral
from uplink.origin import Url, parse_url
from uplink.trust import Trust


async def refuse_redirect(response: httpx.Response) -> None:
    """The response hook on every client CentralLink uses: any 3xx is refused as
    refusal(...) (REDIRECT/unexpected) before httpx could follow it, whatever the client's or
    the request's follow_redirects says. Only locate follows redirects (R3)."""
    if 300 <= response.status_code < 400:
        url = parse_url(str(response.request.url))
        if url is None:
            raise UplinkError(Cause.REDIRECT, "unexpected",
                              detail=f"status={response.status_code}")
        raise await read_refusal(response, url)


def guarded(client: httpx.AsyncClient) -> httpx.AsyncClient:
    """`client` with refuse_redirect installed as a response hook (once)."""
    hooks = client.event_hooks
    if refuse_redirect not in hooks["response"]:
        client.event_hooks = {**hooks, "response": [*hooks["response"], refuse_redirect]}
    return client


def central_http_client(trust: Trust, *, connections: int, keepalive: int) -> httpx.AsyncClient:
    """verify=trust.context, follow_redirects=False, trust_env=False (no proxy, netrc or
    SSL_CERT_FILE from the environment), timeout=REQUEST_TIMEOUT,
    limits=httpx.Limits(max_connections=connections, max_keepalive_connections=keepalive),
    and the refuse_redirect response hook. It has no base URL: every request names a URL built
    by central_url."""
    return guarded(httpx.AsyncClient(
        verify=trust.context, follow_redirects=False, trust_env=False, timeout=REQUEST_TIMEOUT,
        limits=httpx.Limits(max_connections=connections, max_keepalive_connections=keepalive)))


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


class Credential(Protocol):
    """What a Session needs of a registration: its bearer token."""

    @property
    def token(self) -> str: ...


@dataclass(frozen=True, slots=True)
class Session:
    """A located Central and at most the registration that origin issued (U8, R8).

    The constructor takes no registration: one joins a Session only through `enrolled`, after
    enrolling at `central.origin`, and survives a new locate only through `relocated`, which
    keeps it when the origin is unchanged and drops it otherwise (so the Player re-enrolls,
    silently, at the new origin). Scheme is part of the origin, so https -> http drops it too.
    CentralLink reads the bearer from here and nowhere else, so it is only ever sent to the
    origin that issued it.

    Cost: an origin change (a moved Central, a changed port or scheme) always costs one
    re-enrollment and an authority-epoch bump, even when it is the same Central."""

    central: LocatedCentral
    registration: Credential | None = field(default=None, init=False)

    def enrolled(self, registration: Credential) -> Session:
        """This Session with `registration`, which this Session's origin issued."""
        session = Session(self.central)
        object.__setattr__(session, "registration", registration)
        return session

    def unregistered(self) -> Session:
        """This Session with no registration (Central refused it, or enrollment failed)."""
        return Session(self.central)

    def relocated(self, central: LocatedCentral) -> Session:
        """A Session for a new locate: the registration is kept only if `central` has the same
        origin (scheme, host, port) as this one."""
        if self.registration is None or central.origin != self.central.origin:
            return Session(central)
        return Session(central).enrolled(self.registration)


class CentralLink:
    """The Player's one link to Central: it owns the httpx clients (each guarded by
    refuse_redirect), the websocket factory (ssl from the one Trust for wss, proxy=None,
    DirectWebsocket refusing redirects), the bearer (from the Session only), and failure naming
    (Exchange). Callers name a Session and a target; they cannot pass a URL, follow_redirects,
    ssl or an Authorization header.

    `unauthorized(code)` builds the caller's exception for a 401 answer or an authenticated
    request on a Session with no registration. `client`/`time_client`/`websocket_connect` may
    be injected (tests, fixtures); any client given is guarded too. `websocket_connect=False`
    is the caller's own "no websocket" switch and is kept as given."""

    WEBSOCKET_OPTIONS: Final = dict(max_queue=4, compression=None, proxy=None, open_timeout=15,
                                    close_timeout=3, ping_interval=10, ping_timeout=10)

    def __init__(self, trust: Trust, *, unauthorized: Callable[[str], Exception],
                 client: httpx.AsyncClient | None = None,
                 time_client: httpx.AsyncClient | None = None,
                 websocket_connect: Any = None) -> None:
        self.trust = trust
        self._unauthorized = unauthorized
        self._client = self._time_client = None
        self._owned: list[httpx.AsyncClient] = []
        self.client, self.time_client = client, time_client
        self.websocket_connect = websocket_connect

    @property
    def client(self) -> httpx.AsyncClient | None:
        return self._client

    @client.setter
    def client(self, client: httpx.AsyncClient | None) -> None:
        self._client = None if client is None else guarded(client)

    @property
    def time_client(self) -> httpx.AsyncClient | None:
        return self._time_client

    @time_client.setter
    def time_client(self, client: httpx.AsyncClient | None) -> None:
        self._time_client = None if client is None else guarded(client)

    def open(self) -> None:
        """Builds, from the one Trust, each client that was not injected: the request client
        (4 connections, 2 keep-alive) and the clock-probe client (1, 1)."""
        if self._client is None:
            self._client = central_http_client(self.trust, connections=4, keepalive=2)
            self._owned.append(self._client)
        if self._time_client is None:
            self._time_client = central_http_client(self.trust, connections=1, keepalive=1)
            self._owned.append(self._time_client)

    async def aclose(self) -> None:
        """Closes the clients open() built; injected ones belong to their owner."""
        owned, self._owned = self._owned, []
        for client in owned:
            await client.aclose()

    def _headers(self, session: Session, authenticated: bool) -> dict[str, str]:
        headers = {"Accept-Encoding": "identity"}
        if authenticated:
            if session.registration is None:
                raise self._unauthorized("not_registered")
            headers["Authorization"] = "Bearer " + session.registration.token
        return headers

    def _named(self, exchange: Exchange, error: Exception) -> Exception:
        named = exchange.name(error)
        return error if named is None else named

    @contextlib.asynccontextmanager
    async def stream(self, session: Session, method: str, target: str, *, body: Any = None,
                     authenticated: bool = True, timeout: float = REQUEST_TIMEOUT,
                     time: bool = False) -> AsyncIterator[httpx.Response]:
        """One streamed request to session.central.origin.url(target), bounded by `timeout`
        for the whole exchange including the caller's body reads. Yields the answered response
        (a 3xx never gets here; a 401 is `unauthorized("unauthorized")`). `time` selects the
        clock-probe client. Every failure inside, the caller's included, is named by Exchange;
        what it does not name is re-raised unchanged."""
        client = (self._time_client if time else None) or self._client
        if client is None:
            raise RuntimeError("CentralLink is not open")
        exchange = Exchange(session.central, target)
        try:
            headers = self._headers(session, authenticated)
            async with asyncio.timeout(timeout):
                async with client.stream(method, str(exchange.url), json=body,
                                         headers=headers) as response:
                    exchange.answered()
                    if response.status_code == 401:
                        raise self._unauthorized("unauthorized")
                    yield response
        except Exception as error:
            named = self._named(exchange, error)
            if named is error:
                raise
            raise named from error

    @contextlib.asynccontextmanager
    async def websocket(self, session: Session, target: str, *,
                        max_size: int) -> AsyncIterator[Any]:
        """The authenticated session websocket at session.central's origin (ws/wss by scheme),
        with ssl=trust.context for wss and WEBSOCKET_OPTIONS. A 401 handshake is
        `unauthorized("unauthorized")`; every other failure is named by Exchange."""
        exchange = Exchange(session.central, target)
        try:
            options = dict(self.WEBSOCKET_OPTIONS, max_size=max_size,
                           additional_headers=self._headers(session, True))
            if session.central.origin.scheme == "https":
                options["ssl"] = self.trust.context
            connector = self.websocket_connect or DirectWebsocket
            async with connector(websocket_url(session.central, target), **options) as socket:
                exchange.answered()
                yield socket
        except Exception as error:
            if isinstance(error, InvalidStatus) and error.response.status_code == 401:
                raise self._unauthorized("unauthorized") from error
            named = self._named(exchange, error)
            if named is error:
                raise
            raise named from error
