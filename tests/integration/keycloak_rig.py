"""The real-Keycloak rig (decision 0035, Acceptance): the realm tests/integration/keycloak/realm.json
imports into the pinned Keycloak of tests/integration/compose.keycloak.yml, Central under uvicorn
where that realm's `photo-wall` client says it is, a browser that is only an HTTP client with one
cookie jar, and Keycloak's admin REST API.

`PHOTO_WALL_KEYCLOAK` is Keycloak's address as the compose file publishes it
(`http://127.0.0.1:18180`); without it every test that takes the `keycloak` fixture skips with
"set PHOTO_WALL_KEYCLOAK" (CI's `keycloak` job in software-e2e.yml sets it).

Every name, secret and lifetime here is read from the realm file, its one home. The secrets are
fixed, test-only values of a disposable realm, never deployment data.

**Central's address is fixed** (`CENTRAL_ORIGIN`, 127.0.0.1:18181), because two things in the
realm name it and Keycloak takes neither as a pattern over ports: the client's redirect address,
which Keycloak compares exactly (the record's "refuses any redirect address not registered on the
client, exactly" is what is pinned), and the client's back-channel logout address.

**Keycloak reaches Central** for back-channel logout at `host.docker.internal:18181`, which the
compose file maps to the Docker host (`extra_hosts: host-gateway`). So Central listens on every
interface by default (`PHOTO_WALL_KEYCLOAK_CENTRAL_BIND`, default `0.0.0.0`): from a container
the host is the bridge's gateway address, never loopback. It serves a throwaway database for the
length of one test.
"""
from __future__ import annotations

import base64
import contextlib
import html
import http.cookiejar
import json
import os
import re
import secrets
import socket
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
import uvicorn

from central.app import create_app
from central.db import Database
from central.operator_access.policy import code_challenge
from central.operator_access.settings import OidcSettings
from central.operator_auth import BACKCHANNEL_LOGOUT_PATH, CALLBACK_PATH, START_PATH
from contracts.time import SystemClock

KEYCLOAK_VARIABLE: Final = "PHOTO_WALL_KEYCLOAK"
SKIP_REASON: Final = f"set {KEYCLOAK_VARIABLE}"
BIND_VARIABLE: Final = "PHOTO_WALL_KEYCLOAK_CENTRAL_BIND"
# The compose file's bootstrap administrator: fixed, test-only, of a disposable Keycloak.
ADMIN_USER: Final = "admin"
ADMIN_PASSWORD: Final = "isolated-test-only"

REALM_FILE: Final = Path(__file__).parent / "keycloak" / "realm.json"
_REALM: Final[Mapping[str, Any]] = json.loads(REALM_FILE.read_text())
REALM: Final[str] = _REALM["realm"]
ACCESS_TOKEN_SECONDS: Final[int] = _REALM["accessTokenLifespan"]
_CLIENTS: Final = {client["clientId"]: client for client in _REALM["clients"]}
_USERS: Final = {user["username"]: user for user in _REALM["users"]}

CLIENT_ID: Final = "photo-wall"
ROLE: Final = "owner"
AGENT: Final = "photo-wall-agent"          # a service account that holds photo-wall's owner role
STRANGER: Final = "photo-wall-stranger"    # a service account that holds no role
OWNER_USER: Final = "owner-user"
NO_ROLE_USER: Final = "no-role-user"
PROVIDER_NAME: Final = "Keycloak under test"

(_REDIRECT,) = _CLIENTS[CLIENT_ID]["redirectUris"]
CENTRAL_ORIGIN: Final = "{0.scheme}://{0.netloc}".format(urlsplit(_REDIRECT))
CENTRAL_PORT: Final[int] = urlsplit(_REDIRECT).port
assert _REDIRECT == CENTRAL_ORIGIN + CALLBACK_PATH, _REDIRECT
_BACKCHANNEL: Final = urlsplit(_CLIENTS[CLIENT_ID]["attributes"]["backchannel.logout.url"])
assert (_BACKCHANNEL.port, _BACKCHANNEL.path) == (CENTRAL_PORT, BACKCHANNEL_LOGOUT_PATH), _BACKCHANNEL

