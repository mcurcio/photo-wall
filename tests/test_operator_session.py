"""Pass A (stay signed in): the operator sign-in, the `admin` dependency and the total codec.

Behaviour at the HTTP boundary against the production `create_app`: the Bearer rule, the signed
session cookie, the Origin binding of cookie writes, the exact cookie attributes, `no-store` on
every operator response and no CORS. Cookies are sent as explicit `Cookie` headers (the client jar
is cleared) so each test states exactly what the browser would send.

The scrypt parameters are production constants and are never lowered here: the per-process key
cache (keyed by token) is what keeps this module fast.
"""

import hashlib
import re
import urllib.request
from email.message import Message
from http.cookiejar import CookieJar

import pytest
from fastapi.testclient import TestClient

from central.app import create_app
from central.operator_auth import (
    COOKIE,
    LEGACY_HOST_COOKIE,
    OPERATOR_PREFIX,
    SECURE_COOKIE,
    SESSION_PATH,
)
from central.operator_session import SESSION_SECONDS, SessionCodec, session_key
from central.registry import FrameCreate
from contracts.models import FrameProfile
from contracts.time import ManualClock

ADMIN = "test-operator-" + "x" * 40
OTHER_ADMIN = "test-rotated-" + "y" * 40
ORIGIN = "http://testserver"
MARK = {"X-Photo-Wall-Console": "1"}
READ = "/v1/operator/inventory"
WRITE = "/v1/operator/frames/portrait"
SCOPED = "Path=/v1/operator/"
HTTP_LEGACY_CLEARS = [
    "__Host-photo_wall_session=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict",
    "photo_wall_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict",
]
HTTPS_LEGACY_CLEARS = [
    "__Host-photo_wall_session=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict",
    "photo_wall_session=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict",
]
VALUE = re.compile(r"v1\.[0-9]{10}\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{1,344}\.[A-Za-z0-9_-]{43}")


def _portrait(registry):
    registry.create_frame(FrameCreate(id="portrait", width_mm=300, height_mm=500,
                                      profile=FrameProfile(width_px=1080, height_px=1920,
                                                           diagonal_inches=24)))


@pytest.fixture
def client(registry):
    _portrait(registry)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        yield client


def sign_in(client, origin=ORIGIN, token=ADMIN, **headers):
    response = client.post(SESSION_PATH, json={"token": token},
                           headers={**MARK, "Origin": origin, **headers})
    client.cookies.clear()
    return response


def cookie_of(response):
    """(name, value) of the issued Set-Cookie (the first; the rest clear legacy cookies)."""
    header = response.headers.get_list("set-cookie")[0]
    name, _, rest = header.partition("=")
    return name, rest.split(";", 1)[0]


def jar(name_value):
    name, value = name_value
    return {"Cookie": f"{name}={value}"}


def signed_in(client, origin=ORIGIN):
    response = sign_in(client, origin)
    assert response.status_code == 204
    return jar(cookie_of(response))


def move(client, headers):
    return client.patch(WRITE, json={"x_mm": 10}, headers=headers)


# --- Tracer bullet (pass A §10).


def test_tracer_cookie_reads_writes_from_its_origin_and_dies_on_rotation(registry, client):
    cookie = signed_in(client)
    assert client.get(READ, headers=cookie).status_code == 200
    assert move(client, {**cookie, **MARK, "Origin": ORIGIN}).status_code == 200
    refused = move(client, {**cookie, **MARK, "Origin": "http://evil.example"})
    assert (refused.status_code, refused.json()) == (403, {"error": "origin_mismatch"})
    rotated = create_app(registry.db, registry.clock, OTHER_ADMIN, run_scheduler=False)
    with TestClient(rotated) as other:
        assert other.get(READ, headers=cookie).status_code == 401


# --- Issued and clearing headers.


def test_https_sign_in_issues_the_secure_prefixed_scoped_cookie(client):
    response = sign_in(client, "https://wall.example")
    assert response.status_code == 204
    assert response.headers["cache-control"] == "no-store"
    [header, *legacy] = response.headers.get_list("set-cookie")
    name, value = cookie_of(response)
    assert name == SECURE_COOKIE and VALUE.fullmatch(value)
    assert header == (f"__Secure-photo_wall_session={value}; Path=/v1/operator/; "
                      "Max-Age=2592000; Secure; HttpOnly; SameSite=Strict")
    assert "domain" not in header.lower()
    assert legacy == HTTPS_LEGACY_CLEARS


