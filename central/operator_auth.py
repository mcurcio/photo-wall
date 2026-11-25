"""Who the operator is, at the HTTP boundary (pass A §5-§6; decision 0035).

Two modes behind one door, each a small strategy (`_TokenMode`, `_ProviderMode`); the deployment
picks one by configuring an identity provider or not:

- **Token mode** (no provider): pass A, unchanged. One principal, the admin token, presented as
  `Bearer` or as the signed session cookie of `central.operator_session`.
- **Provider mode** (decision 0035): a person signs in through the provider and holds an opaque
  cookie naming an `operator_sessions` row; a machine sends the provider's access token as
  `Bearer`; the admin token, when set, is the break-glass `Bearer` and never signs a browser in.

What both modes share, decided here once:

1. **Bearer decides alone.** A non-empty `Bearer` credential is judged by the mode's Bearer order
   (`policy.bearer_principal`); the result is final (a mismatch is 401 even beside a valid
   cookie). Other schemes and an empty `Bearer` are ignored.
2. **Otherwise the session cookie.** It is scoped to `Path=/v1/operator/` so other servers on the
   same host (cookies ignore the port) never receive it: `__Secure-photo_wall_session` over
   https, `photo_wall_session` over http. The first present name decides: `__Secure-`, then the
   legacy `__Host-`, then the plain one. Legacy `Path=/` cookies issued before the scoping are
   still ACCEPTED in token mode until they expire (at most `SESSION_SECONDS` after the scoped
   release), so nobody is signed out by the upgrade; every sign-in and log out clears them.
3. **A write the cookie authorizes** must be marked (`X-Photo-Wall-Console`), carry the exact
   Origin that signed in (a missing Origin is refused), and, when `Sec-Fetch-Site` is sent, say
   `same-origin` (`policy.write_refusal`).
4. **A permission.** `OperatorAuth.admin` asks for `read` on GET, HEAD and OPTIONS and for
   `configure` on any other method; `OperatorAuth.operate` asks for `operate`. Both answer the
   audit `Actor` (`policy.actor_of`). A principal without the permission is 403 `forbidden`.

The routes (decision 0035, Routes): `GET sign-in`, `GET session`, `DELETE session` and
`GET access` in both modes; `POST session` (token mode; 404 with a provider); `GET session/start`,
`GET session/callback` and `POST session/backchannel-logout` (provider mode). Sign-in's redirects
back to the console are relative (`CONSOLE_PATH`), so they stay on the origin the browser reached
Central at, whatever a crafted `origin` says. No answer and no log line here carries the refresh
or access token; Log out's end-session address carries the person's own ID token as the
provider's hint.

Every `/v1/operator/*` response is `Cache-Control: no-store`, including exception-handler and
unhandled-500 responses. Nothing here reads a cookie for Player, media or netboot routes, and no
CORS header is ever sent.

`OPERATOR_PREFIX` is the one declaration the cookie path, the no-store scope and the route scope
derive from: `OperatorAuth.require_scoped` refuses, at app construction, any route that depends on
`admin` or `operate` outside it (a browser would never send the cookie there).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final, Protocol
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.dependencies.models import Dependant
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from central.actor import Actor
from central.operator_access import policy
from central.operator_access.model import (
    Machine,
    Permission,
    Person,
    Principal,
    ProviderUnreachable,
    SharedConsole,
    SignInRefusal,
    TokenRefused,
)
from central.operator_access.ports import TransactionSeal
from central.operator_access.service import ProviderAccess, SignInRefused
from central.operator_access.views import (
    AccessView,
    LoggedOut,
    ProviderView,
    SessionView,
    SignInMethod,
    SignInView,
)
from central.operator_session import SESSION_SECONDS, SessionCodec, origin_scheme
from central.registry import RegistryError
from contracts.models import Model
from contracts.time import Clock

OPERATOR_PREFIX = "/v1/operator/"
SESSION_PATH = "/v1/operator/session"
SIGN_IN_PATH = OPERATOR_PREFIX + "sign-in"
ACCESS_PATH = OPERATOR_PREFIX + "access"
START_PATH = SESSION_PATH + "/start"
CALLBACK_PATH = SESSION_PATH + "/callback"
BACKCHANNEL_LOGOUT_PATH = SESSION_PATH + "/backchannel-logout"
# Where the console is served (central/app.py `index`): sign-in and the provider's Log out land here.
CONSOLE_PATH: Final = "/"
SIGN_IN_OUTCOME: Final = "sign_in"   # the console reads `?sign_in=<SignInRefusal>` after a failed sign-in
# The sign-in transaction's cookie (decision 0035): sealed, ten minutes, sent only to the two
# sign-in routes. SameSite=Lax, because the provider's redirect back is a cross-site navigation.
TRANSACTION_COOKIE = "photo_wall_sign_in"
SECURE_TRANSACTION_COOKIE = "__Secure-" + TRANSACTION_COOKIE
TRANSACTION_COOKIE_PATH = SESSION_PATH + "/"
MAX_LOGOUT_BODY_BYTES: Final = 64 * 1024
COOKIE = "photo_wall_session"  # over http
SECURE_COOKIE = "__Secure-" + COOKIE  # over https (`__Host-` would force Path=/)
COOKIE_PATH = OPERATOR_PREFIX
# Issued with Path=/ before the scoping; accepted until they expire, cleared at sign-in and log out.
LEGACY_HOST_COOKIE = "__Host-" + COOKIE
LEGACY_PATH = "/"
# The first name present decides: prefix-protected names before the plain one.
COOKIE_PRECEDENCE = (SECURE_COOKIE, LEGACY_HOST_COOKIE, COOKIE)
MARKER = "x-photo-wall-console"
READ_METHODS = policy.READ_METHODS
NO_STORE = "no-store"

_LOG = logging.getLogger(__name__)

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


async def _server_error(request: Request, exc: Exception) -> Response:
    """The unhandled-500 answer, built from the prefix table (`add_prefix_headers`)."""
    headers = _prefix_headers_for(request.app, request.url.path) or None
    return PlainTextResponse("Internal Server Error", status_code=500, headers=headers)


def add_prefix_headers(app: FastAPI, prefix: str, headers: Mapping[str, str]) -> None:
    """Put `headers` on every response under `prefix`: the user middleware for every answer a
    route or FastAPI builds, and the same table for the unhandled-500 handler, which Starlette's
    outermost ServerErrorMiddleware runs outside every user middleware. The table and that
    handler are created together, on the first call for an app, so no answer under a prefix
    can miss its headers whatever else the app mounts."""
    table = getattr(app.state, "prefix_headers", None)
    if table is None:
        table = app.state.prefix_headers = []
        # An unhandled exception is answered by Starlette's outermost ServerErrorMiddleware,
        # outside every user middleware; this handler is where that response is built.
        app.add_exception_handler(Exception, _server_error)
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


def _transaction_cookie(scheme: str | None, value: str, max_age: int) -> str:
    name, secure = ((SECURE_TRANSACTION_COOKIE, " Secure;") if scheme == "https"
                    else (TRANSACTION_COOKIE, ""))
    return (f"{name}={value}; Path={TRANSACTION_COOKIE_PATH}; Max-Age={max_age};{secure} HttpOnly; "
            "SameSite=Lax")


def _console(redirect: Response, outcome: SignInRefusal | None = None, hash_route: str = "") -> Response:
    """Point a redirect at the console: relative, so it stays on the origin the browser used."""
    query = f"?{SIGN_IN_OUTCOME}={outcome.value}" if outcome is not None else ""
    redirect.headers["location"] = CONSOLE_PATH + query + hash_route
    return redirect


def _session_view(principal: Principal, provider_name: str | None) -> SessionView:
    if isinstance(principal, (Person, Machine)):
        return SessionView(method="provider", name=principal.name, roles=tuple(sorted(principal.roles)),
                           provider=provider_name)
    return SessionView(method="token", name=None, roles=("owner",), provider=None)


class _Mode(Protocol):
    """How the deployment lets operators in. `OperatorAuth` asks; a mode never reads a request's
    method or decides a permission."""
    method: SignInMethod

    def bearer(self, candidate: bytes) -> Principal | None:
        """Who a non-empty Bearer credential is (the wire's bytes); None is 401."""

    def cookie(self, value: str | None) -> Person | SharedConsole | None:
        """Who the session cookie is; None is 401."""

    def sign_in_view(self) -> SignInView: ...

    def session_view(self, principal: Principal) -> SessionView: ...

    def access_view(self) -> AccessView: ...

    def mount(self, app: FastAPI) -> None:
        """Bind this mode's session routes."""


class _TokenMode:
    """Pass A, unchanged: the admin token as Bearer, or the signed cookie it keys."""
    method: SignInMethod = "token"

    def __init__(self, codec: SessionCodec) -> None:
        self.codec = codec

    def bearer(self, candidate: bytes) -> Principal | None:
        return policy.bearer_principal(candidate, break_glass=self.codec.token_matches, machine=None)

    def cookie(self, value: str | None) -> SharedConsole | None:
        session = self.codec.verify(value)
        return None if session is None else SharedConsole(origin=session.origin)

    def sign_in_view(self) -> SignInView:
        return SignInView(method="token")

    def session_view(self, principal: Principal) -> SessionView:
        return _session_view(principal, None)

    def access_view(self) -> AccessView:
        return AccessView(method="token", provider=None)

    def mount(self, app: FastAPI) -> None:
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


class _ProviderMode:
    """Decision 0035: a person's session row, a machine's access token, and the break-glass
    token when the deployment keeps one (`break_glass`: the one token compare, or None)."""
    method: SignInMethod = "provider"

    def __init__(self, access: ProviderAccess, seal: TransactionSeal, break_glass: SessionCodec | None) -> None:
        self.access = access
        self.seal = seal
        self._break_glass = None if break_glass is None else break_glass.token_matches

    def bearer(self, candidate: bytes) -> Principal | None:
        return policy.bearer_principal(candidate, break_glass=self._break_glass, machine=self._machine)

    def _machine(self, candidate: bytes) -> Machine | None:
        try:
            return self.access.machine(candidate.decode("ascii"))
        except UnicodeDecodeError:   # a signed token is ASCII
            return None

    def cookie(self, value: str | None) -> Person | None:
        return self.access.person(value)

    def sign_in_view(self) -> SignInView:
        return SignInView(method="provider", name=self.access.settings.name)

    def session_view(self, principal: Principal) -> SessionView:
        return _session_view(principal, self.access.settings.name)

    def access_view(self) -> AccessView:
        settings, reach = self.access.settings, self.access.reach()
        return AccessView(method="provider", provider=ProviderView(
            name=settings.name, issuer_host=settings.issuer_host, reachable=reach.reachable,
            last_reached_at=reach.last_reached_at))

    def mount(self, app: FastAPI) -> None:
        access, seal = self.access, self.seal

        @app.post(SESSION_PATH)
        def token_sign_in() -> Response:
            # The token never signs a browser in when a provider is configured.
            raise RegistryError("not_found", 404)

        @app.get(START_PATH)
        def start(request: Request) -> Response:
            """`origin`: the console's own origin (the redirect address the provider must have
            registered is built from it); `return`: the console hash route to land on."""
            origin = request.query_params.get("origin")
            scheme = origin_scheme(origin)
            if scheme is None:
                raise RegistryError("origin_mismatch", 403)
            try:
                address, transaction = access.begin(origin, request.query_params.get("return"), CALLBACK_PATH)
            except ProviderUnreachable:
                return _console(Response(status_code=302), SignInRefusal.PROVIDER_UNREACHABLE)
            response = Response(status_code=302, headers={"location": address})
            response.headers.append("set-cookie", _transaction_cookie(
                scheme, seal.seal(transaction), int(policy.TRANSACTION_SECONDS)))
            return response

        @app.get(CALLBACK_PATH)
        def callback(request: Request) -> Response:
            cookies, query = request.cookies, request.query_params
            transaction = seal.open(next(
                (cookies[name] for name in (SECURE_TRANSACTION_COOKIE, TRANSACTION_COOKIE) if name in cookies),
                None))
            response = Response(status_code=302)
            # The transaction is over either way: one cookie, one redirect back.
            for scheme in ("https", "http"):
                response.headers.append("set-cookie", _transaction_cookie(scheme, "", 0))
            try:
                signed_in = access.finish(transaction, code=query.get("code"), state=query.get("state"),
                                          callback_path=CALLBACK_PATH)
            except SignInRefused as refused:
                _LOG.warning("operator sign-in refused: %s%s", refused.refusal.value,
                             f" ({refused.detail})" if refused.detail else "")
                return _console(response, refused.refusal)
            scheme = origin_scheme(signed_in.session.origin)
            for header in (issued_cookie(scheme, signed_in.cookie_value),
                           *legacy_clearing_cookies(scheme == "https")):
                response.headers.append("set-cookie", header)
            _LOG.info("operator signed in: %s", signed_in.session.operator_id)
            return _console(response, hash_route=transaction.return_to if transaction else "")

        @app.delete(SESSION_PATH, dependencies=[Depends(_marked)], response_model=LoggedOut)
        def log_out(request: Request) -> Response:
            end_session = access.log_out(_session_cookie(request), CONSOLE_PATH)
            response = JSONResponse(LoggedOut(end_session=end_session).model_dump(mode="json"))
            secure_plain = origin_scheme(request.headers.get("origin")) == "https"
            for header in clearing_cookies(secure_plain):
                response.headers.append("set-cookie", header)
            return response

        @app.post(BACKCHANNEL_LOGOUT_PATH)
        async def backchannel_logout(request: Request) -> Response:
            """OpenID Connect Back-Channel Logout 1.0: the provider posts `logout_token` as a
            form field. 200 once every session it names is ended; 400 for anything else."""
            body = await request.body()
            tokens = (parse_qs(body.decode("ascii", "replace")).get("logout_token", [])
                      if len(body) <= MAX_LOGOUT_BODY_BYTES else [])
            if len(tokens) != 1:
                raise RegistryError("invalid_request", 400)
            try:
                ended = await run_in_threadpool(access.backchannel_logout, tokens[0])
            except TokenRefused as refused:
                _LOG.warning("back-channel logout refused: %s", refused.reason.value)
                raise RegistryError("invalid_request", 400) from None
            except ProviderUnreachable:
                _LOG.warning("back-channel logout refused: the provider's keys could not be read")
                raise RegistryError("invalid_request", 400) from None
            _LOG.info("back-channel logout ended %d session(s)", ended)
            return Response(status_code=200)


class OperatorAuth:
    """The `admin` and `operate` dependencies and the operator's door. `provider`: provider
    mode's use cases with the seal of its transaction cookie, or None for token mode, where
    `admin_token` is required. With a provider, `admin_token` is the optional break-glass token.
    The session key derives from the token here, once, so a wrong-length or unencodable token
    fails at construction."""

    def __init__(self, admin_token: str | None, clock: Clock, *,
                 provider: tuple[ProviderAccess, TransactionSeal] | None = None) -> None:
        codec = None if admin_token is None else SessionCodec(admin_token, clock)
        if provider is not None:
            self._mode: _Mode = _ProviderMode(*provider, break_glass=codec)
        elif codec is not None:
            self._mode = _TokenMode(codec)
        else:
            raise ValueError("an admin token or an identity provider is required")

    def _principal(self, request: Request, credentials: HTTPAuthorizationCredentials | None) -> Principal:
        # 1. Bearer decides alone (HTTPBearer yields None for other schemes and an empty Bearer).
        if credentials is not None and credentials.credentials:
            candidate = _wire(credentials.credentials)
            principal = None if candidate is None else self._mode.bearer(candidate)
            if principal is None:
                raise RegistryError("unauthorized", 401)
            return principal
        # 2. The cookie: the first present name in COOKIE_PRECEDENCE decides.
        session = self._mode.cookie(_session_cookie(request))
        if session is None:
            raise RegistryError("unauthorized", 401)
        # 3. A write must come from the page that signed in.
        if request.method not in READ_METHODS:
            refusal = policy.write_refusal(
                marked=MARKER in request.headers, origin=request.headers.get("origin"),
                session_origin=session.origin, fetch_site=request.headers.get("sec-fetch-site"))
            if refusal is not None:
                raise RegistryError(refusal, 403)
        return session

    def _require(self, request: Request, credentials: HTTPAuthorizationCredentials | None,
                 permission: Permission) -> Actor:
        principal = self._principal(request, credentials)
        if not policy.permits(principal, permission):
            raise RegistryError("forbidden", 403)
        request.state.operator = principal
        return policy.actor_of(principal)

    def admin(
        self,
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> Actor:
        """The operator's principal as the actor its writes are recorded under, once it holds
        what the request's method asks for: `read` for GET, HEAD and OPTIONS, `configure` for any
        other. "api-token" for the admin (break-glass) token, "console" for token mode's cookie,
        `person:<id>` for a signed-in person, `client:<id>` for a machine."""
        return self._require(request, credentials, policy.permission_for(request.method))

    def operate(
        self,
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> Actor:
        """As `admin`, asking for `operate`: the everyday actions (decision 0036)."""
        return self._require(request, credentials, "operate")

    @staticmethod
    def holds(request: Request, permission: Permission) -> bool:
        """Whether the operator `admin` or `operate` admitted for `request` also holds
        `permission`: for a route that asks more of some of its options (Show now's `force`,
        restart and priority ask `configure`, decision 0036). False when neither admitted it."""
        principal = getattr(request.state, "operator", None)
        return principal is not None and policy.permits(principal, permission)

    def mount(self, app: FastAPI) -> None:
        """Bind the door's routes (the mode's and the shared reads) and the no-store headers
        (with their unhandled-500 handler, `add_prefix_headers`)."""
        mode = self._mode
        mode.mount(app)

        @app.get(SIGN_IN_PATH, response_model=SignInView, response_model_exclude_none=True)
        def how_to_sign_in() -> SignInView:
            return mode.sign_in_view()

        @app.get(SESSION_PATH, response_model=SessionView, dependencies=[Depends(self.admin)])
        def who_am_i(request: Request) -> SessionView:
            return mode.session_view(request.state.operator)

        @app.get(ACCESS_PATH, response_model=AccessView, dependencies=[Depends(self.admin)])
        def access() -> AccessView:
            return mode.access_view()

        add_prefix_headers(app, OPERATOR_PREFIX, {"Cache-Control": NO_STORE})

    def require_scoped(self, app: FastAPI) -> None:
        """Refuse an app with a route that depends on `admin` or `operate` outside
        `OPERATOR_PREFIX`, where the session cookie is never sent and no-store does not apply.
        Call once every route is bound."""
        stray = sorted(f"{','.join(sorted(route.methods))} {route.path}" for route in app.routes
                       if isinstance(route, APIRoute)
                       and not route.path.startswith(OPERATOR_PREFIX)
                       and any(_depends_on(route.dependant, guard) for guard in (self.admin, self.operate)))
        if stray:
            raise RuntimeError(f"operator routes outside {OPERATOR_PREFIX}: {stray}")