MARK: Final = {"X-Photo-Wall-Console": "1"}
START_SECONDS: Final = 20.0
_LOGIN_FORM: Final = re.compile(r'<form[^>]*\bid="kc-form-login"[^>]*>', re.IGNORECASE)
_ACTION: Final = re.compile(r'\baction="([^"]*)"')


def secret_of(client_id: str) -> str:
    return _CLIENTS[client_id]["secret"]


def password_of(username: str) -> str:
    (credential,) = _USERS[username]["credentials"]
    return credential["value"]


def name_of(username: str) -> str:
    """A person's name as Keycloak's `name` claim states it: first and last name."""
    return f"{_USERS[username]['firstName']} {_USERS[username]['lastName']}"


def keycloak_address() -> str:
    """Keycloak's address from the environment. LookupError(SKIP_REASON) without it."""
    address = os.environ.get(KEYCLOAK_VARIABLE, "").strip().rstrip("/")
    if not address:
        raise LookupError(SKIP_REASON)
    return address


def claims_of(token: str) -> dict[str, Any]:
    """A JWT's payload, unverified: what a test reads to say what Keycloak stated. Central's own
    judge is what verifies (central/operator_access/policy.py)."""
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def header_of(token: str) -> dict[str, Any]:
    first = token.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(first + "=" * (-len(first) % 4)))


def until(condition: Callable[[], Any], seconds: float, what: str, interval: float = 0.1) -> Any:
    """`condition`'s first truthy answer within `seconds`; AssertionError naming `what` otherwise."""
    deadline = time.monotonic() + seconds
    while True:
        answer = condition()
        if answer:
            return answer
        if time.monotonic() >= deadline:
            raise AssertionError(f"not within {seconds:g} s: {what}")
        time.sleep(interval)