def test_http_sign_in_issues_the_plain_scoped_cookie_without_secure(client):
    response = sign_in(client)
    [header, *legacy] = response.headers.get_list("set-cookie")
    name, value = cookie_of(response)
    assert name == COOKIE
    assert header == (f"photo_wall_session={value}; Path=/v1/operator/; Max-Age=2592000; "
                      "HttpOnly; SameSite=Strict")
    assert legacy == HTTP_LEGACY_CLEARS


def test_log_out_clears_both_scoped_and_both_legacy_names_with_exact_attributes(client):
    response = client.delete(SESSION_PATH, headers={**MARK, "Origin": ORIGIN})
    assert response.status_code == 204
    assert response.headers["cache-control"] == "no-store"
    assert response.headers.get_list("set-cookie") == [
        "__Secure-photo_wall_session=; Path=/v1/operator/; Max-Age=0; Secure; HttpOnly; "
        "SameSite=Strict",
        "photo_wall_session=; Path=/v1/operator/; Max-Age=0; HttpOnly; SameSite=Strict",
        *HTTP_LEGACY_CLEARS,
    ]
    again = client.delete(SESSION_PATH, headers=MARK)  # idempotent, no Origin needed
    assert again.status_code == 204


def test_log_out_over_https_marks_the_plain_clearing_cookies_secure(client):
    response = client.delete(SESSION_PATH, headers={**MARK, "Origin": "https://wall.example"})
    assert response.headers.get_list("set-cookie") == [
        "__Secure-photo_wall_session=; Path=/v1/operator/; Max-Age=0; Secure; HttpOnly; "
        "SameSite=Strict",
        "photo_wall_session=; Path=/v1/operator/; Max-Age=0; Secure; HttpOnly; SameSite=Strict",
        *HTTPS_LEGACY_CLEARS,
    ]


class _AsUrllib:
    """An httpx response as the `http.cookiejar` response protocol (`info()` headers)."""

    def __init__(self, response):
        self.headers = Message()
        for header in response.headers.get_list("set-cookie"):
            self.headers["Set-Cookie"] = header

    def info(self):
        return self.headers


@pytest.mark.parametrize("origin", [ORIGIN, "https://testserver"])
def test_the_session_cookie_is_sent_only_under_the_operator_prefix(client, origin):
    # A standard cookie jar (RFC 6265 path-match) decides what a browser would send: the
    # console page, Player/media routes and any other server on this host never get it.
    response = client.post(SESSION_PATH, json={"token": ADMIN},
                           headers={**MARK, "Origin": origin})
    assert response.status_code == 204
    cookie_jar = CookieJar()
    cookie_jar.extract_cookies(_AsUrllib(response), urllib.request.Request(origin + SESSION_PATH))
    client.cookies.clear()
    [issued] = [c for c in cookie_jar if c.value]  # the legacy clears leave nothing behind
    assert issued.path == "/v1/operator/"

    def sent(path):
        request = urllib.request.Request(origin + path)
        cookie_jar.add_cookie_header(request)
        return request.get_header("Cookie")

    assert sent(READ) == f"{issued.name}={issued.value}"
    assert sent(SESSION_PATH) is not None
    for path in ("/", "/console", "/healthz", "/v1/media/abc", "/v1/player/config",
                 "/v1/operator", "/v1/operatorx/inventory", "/photos/api/assets"):
        assert sent(path) is None, path


def test_log_out_must_be_marked(client):
    response = client.delete(SESSION_PATH, headers={"Origin": ORIGIN})
    assert (response.status_code, response.json()) == (403, {"error": "request_unmarked"})
    assert response.headers.get_list("set-cookie") == []


# --- Sign-in refusals.


