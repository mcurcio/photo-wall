"""`FakeProvider`: an identity provider for tests (decision 0035), in two forms at once.

- **In process** it implements `central.operator_access.ports.IdentityProvider`, so a test hands
  it to `create_app(..., identity_provider=fake)` and no socket is opened.
- **Over HTTP** (`with fake.serving():`) it is a minimal OpenID Connect issuer on an ephemeral
  loopback port: discovery, JWKS, the authorization endpoint (which signs in `approves` at once
  and redirects back), the token endpoint (authorization code with PKCE, refresh, client
  credentials) and end-session. The production client (`central/infra/oidc_provider.py`) and a
  browser drive that form. `issuer` becomes the listener's address while it serves.

Both forms are one state machine (`_grant`), and tests/test_operator_provider_conformance.py
holds the in-process port and the production client over HTTP to one suite.

Tokens are real: RS256 under an RSA key generated once per process (`rotate_keys` makes
another). Access tokens are shaped as Keycloak's: client roles under
`resource_access.<client id>.roles`, and the client in `aud` only while the principal holds one of
its roles. Refresh tokens are opaque and single use.

Switches: `drop_role`, `grant_role`, `disable` (its refresh is refused), `unreachable`
(every call raises `ProviderUnreachable`; over HTTP every answer is 503), `end_session`,
`rotate_keys`, and `logout_token` / `send_backchannel_logout`.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from central.infra.jwt_signature import verified_claims
from central.operator_access.model import (
    Claims,
    ProviderRefused,
    ProviderUnreachable,
    Reach,
    SignatureRefusal,
    TokenSet,
)
from central.operator_access.policy import BACKCHANNEL_LOGOUT_EVENT
from central.operator_access.settings import OidcSettings
from contracts.time import Clock, SystemClock

ISSUER = "https://id.test/realms/home"
CLIENT_ID = "photo-wall"
CLIENT_SECRET = "fake-client-secret-" + "s" * 24
OWNER = "owner"


@lru_cache(maxsize=4)
def _rsa_key(generation: int) -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@dataclass
class FakeUser:
    subject: str
    name: str
    roles: set[str] = field(default_factory=lambda: {OWNER})
    enabled: bool = True


@dataclass
class _ServiceAccount:
    secret: str
    user: FakeUser


class _Refusal(Exception):
    """The token endpoint's "no": (status, error word)."""

    def __init__(self, error: str, status: int = 400) -> None:
        self.error, self.status = error, status
        super().__init__(error)