class Keycloak:
    """The Keycloak under test: its realm's protocol endpoints and its admin REST API, as the
    bootstrap administrator. It prints every token's claims as it hears them."""

    def __init__(self, address: str) -> None:
        self.address = address
        self.issuer = f"{address}/realms/{REALM}"
        self._http = httpx.Client(timeout=15.0, follow_redirects=False)

    def close(self) -> None:
        self._http.close()

    def settings(self) -> OidcSettings:
        """Central's settings for this realm: the role and its claim are the defaults."""
        return OidcSettings(issuer=self.issuer, client_id=CLIENT_ID, client_secret=secret_of(CLIENT_ID),
                            name=PROVIDER_NAME)

    # ---- the realm's token endpoint ----

    @property
    def token_endpoint(self) -> str:
        return f"{self.issuer}/protocol/openid-connect/token"

    def _token(self, client_id: str, form: Mapping[str, str]) -> dict[str, Any]:
        response = self._http.post(self.token_endpoint, data=dict(form), auth=(client_id, secret_of(client_id)))
        assert response.status_code == 200, (client_id, form.get("grant_type"), response.status_code, response.text)
        tokens = response.json()
        for kind in ("access_token", "id_token"):
            if kind in tokens:
                # Printed as heard: pytest shows a failed test its output. Claims only, no signature.
                print(f"keycloak: {client_id} {form['grant_type']} {kind}: header "
                      f"{json.dumps(header_of(tokens[kind]), sort_keys=True)} claims "
                      f"{json.dumps(claims_of(tokens[kind]), sort_keys=True)}")
        return tokens

    def service_token(self, client_id: str) -> str:
        """A service account's access token (the client credentials grant)."""
        return self._token(client_id, {"grant_type": "client_credentials"})["access_token"]

    # ---- the admin REST API ----

    def _admin(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        granted = self._http.post(
            f"{self.address}/realms/master/protocol/openid-connect/token",
            data={"grant_type": "password", "client_id": "admin-cli", "username": ADMIN_USER,
                  "password": ADMIN_PASSWORD})
        assert granted.status_code == 200, ("the administrator's token", granted.status_code, granted.text)
        response = self._http.request(
            method, f"{self.address}/admin/realms/{REALM}{path}",
            headers={"Authorization": "Bearer " + granted.json()["access_token"]}, **kwargs)
        assert response.status_code < 300, (method, path, response.status_code, response.text)
        return response

    def user_id(self, username: str) -> str:
        (user,) = self._admin("GET", "/users", params={"username": username, "exact": "true"}).json()
        return user["id"]

    def _role_mapping(self, username: str) -> tuple[str, list[dict[str, Any]]]:
        (client,) = self._admin("GET", "/clients", params={"clientId": CLIENT_ID}).json()
        role = self._admin("GET", f"/clients/{client['id']}/roles/{ROLE}").json()
        return (f"/users/{self.user_id(username)}/role-mappings/clients/{client['id']}",
                [{"id": role["id"], "name": role["name"]}])

    def grant_role(self, username: str) -> None:
        path, roles = self._role_mapping(username)
        self._admin("POST", path, json=roles)

    def remove_role(self, username: str) -> None:
        path, roles = self._role_mapping(username)
        self._admin("DELETE", path, json=roles)

    def sessions(self, username: str) -> list[dict[str, Any]]:
        return self._admin("GET", f"/users/{self.user_id(username)}/sessions").json()

    def log_out(self, username: str) -> None:
        """An administrator's "Sign out" of every session of a user: Keycloak tells each client
        that has a back-channel logout address."""
        self._admin("POST", f"/users/{self.user_id(username)}/logout")

    def as_imported(self) -> None:
        """The realm as its file states it, whatever an earlier test left: nobody signed in, and
        the role where the file grants it."""
        for username in (OWNER_USER, NO_ROLE_USER):
            self.log_out(username)
        self.grant_role(OWNER_USER)
        self.remove_role(NO_ROLE_USER)


class Central:
    """Central's own app (`create_app`, the production OpenID Connect client, the system clock)
    under uvicorn at `CENTRAL_ORIGIN`, with no break-glass token: only Keycloak lets anyone in."""

    origin: Final = CENTRAL_ORIGIN

    def __init__(self, database: Database, keycloak: Keycloak) -> None:
        self._app = create_app(database, SystemClock(), None, run_scheduler=False, oidc=keycloak.settings())
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Central:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # one test after another, one port
        try:
            listener.bind((os.environ.get(BIND_VARIABLE, "0.0.0.0"), CENTRAL_PORT))
        except OSError as busy:
            listener.close()
            raise RuntimeError(f"Central's port under Keycloak, {CENTRAL_PORT}, is not free "
                               f"(tests/integration/keycloak/realm.json names it): {busy}") from None
        self._server = uvicorn.Server(uvicorn.Config(self._app, log_level="warning"))
        self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [listener]}, daemon=True)
        self._thread.start()
        until(lambda: self._server.started or not self._thread.is_alive(), START_SECONDS, "Central started")
        if not self._server.started:
            raise RuntimeError("Central did not start under uvicorn")
        return self

    def __exit__(self, *exc: object) -> None:
        if self._server is not None and self._thread is not None:
            self._server.should_exit = True
            self._thread.join(timeout=START_SECONDS)


class _LoopbackJar(http.cookiejar.CookieJar):
    """Keycloak marks its cookies `Secure` whatever the scheme, and a browser sends a `Secure`
    cookie back to loopback over http (loopback is a secure context); Python's jar would not.
    So this jar keeps each cookie without the mark: every address here is loopback."""

    def set_cookie(self, cookie: http.cookiejar.Cookie) -> None:
        cookie.secure = False
        super().set_cookie(cookie)


