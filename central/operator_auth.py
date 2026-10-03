"""Who the operator is, at the HTTP boundary (pass A §5-§6).

One principal (the admin token), two ways to present it:

1. **Bearer decides alone.** A non-empty `Bearer` credential is compared with the token as bytes;
   the result is final (a mismatch is 401 even beside a valid cookie). Other schemes and an empty
   `Bearer` are ignored.
2. **Otherwise the session cookie**, verified by the total codec in `central.operator_session`.
   It is scoped to `Path=/v1/operator/` so other servers on the same host (cookies ignore the
   port) never receive it: `__Secure-photo_wall_session` over https, `photo_wall_session` over
   http. The first present name decides: `__Secure-`, then the legacy `__Host-`, then the plain
   one. Legacy `Path=/` cookies issued before the scoping are still ACCEPTED until they expire
   (at most `SESSION_SECONDS` after the scoped release), so nobody is signed out by the upgrade;
   every sign-in and log out clears them. A write it authorizes must be marked
   (`X-Photo-Wall-Console`), carry the exact Origin that signed in (a missing Origin is refused),
   and, when `Sec-Fetch-Site` is sent, say `same-origin`.

Every `/v1/operator/*` response is `Cache-Control: no-store`, including exception-handler and
unhandled-500 responses. Nothing here reads a cookie for Player, media or netboot routes, and no
CORS header is ever sent.

`OPERATOR_PREFIX` is the one declaration the cookie path, the no-store scope and the route scope
derive from: `OperatorAuth.require_scoped` refuses, at app construction, any route that depends on
`admin` outside it (a browser would never send the cookie there).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.dependencies.models import Dependant
from fastapi.responses import PlainTextResponse, Response
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from central.operator_session import SESSION_SECONDS, SessionCodec, origin_scheme
from central.registry import RegistryError
from contracts.models import Model
from contracts.time import Clock

OPERATOR_PREFIX = "/v1/operator/"
SESSION_PATH = "/v1/operator/session"
COOKIE = "photo_wall_session"  # over http
SECURE_COOKIE = "__Secure-" + COOKIE  # over https (`__Host-` would force Path=/)
COOKIE_PATH = OPERATOR_PREFIX
# Issued with Path=/ before the scoping; accepted until they expire, cleared at sign-in and log out.
LEGACY_HOST_COOKIE = "__Host-" + COOKIE
LEGACY_PATH = "/"
# The first name present decides: prefix-protected names before the plain one.
COOKIE_PRECEDENCE = (SECURE_COOKIE, LEGACY_HOST_COOKIE, COOKIE)
MARKER = "x-photo-wall-console"
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
NO_STORE = "no-store"

_BEARER = HTTPBearer(auto_error=False)


class SignIn(Model):
    token: str


def _set_cookie(name: str, value: str, path: str, max_age: int, secure: bool) -> str:
    secure_attr = " Secure;" if secure else ""
    return (f"{name}={value}; Path={path}; Max-Age={max_age};{secure_attr} HttpOnly; "
            "SameSite=Strict")


def issued_cookie(scheme: str, value: str) -> str:
    """The sign-in `Set-Cookie`: the name and `Secure` follow the sign-in Origin's scheme."""
    if scheme == "https":
        return _set_cookie(SECURE_COOKIE, value, COOKIE_PATH, SESSION_SECONDS, True)
    return _set_cookie(COOKIE, value, COOKIE_PATH, SESSION_SECONDS, False)


def legacy_clearing_cookies(secure_plain: bool) -> tuple[str, str]:
    """Clear the pre-scoping `Path=/` cookies (sent at sign-in and log out)."""
    return (
        _set_cookie(LEGACY_HOST_COOKIE, "", LEGACY_PATH, 0, True),
        _set_cookie(COOKIE, "", LEGACY_PATH, 0, secure_plain),
    )


def clearing_cookies(secure_plain: bool) -> tuple[str, ...]:
    """Log out's clearing `Set-Cookie`s: both scoped names, then both legacy ones. A plain one is
    `Secure` when logging out over https."""
    return (
        _set_cookie(SECURE_COOKIE, "", COOKIE_PATH, 0, True),
        _set_cookie(COOKIE, "", COOKIE_PATH, 0, secure_plain),
        *legacy_clearing_cookies(secure_plain),
    )


def _session_cookie(request: Request) -> str | None:
    cookies = request.cookies
    return next((cookies[name] for name in COOKIE_PRECEDENCE if name in cookies), None)


def _utf8(text: str) -> bytes | None:
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate from JSON can match no token
        return None


def _wire(text: str) -> bytes | None:
    # Starlette decodes header bytes as latin-1, so this recovers the bytes the client sent.
    try:
        return text.encode("latin-1")
    except UnicodeEncodeError:
        return None


def _marked(request: Request) -> None:
    if MARKER not in request.headers:
        raise RegistryError("request_unmarked", 403)


def _same_origin_fetch(request: Request) -> None:
    site = request.headers.get("sec-fetch-site")
    if site is not None and site != "same-origin":
        raise RegistryError("origin_mismatch", 403)