@pytest.mark.parametrize("token", ["wrong-token-" + "z" * 40, "éé", ADMIN + "x", ""])
def test_sign_in_with_a_wrong_token_is_401_without_a_cookie(client, token):
    response = sign_in(client, token=token)
    assert (response.status_code, response.json()) == (401, {"error": "unauthorized"})
    assert response.headers.get_list("set-cookie") == []
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("headers, code", [
    ({"Origin": ORIGIN}, "request_unmarked"),
    (MARK, "origin_mismatch"),
    ({**MARK, "Origin": "null"}, "origin_mismatch"),
    ({**MARK, "Origin": "file://"}, "origin_mismatch"),
    ({**MARK, "Origin": ORIGIN + "/path"}, "origin_mismatch"),
    ({**MARK, "Origin": "http://" + "a" * 260}, "origin_mismatch"),
    ({**MARK, "Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"}, "origin_mismatch"),
    ({**MARK, "Origin": ORIGIN, "Sec-Fetch-Site": "same-site"}, "origin_mismatch"),
])
def test_sign_in_refusals_are_403_without_a_cookie(client, headers, code):
    response = client.post(SESSION_PATH, json={"token": ADMIN}, headers=headers)
    assert (response.status_code, response.json()) == (403, {"error": code})
    assert response.headers.get_list("set-cookie") == []


def test_sign_in_with_same_origin_fetch_metadata_is_accepted(client):
    assert sign_in(client, **{"Sec-Fetch-Site": "same-origin"}).status_code == 204


@pytest.mark.parametrize("body", [{}, {"token": 5}, {"token": ADMIN, "extra": 1}, ["x"]])
def test_sign_in_with_an_invalid_body_is_422_and_never_echoes_it(client, body):
    response = client.post(SESSION_PATH, json=body, headers={**MARK, "Origin": ORIGIN})
    assert (response.status_code, response.json()) == (422, {"error": "invalid_request"})
    assert ADMIN not in response.text
    assert response.headers.get_list("set-cookie") == []


def test_sign_in_with_a_lone_surrogate_token_never_500s(client):
    response = client.post(SESSION_PATH, content=b'{"token": "\\ud800abc"}',
                           headers={**MARK, "Origin": ORIGIN, "Content-Type": "application/json"})
    assert response.status_code in (401, 422)
    assert response.headers.get_list("set-cookie") == []


# --- Cookie writes.


def test_cookie_reads_need_no_marker_or_origin(client):
    assert client.get(READ, headers=signed_in(client)).status_code == 200


@pytest.mark.parametrize("extra, code", [
    ({"Origin": ORIGIN}, "request_unmarked"),
    (MARK, "origin_mismatch"),  # a MISSING Origin is refused, as at sign-in
    ({**MARK, "Origin": "http://other.example"}, "origin_mismatch"),
    ({**MARK, "Origin": "https://testserver"}, "origin_mismatch"),
    ({**MARK, "Origin": ORIGIN, "Sec-Fetch-Site": "same-site"}, "origin_mismatch"),
    ({**MARK, "Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"}, "origin_mismatch"),
])
def test_cookie_writes_refused_unless_from_the_signed_in_page(client, extra, code):
    response = move(client, {**signed_in(client), **extra})
    assert (response.status_code, response.json()) == (403, {"error": code})
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("extra", [{}, {"Sec-Fetch-Site": "same-origin"}])
def test_cookie_write_from_the_signed_in_page_succeeds(client, extra):
    response = move(client, {**signed_in(client), **MARK, "Origin": ORIGIN, **extra})
    assert response.status_code == 200


def test_http_sign_in_then_https_same_host_reads_but_cannot_write(client):
    # The plain cookie is still sent over https to the same host (not Secure): reads work, and
    # every write is origin_mismatch until the operator signs in again at the https address.
    cookie = signed_in(client, "http://wall.local")
    https = {**cookie, "Origin": "https://wall.local"}
    assert client.get(READ, headers=https).status_code == 200
    response = move(client, {**https, **MARK})
    assert (response.status_code, response.json()) == (403, {"error": "origin_mismatch"})
    again = signed_in(client, "https://wall.local")
    assert move(client, {**again, **MARK, "Origin": "https://wall.local"}).status_code == 200


def test_the_secure_cookie_is_preferred_over_the_plain_one(client):
    secure = cookie_of(sign_in(client, "https://wall.local"))
    assert secure[0] == SECURE_COOKIE
    both = {"Cookie": f"{COOKIE}=forged; {SECURE_COOKIE}={secure[1]}"}
    assert client.get(READ, headers=both).status_code == 200
    # Present but invalid, the __Secure- cookie decides: no other name is consulted.
    plain = cookie_of(sign_in(client))
    for other in (COOKIE, LEGACY_HOST_COOKIE):
        shadowed = {"Cookie": f"{other}={plain[1]}; {SECURE_COOKIE}=forged"}
        assert client.get(READ, headers=shadowed).status_code == 401, other