class Browser:
    """A browser as far as sign-in needs one: an HTTP client that follows no redirect by itself
    and keeps one cookie jar, so Keycloak's cookies and Central's each go back where they came
    from (the two differ by path: cookies ignore the port). It prints what it did as it does it: pytest
    shows a failed test its output."""

    def __init__(self) -> None:
        self.http = httpx.Client(timeout=15.0, follow_redirects=False,
                                 cookies=_LoopbackJar())

    def close(self) -> None:
        self.http.close()

    def _go(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        response = self.http.request(method, url, **kwargs)
        # Addresses without their query: a code, a state and an ID token travel there.
        print(f"browser: {method} {url.split('?', 1)[0]} -> {response.status_code}"
              + (f" to {_without_secrets(response.headers['location'])}" if response.is_redirect else ""))
        return response

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._go("GET", url, **kwargs)

    def start(self, return_to: str = "#/frames") -> httpx.Response:
        """`GET session/start`, then Keycloak's answer to the address it sends the browser to: the
        login page (200), or straight back to Central (302) when Keycloak still knows the browser."""
        started = self.get(CENTRAL_ORIGIN + START_PATH, params={"origin": CENTRAL_ORIGIN, "return": return_to})
        assert started.status_code == 302, (started.status_code, started.text)
        assert started.headers["location"].startswith("http"), started.headers["location"]
        return self.get(started.headers["location"])

    def _log_in(self, page: httpx.Response, username: str) -> str:
        """Type the username and password into Keycloak's login page: the address Keycloak sends
        the browser back to (Central's callback, with the code)."""
        assert shows_login_form(page), (
            f"Keycloak showed no login page: {page.status_code} {page.headers.get('location')} {_text_of(page)[:600]}")
        submitted = self._go("POST", login_form_action(page), data={
            "username": username, "password": password_of(username), "credentialId": ""})
        assert submitted.status_code == 302, (
            f"Keycloak did not take the password: {submitted.status_code} {_text_of(submitted)[:600]}")
        back = submitted.headers["location"]
        assert back.startswith(CENTRAL_ORIGIN + CALLBACK_PATH + "?"), back.split("?", 1)[0]
        return back

    def sign_in(self, username: str, return_to: str = "#/frames") -> httpx.Response:
        """The whole code flow as a person does it: Central's answer to the redirect back (302 to
        the console, with the session cookie in the jar when it signed in)."""
        return self.get(self._log_in(self.start(return_to), username))

    def tokens_as_central_gets_them(self, keycloak: Keycloak, username: str) -> dict[str, Any]:
        """The same code flow with the test in Central's place (its client, its redirect address,
        PKCE S256, `scope=openid`), so the tokens Keycloak hands Central for a person can be read:
        Central itself never shows them."""
        verifier = secrets.token_urlsafe(32)
        redirect = CENTRAL_ORIGIN + CALLBACK_PATH
        page = self.get(f"{keycloak.issuer}/protocol/openid-connect/auth", params={
            "response_type": "code", "scope": "openid", "client_id": CLIENT_ID, "redirect_uri": redirect,
            "state": secrets.token_urlsafe(16), "nonce": secrets.token_urlsafe(16),
            "code_challenge": code_challenge(verifier), "code_challenge_method": "S256"})
        (code,) = parse_qs(urlsplit(self._log_in(page, username)).query)["code"]
        return keycloak._token(CLIENT_ID, {"grant_type": "authorization_code", "code": code,
                                           "redirect_uri": redirect, "code_verifier": verifier})

    def session(self) -> httpx.Response:
        """`GET /v1/operator/session` with the jar's cookies: who am I."""
        return self.get(CENTRAL_ORIGIN + "/v1/operator/session")

    def central(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """A console's request to Central: marked, from the origin that signed in."""
        return self._go(method, CENTRAL_ORIGIN + path, headers={**MARK, "Origin": CENTRAL_ORIGIN}, **kwargs)


def login_form_action(page: httpx.Response) -> str:
    """Where Keycloak's login page posts the username and password."""
    form = _LOGIN_FORM.search(page.text)
    assert form is not None, f"no login form in Keycloak's page: {_text_of(page)[:600]}"
    action = _ACTION.search(form.group(0))
    assert action is not None, form.group(0)
    return urljoin(str(page.url), html.unescape(action.group(1)))


def shows_login_form(page: httpx.Response) -> bool:
    return page.status_code == 200 and _LOGIN_FORM.search(page.text) is not None


def _without_secrets(address: str) -> str:
    """An address for a test's output: its query's names, and the values only of those that say
    how a sign-in ended. A code, a state and an ID token travel in queries."""
    parts = urlsplit(address)
    said = [f"{name}={values[0]}" if name in ("sign_in", "error", "error_description") else name
            for name, values in parse_qs(parts.query).items()]
    return (address.split("?", 1)[0].split("#", 1)[0] + ("?" + "&".join(said) if said else "")
            + ("#" + parts.fragment if parts.fragment else ""))


def _text_of(response: httpx.Response) -> str:
    """A page's words without its markup, for an assertion's message."""
    return " ".join(re.sub(r"<(script|style)\b.*?</\1>|<[^>]+>", " ", response.text, flags=re.S).split())


@contextlib.contextmanager
def browser() -> Iterator[Browser]:
    opened = Browser()
    try:
        yield opened
    finally:
        opened.close()
