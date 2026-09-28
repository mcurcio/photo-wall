"""Who the operator is, at the HTTP boundary (pass A §5-§6).

One principal (the admin token), two ways to present it:

1. **Bearer decides alone.** A non-empty `Bearer` credential is compared with the token as bytes;
   the result is final (a mismatch is 401 even beside a valid cookie). Other schemes and an empty
   `Bearer` are ignored.
2. **Otherwise the session cookie** (`__Host-` name if present, else the plain one), verified by
   the total codec in `central.operator_session`. A write it authorizes must be marked
   (`X-Photo-Wall-Console`), carry the exact Origin that signed in (a missing Origin is refused),
   and, when `Sec-Fetch-Site` is sent, say `same-origin`.

Every `/v1/operator/*` response is `Cache-Control: no-store`, including exception-handler and
unhandled-500 responses. Nothing here reads a cookie for Player, media or netboot routes, and no
CORS header is ever sent.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from central.operator_session import SESSION_SECONDS, SessionCodec, origin_scheme
from central.registry import RegistryError
from contracts.models import Model
from contracts.time import Clock

OPERATOR_PREFIX = "/v1/operator/"
SESSION_PATH = "/v1/operator/session"
COOKIE = "photo_wall_session"
HOST_COOKIE = "__Host-" + COOKIE
MARKER = "x-photo-wall-console"
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
NO_STORE = "no-store"

_BEARER = HTTPBearer(auto_error=False)


class SignIn(Model):
    token: str


def issued_cookie(scheme: str, value: str) -> str:
    """The sign-in `Set-Cookie`: the name and `Secure` follow the sign-in Origin's scheme."""
    if scheme == "https":
        return (f"{HOST_COOKIE}={value}; Path=/; Max-Age={SESSION_SECONDS}; Secure; HttpOnly; "
                "SameSite=Strict")
    return f"{COOKIE}={value}; Path=/; Max-Age={SESSION_SECONDS}; HttpOnly; SameSite=Strict"


def clearing_cookies(secure_plain: bool) -> tuple[str, str]:
    """Log out's two clearing `Set-Cookie`s; the plain one is `Secure` when logging out over https."""
    plain_secure = " Secure;" if secure_plain else ""
    return (
        f"{HOST_COOKIE}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict",
        f"{COOKIE}=; Path=/; Max-Age=0;{plain_secure} HttpOnly; SameSite=Strict",
    )


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


class _NoStoreOperator:
    """`Cache-Control: no-store` on every `/v1/operator/*` response this middleware sees."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(OPERATOR_PREFIX):
            await self.app(scope, receive, send)
            return

        async def send_no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = NO_STORE
            await send(message)

        await self.app(scope, receive, send_no_store)


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
        # 2. The cookie: the __Host- name when present, else the plain one.
        cookies = request.cookies
        session = self.codec.verify(cookies.get(HOST_COOKIE, cookies.get(COOKIE)))
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
            response = Response(status_code=204)
            response.headers.append("set-cookie", issued_cookie(origin_scheme(origin), value))
            return response

        @app.delete(SESSION_PATH, status_code=204, dependencies=[Depends(_marked)])
        def sign_out(request: Request) -> Response:
            response = Response(status_code=204)
            secure_plain = origin_scheme(request.headers.get("origin")) == "https"
            for header in clearing_cookies(secure_plain):
                response.headers.append("set-cookie", header)
            return response

        app.add_middleware(_NoStoreOperator)

        # An unhandled exception is answered by Starlette's outermost ServerErrorMiddleware,
        # outside every user middleware; this handler is where that response is built.
        async def server_error(request: Request, exc: Exception) -> Response:
            headers = {"Cache-Control": NO_STORE} if request.url.path.startswith(
                OPERATOR_PREFIX) else None
            return PlainTextResponse("Internal Server Error", status_code=500, headers=headers)

        app.add_exception_handler(Exception, server_error)