def test_legacy_path_root_cookies_are_accepted_until_they_expire(registry, client):
    # Cookies minted before the Path scoping (same value format, Path=/) keep a signed-in
    # browser signed in across the upgrade; they are only ever cleared, never issued.
    value = cookie_of(sign_in(client))[1]
    for name in (LEGACY_HOST_COOKIE, COOKIE):
        legacy = {"Cookie": f"{name}={value}"}
        assert client.get(READ, headers=legacy).status_code == 200, name
        assert move(client, {**legacy, **MARK, "Origin": ORIGIN}).status_code == 200, name
    # The legacy __Host- name outranks the plain one, present but invalid.
    shadowed = {"Cookie": f"{COOKIE}={value}; {LEGACY_HOST_COOKIE}=forged"}
    assert client.get(READ, headers=shadowed).status_code == 401
    registry.clock.advance(SESSION_SECONDS)
    for name in (LEGACY_HOST_COOKIE, COOKIE):
        assert client.get(READ, headers={"Cookie": f"{name}={value}"}).status_code == 401


# --- Bearer decides alone.


def test_bearer_reads_and_writes_need_no_marker_or_origin(client):
    bearer = {"Authorization": "Bearer " + ADMIN}
    assert client.get(READ, headers=bearer).status_code == 200
    assert move(client, bearer).status_code == 200


def test_an_invalid_bearer_is_401_even_beside_a_valid_cookie(client):
    cookie = signed_in(client)
    response = client.get(READ, headers={**cookie, "Authorization": "Bearer wrong"})
    assert (response.status_code, response.json()) == (401, {"error": "unauthorized"})
    write = move(client, {**cookie, **MARK, "Origin": ORIGIN, "Authorization": "Bearer wrong"})
    assert write.status_code == 401


@pytest.mark.parametrize("authorization", ["Basic dXNlcjpwYXNz", "Bearer ", "Bearer", "Token x"])
def test_other_schemes_and_an_empty_bearer_fall_through_to_the_cookie(client, authorization):
    assert client.get(READ, headers={"Authorization": authorization}).status_code == 401
    cookie = signed_in(client)
    response = client.get(READ, headers={**cookie, "Authorization": authorization})
    assert response.status_code == 200
    # ...and the cookie's write rules then apply in full.
    assert move(client, {**cookie, "Authorization": authorization}).status_code == 403


@pytest.mark.parametrize("credential", ["éé".encode(), b"\xff\xfe", ("é" + ADMIN).encode()])
def test_a_non_ascii_bearer_is_401_not_500(client, credential):
    response = client.get(READ, headers={"Authorization": b"Bearer " + credential})
    assert (response.status_code, response.json()) == (401, {"error": "unauthorized"})


# --- The codec is total: every refusal is 401.


def _codec(registry, offset=0.0):
    return SessionCodec(ADMIN, ManualClock(registry.clock.utc() + offset))


def _forge(value, part, replacement):
    parts = value.split(".")
    parts[part] = replacement
    return ".".join(parts)


def test_codec_refusals_are_all_401(registry, client):
    good = cookie_of(sign_in(client))[1]
    far = _codec(registry, offset=100).mint(ORIGIN)  # right key, expiry beyond now + 30 days
    refused = {
        "malformed": "garbage",
        "empty": "",
        "oversized": good + "A" * 600,
        "oversized_origin": _forge(good, 3, "A" * 345),
        "tampered_origin": _forge(good, 3, "aHR0cDovL2V2aWwuZXhhbXBsZQ"),
        "tampered_expiry": _forge(good, 1, "9999999999"),
        "tampered_mac": _forge(good, 4, "A" * 43),
        "wrong_version": _forge(good, 0, "v2"),
        "unicode_digits": _forge(good, 1, "١٢٣٤٥٦٧٨٩٠"),
        "non_ascii": good[:-1] + "é",
        # Undecodable parts behind a wrong MAC: refused before any parse is attempted.
        "undecodable_origin": _forge(_forge(good, 3, "A"), 4, "B" * 43),
        "non_ascii_origin": _forge(_forge(good, 3, "_w"), 4, "B" * 43),
        "beyond_the_cap": far,
    }
    for name, value in refused.items():
        response = client.get(READ, headers={"Cookie": f"{COOKIE}={value}".encode()})
        assert (name, response.status_code, response.json()) == (
            name, 401, {"error": "unauthorized"})
    assert client.get(READ, headers={"Cookie": f"{COOKIE}={good}"}).status_code == 200