def _sign_in_origin(request: Request) -> str:
    """The sign-in gate, resolved before the body: marked, a bindable Origin, same-origin fetch."""
    _marked(request)
    origin = request.headers.get("origin")
    if origin_scheme(origin) is None:
        raise RegistryError("origin_mismatch", 403)
    _same_origin_fetch(request)
    return origin


class _PrefixHeaders:
    """Fixed headers on every response under one path prefix that this middleware sees."""

    def __init__(self, app: ASGIApp, *, prefix: str, headers: Mapping[str, str]):
        self.app, self.prefix, self.headers = app, prefix, dict(headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(self.prefix):
            await self.app(scope, receive, send)
            return

        async def send_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).update(self.headers)
            await send(message)

        await self.app(scope, receive, send_headers)


def add_prefix_headers(app: FastAPI, prefix: str, headers: Mapping[str, str]) -> None:
    """Put `headers` on every response under `prefix`: the user middleware for every answer a
    route or FastAPI builds, and the same table for the unhandled-500 handler, which Starlette's
    outermost ServerErrorMiddleware runs outside every user middleware (`OperatorAuth.install`).
    The one way a prefix gets fixed headers, so no answer under it can miss them."""
    table = getattr(app.state, "prefix_headers", None)
    if table is None:
        table = app.state.prefix_headers = []
    table.append((prefix, dict(headers)))
    app.add_middleware(_PrefixHeaders, prefix=prefix, headers=headers)


def _prefix_headers_for(app: Any, path: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for prefix, extra in getattr(app.state, "prefix_headers", ()):
        if path.startswith(prefix):
            headers.update(extra)
    return headers


def _depends_on(dependant: Dependant, call: object) -> bool:
    return any(sub.call == call or _depends_on(sub, call) for sub in dependant.dependencies)


class OperatorAuth:
    """The `admin` dependency and the sign-in routes, under one admin token."""

    def __init__(self, admin_token: str, clock: Clock):
        self.codec = SessionCodec(admin_token, clock)

    def admin(
        self,
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> None:
        # 1. Bearer decides alone (HTTPBearer yields None for other schemes and an empty Bearer).
        if credentials is not None and credentials.credentials:
            candidate = _wire(credentials.credentials)
            if candidate is None or not self.codec.token_matches(candidate):
                raise RegistryError("unauthorized", 401)
            return
        # 2. The cookie: the first present name in COOKIE_PRECEDENCE decides.
        session = self.codec.verify(_session_cookie(request))
        if session is None:
            raise RegistryError("unauthorized", 401)
        # 3. A write must come from the page that signed in.
        if request.method not in READ_METHODS:
            _marked(request)
            if request.headers.get("origin") != session.origin:
                raise RegistryError("origin_mismatch", 403)
            _same_origin_fetch(request)

    def mount(self, app: FastAPI) -> None:
        """Bind the two session routes, the no-store middleware and the unhandled-500 handler."""
        codec = self.codec

        @app.post(SESSION_PATH, status_code=204)
        def sign_in(body: SignIn, origin: str = Depends(_sign_in_origin)) -> Response:
            candidate = _utf8(body.token)
            if candidate is None or not codec.token_matches(candidate):
                raise RegistryError("unauthorized", 401)
            value = codec.mint(origin)
            if value is None:
                raise RegistryError("origin_mismatch", 403)
            scheme = origin_scheme(origin)
            response = Response(status_code=204)
            for header in (issued_cookie(scheme, value),
                           *legacy_clearing_cookies(scheme == "https")):
                response.headers.append("set-cookie", header)
            return response

        @app.delete(SESSION_PATH, status_code=204, dependencies=[Depends(_marked)])
        def sign_out(request: Request) -> Response:
            response = Response(status_code=204)
            secure_plain = origin_scheme(request.headers.get("origin")) == "https"
            for header in clearing_cookies(secure_plain):
                response.headers.append("set-cookie", header)
            return response

        add_prefix_headers(app, OPERATOR_PREFIX, {"Cache-Control": NO_STORE})

        # An unhandled exception is answered by Starlette's outermost ServerErrorMiddleware,
        # outside every user middleware; this handler is where that response is built, from
        # the same prefix table (`add_prefix_headers`).
        async def server_error(request: Request, exc: Exception) -> Response:
            headers = _prefix_headers_for(request.app, request.url.path) or None
            return PlainTextResponse("Internal Server Error", status_code=500, headers=headers)

        app.add_exception_handler(Exception, server_error)

    def require_scoped(self, app: FastAPI) -> None:
        """Refuse an app with a route that depends on `admin` outside `OPERATOR_PREFIX`, where the
        session cookie is never sent and no-store does not apply. Call once every route is bound."""
        stray = sorted(f"{','.join(sorted(route.methods))} {route.path}" for route in app.routes
                       if isinstance(route, APIRoute)
                       and not route.path.startswith(OPERATOR_PREFIX)
                       and _depends_on(route.dependant, self.admin))
        if stray:
            raise RuntimeError(f"operator routes outside {OPERATOR_PREFIX}: {stray}")