class FakeProvider:
    def __init__(self, *, issuer: str = ISSUER, client_id: str = CLIENT_ID,
                 client_secret: str = CLIENT_SECRET, clock: Clock | None = None,
                 access_seconds: float = 300.0) -> None:
        self.issuer = issuer
        self.client_id = client_id
        self.client_secret = client_secret
        self.clock: Clock = clock or SystemClock()
        self.access_seconds = access_seconds
        self.users: dict[str, FakeUser] = {}
        self.service_accounts: dict[str, _ServiceAccount] = {}
        self.approves: str | None = None       # the subject the authorization endpoint signs in
        self.unreachable = False
        self.redirect_uris: set[str] | None = None   # None: any redirect address is registered
        self.backchannel_url: str | None = None
        self.token_requests: list[dict[str, str]] = []   # every grant asked for, as its form
        self.issued: list[dict[str, Any]] = []            # every token endpoint answer, as given
        self.refresh_calls = 0                            # the port's refreshes asked, answered or not
        self._generation = 0
        self._codes: dict[str, dict[str, Any]] = {}
        self._refresh_tokens: dict[str, tuple[str, str]] = {}   # token -> (subject, sid)
        self._sessions: set[str] = set()
        self._last_reached_at: float | None = None
        self._lock = threading.RLock()

    # ---- the realm ----

    def settings(self, **overrides: Any) -> OidcSettings:
        """Central's settings for this provider as it stands now (its issuer while serving)."""
        return OidcSettings(**{"issuer": self.issuer, "client_id": self.client_id,
                               "client_secret": self.client_secret, "name": "Fake ID", **overrides})

    def add_user(self, subject: str = "user-1", name: str = "Ada Owner", *,
                 roles: tuple[str, ...] = (OWNER,), approve: bool = True) -> FakeUser:
        user = self.users[subject] = FakeUser(subject, name, set(roles))
        if approve:
            self.approves = subject
        return user

    def add_service_account(self, client_id: str = "reader-script", *, secret: str = "script-secret",
                            roles: tuple[str, ...] = (OWNER,)) -> FakeUser:
        user = FakeUser(f"service-account-{client_id}", f"service-account-{client_id}", set(roles))
        self.users[user.subject] = user
        self.service_accounts[client_id] = _ServiceAccount(secret, user)
        return user

    def drop_role(self, subject: str, role: str = OWNER) -> None:
        self.users[subject].roles.discard(role)

    def grant_role(self, subject: str, role: str = OWNER) -> None:
        self.users[subject].roles.add(role)

    def disable(self, subject: str) -> None:
        self.users[subject].enabled = False

    def end_session(self, sid: str) -> None:
        """The provider's own session ends (an admin's sign-out, its timeout)."""
        with self._lock:
            self._sessions.discard(sid)

    def rotate_keys(self) -> None:
        self._generation += 1

    # ---- keys and tokens ----

    @property
    def key_id(self) -> str:
        return f"fake-key-{self._generation}"

    def jwks(self) -> dict[str, Any]:
        public = jwt.algorithms.RSAAlgorithm.to_jwk(_rsa_key(self._generation).public_key(), as_dict=True)
        return {"keys": [{**public, "kid": self.key_id, "use": "sig", "alg": "RS256"}]}

    def sign(self, claims: Mapping[str, Any], *, algorithm: str = "RS256", key_id: str | None = None) -> str:
        """Any claims as a token under the current key: how a test forges a wrong one."""
        return jwt.encode(dict(claims), _rsa_key(self._generation), algorithm=algorithm,
                          headers={"kid": key_id or self.key_id})

    def _times(self, seconds: float) -> dict[str, Any]:
        now = self.clock.utc()
        return {"iss": self.issuer, "iat": int(now), "exp": int(now + seconds), "jti": secrets.token_hex(8)}

    def access_claims(self, subject: str, sid: str | None = None) -> dict[str, Any]:
        user = self.users[subject]
        claims: dict[str, Any] = {
            **self._times(self.access_seconds), "sub": subject, "typ": "Bearer", "azp": self.client_id,
            # Keycloak adds a client to `aud` only while the principal holds one of its roles.
            "aud": [self.client_id, "account"] if user.roles else ["account"],
            "resource_access": {self.client_id: {"roles": sorted(user.roles)}},
            "name": user.name, "preferred_username": user.name.lower().replace(" ", "."),
        }
        if sid is not None:
            claims["sid"] = sid
        return claims

    def access_token(self, subject: str, sid: str | None = None, **overrides: Any) -> str:
        """An access token for `subject`, with any claim overridden (None removes it)."""
        claims = {**self.access_claims(subject, sid), **overrides}
        return self.sign({key: value for key, value in claims.items() if value is not None})

    def id_token(self, subject: str, *, nonce: str | None, sid: str | None, access_token: str = "",
                 **overrides: Any) -> str:
        half = hashlib.sha256(access_token.encode("ascii")).digest()[:16]
        claims = {**self._times(self.access_seconds), "sub": subject, "aud": self.client_id, "typ": "ID",
                  "azp": self.client_id, "nonce": nonce, "sid": sid, "at_hash": _b64(half),
                  "name": self.users[subject].name, **overrides}
        return self.sign({key: value for key, value in claims.items() if value is not None})

    def logout_token(self, *, sid: str | None = None, sub: str | None = None, **overrides: Any) -> str:
        claims = {**self._times(120), "aud": self.client_id, "typ": "Logout", "sid": sid, "sub": sub,
                  "events": {BACKCHANNEL_LOGOUT_EVENT: {}}, **overrides}
        return self.sign({key: value for key, value in claims.items() if value is not None})

    def service_token(self, client_id: str = "reader-script") -> str:
        """What a script gets from the client-credentials grant."""
        account = self.service_accounts[client_id]
        return self._grant({"grant_type": "client_credentials"}, client_id, account.secret)["access_token"]

    def send_backchannel_logout(self, *, sid: str | None = None, sub: str | None = None,
                                post: Callable[..., Any] | None = None, url: str | None = None) -> Any:
        """Post a logout token to Central as the provider does (`post`: a client's `post`, e.g. a
        TestClient's; httpx otherwise). The provider's session ends too."""
        if sid is not None:
            self.end_session(sid)
        return (post or httpx.post)(url or self.backchannel_url,
                                    data={"logout_token": self.logout_token(sid=sid, sub=sub)})

    # ---- the state machine both forms share ----

    def _reached(self) -> None:
        if self.unreachable:
            raise ProviderUnreachable()
        self._last_reached_at = self.clock.utc()

    def _authorize(self, query: Mapping[str, str]) -> str:
        """The authorization endpoint: where the browser goes next."""
        redirect_uri = query.get("redirect_uri", "")
        if (query.get("client_id") != self.client_id or query.get("response_type") != "code"
                or "openid" not in query.get("scope", "").split()
                or query.get("code_challenge_method") != "S256" or not query.get("code_challenge")
                or (self.redirect_uris is not None and redirect_uri not in self.redirect_uris)):
            raise _Refusal("invalid_request")
        back = {"state": query.get("state", "")}
        user = self.users.get(self.approves or "")
        if user is None or not user.enabled:
            back["error"] = "access_denied"
        else:
            code, sid = secrets.token_urlsafe(16), secrets.token_urlsafe(8)
            with self._lock:
                self._sessions.add(sid)
                self._codes[code] = {"subject": user.subject, "redirect_uri": redirect_uri, "sid": sid,
                                     "challenge": query["code_challenge"], "nonce": query.get("nonce")}
            back["code"] = code
        return f"{redirect_uri}{'&' if '?' in redirect_uri else '?'}{urlencode(back)}"

    def approve(self, authorization_url: str) -> str:
        """The browser's visit to the authorization address, in process: the redirect back."""
        self._reached()
        parts = urlsplit(authorization_url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == self.issuer + "/authorize"
        return self._authorize({key: values[0] for key, values in parse_qs(parts.query).items()})

    def _grant(self, form: Mapping[str, str], client_id: str | None, client_secret: str | None) -> dict[str, Any]:
        self.token_requests.append(dict(form))
        grant_type = form.get("grant_type")
        with self._lock:
            if grant_type == "client_credentials":
                account = self.service_accounts.get(client_id or "")
                if account is None or account.secret != client_secret:
                    raise _Refusal("invalid_client", 401)
                return {"access_token": self.access_token(account.user.subject), "token_type": "Bearer",
                        "expires_in": int(self.access_seconds)}
            if client_id != self.client_id or client_secret != self.client_secret:
                raise _Refusal("invalid_client", 401)
            if grant_type == "authorization_code":
                grant = self._codes.pop(form.get("code", ""), None)   # a code is used once
                challenge = _b64(hashlib.sha256(form.get("code_verifier", "").encode("ascii")).digest())
                if (grant is None or grant["redirect_uri"] != form.get("redirect_uri")
                        or grant["challenge"] != challenge):
                    raise _Refusal("invalid_grant")
                return self._tokens(grant["subject"], grant["sid"], grant["nonce"])
            if grant_type == "refresh_token":
                held = self._refresh_tokens.pop(form.get("refresh_token", ""), None)   # single use
                if held is None or held[1] not in self._sessions or not self.users[held[0]].enabled:
                    raise _Refusal("invalid_grant")
                return self._tokens(held[0], held[1], None)
            raise _Refusal("unsupported_grant_type")

    def _tokens(self, subject: str, sid: str, nonce: str | None) -> dict[str, Any]:
        access_token = self.access_token(subject, sid)
        refresh_token = secrets.token_urlsafe(24)
        self._refresh_tokens[refresh_token] = (subject, sid)
        body = {"access_token": access_token, "token_type": "Bearer", "refresh_token": refresh_token,
                "expires_in": int(self.access_seconds),
                "id_token": self.id_token(subject, nonce=nonce, sid=sid, access_token=access_token)}
        self.issued.append(body)
        return body

    def _end_session_address(self) -> str:
        return self.issuer + "/logout"

    # ---- IdentityProvider, in process ----

    def authorization_url(self, *, redirect_uri: str, state: str, nonce: str, code_challenge: str) -> str:
        self._reached()
        return self.issuer + "/authorize?" + urlencode({
            "response_type": "code", "scope": "openid", "client_id": self.client_id,
            "redirect_uri": redirect_uri, "state": state, "nonce": nonce,
            "code_challenge": code_challenge, "code_challenge_method": "S256"})

    def _token_set(self, form: Mapping[str, str]) -> TokenSet:
        self._reached()
        try:
            body = self._grant(form, self.client_id, self.client_secret)
        except _Refusal as refusal:
            raise ProviderRefused(refusal.error) from None
        return TokenSet(body["access_token"], body.get("id_token"), body.get("refresh_token"))

    def exchange(self, *, code: str, redirect_uri: str, code_verifier: str) -> TokenSet:
        return self._token_set({"grant_type": "authorization_code", "code": code,
                                "redirect_uri": redirect_uri, "code_verifier": code_verifier})

    def refresh(self, refresh_token: str) -> TokenSet:
        self.refresh_calls += 1
        return self._token_set({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def signature(self, token: str, algorithm: str, key_id: str | None) -> Claims | SignatureRefusal:
        return verified_claims(token, algorithm, key_id, self.jwks())

    def end_session_url(self, *, id_token_hint: str | None, post_logout_redirect_uri: str) -> str | None:
        if self.unreachable:
            return None
        query = {"client_id": self.client_id, "post_logout_redirect_uri": post_logout_redirect_uri}
        if id_token_hint:
            query["id_token_hint"] = id_token_hint
        return self._end_session_address() + "?" + urlencode(query)

    def reach(self) -> Reach:
        try:
            self._reached()
        except ProviderUnreachable:
            return Reach(False, self._last_reached_at)
        return Reach(True, self._last_reached_at)

    # ---- over HTTP ----

    @contextmanager
    def serving(self) -> Iterator[FakeProvider]:
        """Serve this provider on an ephemeral loopback port; `issuer` is its address meanwhile."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        server.daemon_threads = True
        before, self.issuer = self.issuer, f"http://127.0.0.1:{server.server_address[1]}"
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            yield self
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            self.issuer = before

    def _metadata(self) -> dict[str, Any]:
        return {"issuer": self.issuer, "authorization_endpoint": self.issuer + "/authorize",
                "token_endpoint": self.issuer + "/token", "jwks_uri": self.issuer + "/jwks",
                "end_session_endpoint": self._end_session_address(),
                "id_token_signing_alg_values_supported": ["RS256"],
                "backchannel_logout_supported": True, "backchannel_logout_session_supported": True}

    def _logged_out(self, query: Mapping[str, str]) -> str | None:
        """The end-session endpoint: end the hinted session, and where to go back to."""
        hint = query.get("id_token_hint")
        if hint:
            claims = self.signature(hint, "RS256", jwt.get_unverified_header(hint).get("kid"))
            if isinstance(claims, Mapping) and isinstance(claims.get("sid"), str):
                self.end_session(claims["sid"])
        return query.get("post_logout_redirect_uri")


def _handler(provider: FakeProvider) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:   # quiet
            return None

        def _send(self, status: int, body: bytes = b"", content_type: str = "application/json",
                  location: str | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if location is not None:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, body: Mapping[str, Any]) -> None:
            self._send(status, json.dumps(body).encode())

        def _query(self) -> dict[str, str]:
            return {key: values[0] for key, values in parse_qs(urlsplit(self.path).query).items()}

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if provider.unreachable:
                return self._send(503)
            provider._reached()
            if path == "/.well-known/openid-configuration":
                return self._json(200, provider._metadata())
            if path == "/jwks":
                return self._json(200, provider.jwks())
            if path == "/authorize":
                try:
                    return self._send(302, location=provider._authorize(self._query()))
                except _Refusal as refusal:
                    return self._json(400, {"error": refusal.error})
            if path == "/logout":
                back = provider._logged_out(self._query())
                return (self._send(302, location=back) if back
                        else self._send(200, b"signed out", "text/plain"))
            return self._send(404)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            form = {key: values[0] for key, values in parse_qs(self.rfile.read(length).decode()).items()}
            if provider.unreachable:
                return self._send(503)
            provider._reached()
            if urlsplit(self.path).path != "/token":
                return self._send(404)
            client_id, client_secret = form.pop("client_id", None), form.pop("client_secret", None)
            authorization = self.headers.get("Authorization", "")
            if authorization.startswith("Basic "):
                stated_id, _, stated_secret = base64.b64decode(authorization[6:]).decode().partition(":")
                client_id, client_secret = unquote(stated_id), unquote(stated_secret)
            try:
                return self._json(200, provider._grant(form, client_id, client_secret))
            except _Refusal as refusal:
                return self._json(refusal.status, {"error": refusal.error})

    return Handler