def test_a_session_expires_after_thirty_days(registry, client):
    cookie = signed_in(client)
    registry.clock.advance(SESSION_SECONDS - 1)
    assert client.get(READ, headers=cookie).status_code == 200
    registry.clock.advance(1)
    assert client.get(READ, headers=cookie).status_code == 401


def test_codec_verify_is_total_over_arbitrary_input(registry):
    codec = _codec(registry)
    minted = codec.mint("https://wall.example")
    session = codec.verify(minted)
    assert session is not None and session.origin == "https://wall.example"
    assert session.expires_at == int(registry.clock.utc()) + SESSION_SECONDS
    for value in (None, 5, b"v1", minted.encode(), "v1." + "." * 600, minted + "\x00",
                  minted[:-1] + "é", _forge(minted, 1, "١٢٣٤٥٦٧٨٩٠"),
                  _forge(minted, 2, "é" * 22), _forge(_forge(minted, 3, "A"), 4, "B" * 43)):
        assert codec.verify(value) is None
    assert codec.mint("null") is None


def test_session_key_is_scrypt_of_the_token_with_the_specified_parameters():
    expected = hashlib.scrypt(ADMIN.encode(), salt=b"photo-wall/operator-session/v1",
                              n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
    assert session_key(ADMIN.encode()) == expected
    assert session_key(OTHER_ADMIN.encode()) != expected


# --- no-store on every operator response; none of it leaks elsewhere.


def test_every_operator_response_is_no_store(client):
    bearer = {"Authorization": "Bearer " + ADMIN}
    responses = [
        client.get(READ, headers=bearer),                                  # 200
        client.get(READ),                                                  # 401
        move(client, signed_in(client)),                                   # 403
        client.patch(WRITE, json={"x_mm": "left"}, headers=bearer),        # 422 (validation)
        client.patch(WRITE, json={"width_mm": 600}, headers=bearer),       # 422 (RegistryError)
        client.get("/v1/operator/frames/nope", headers=bearer),            # 405
        client.get("/v1/operator/no-such-route", headers=bearer),          # 404
    ]
    assert [r.status_code for r in responses] == [200, 401, 403, 422, 422, 405, 404]
    assert all(r.headers.get("cache-control") == "no-store" for r in responses)


def test_an_unhandled_operator_500_is_no_store(registry, monkeypatch):
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    monkeypatch.setattr(app.state.registry, "inventory",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(READ, headers={"Authorization": "Bearer " + ADMIN})
        assert response.status_code == 500
        assert response.headers["cache-control"] == "no-store"
        assert "boom" not in response.text


# --- No CORS; Players never read cookies.


def test_no_cors_headers_on_preflight_or_cross_origin_requests(client):
    preflight = client.options(READ, headers={
        "Origin": "http://evil.example", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-photo-wall-console"})
    cross = client.get(READ, headers={"Origin": "http://evil.example",
                                      "Authorization": "Bearer " + ADMIN})
    signin = sign_in(client, "http://evil.example")
    for response in (preflight, cross, signin):
        assert not [h for h in response.headers if h.lower().startswith("access-control-")]


def test_players_ignore_operator_cookies(client):
    cookie = signed_in(client)
    for path in ("/v1/player/config", "/v1/player/state", "/v1/player/time", "/v1/media/abc"):
        assert client.get(path, headers=cookie).status_code == 401, path


def test_every_operator_route_but_the_session_routes_depends_on_admin(registry):
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    admin = app.state.operator_auth.admin
    routes = [r for r in app.routes if getattr(r, "path", "").startswith(OPERATOR_PREFIX)]
    guarded = {(r.path, tuple(sorted(r.methods))): any(d.call == admin
                                                        for d in r.dependant.dependencies)
               for r in routes}
    assert len(guarded) > 20
    for (path, methods), has_admin in guarded.items():
        assert has_admin == (path != SESSION_PATH), (path, methods)
    assert {methods for (path, methods) in guarded if path == SESSION_PATH} == {
        ("POST",), ("DELETE",)}
