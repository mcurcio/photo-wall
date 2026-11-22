"""Operator sign-in through an identity provider (decision 0035), at the HTTP boundary against the
production `create_app` with `FakeProvider` in process (tests/operator_provider_fake.py; held to
the production client's behaviour by tests/test_operator_provider_conformance.py).

The code flow end to end, what a session row holds, who a write names, staying judged (refresh,
refusal, a silent provider, the hard end), back-channel logout, Log out, the Bearer order with and
without the break-glass token, and token mode unchanged. Cookies are sent as explicit `Cookie`
headers (the client jar is cleared) so each test states exactly what the browser would send.
Central and the provider read one ManualClock.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from operator_provider_fake import CLIENT_ID, CLIENT_SECRET, FakeProvider

from central import actor
from central.app import create_app
from central.infra.operator_session_store import PgOperators, PgOperatorSessions, SessionRetention
from central.operator_access import policy
from central.operator_access.model import Rejudged, SessionSecrets
from central.operator_auth import (
    ACCESS_PATH,
    BACKCHANNEL_LOGOUT_PATH,
    CALLBACK_PATH,
    SESSION_PATH,
    SIGN_IN_PATH,
    START_PATH,
)
from central.operator_session import SessionCodec

ADMIN = "test-operator-" + "x" * 40
ORIGIN = "http://testserver"
MARK = {"X-Photo-Wall-Console": "1"}
HOUSE = "/v1/operator/house"
READ = "/v1/operator/inventory"
HOUSE_BODY = {"time": {"zone": "Europe/London", "place": None}, "units": "in", "clock": "12h"}
SESSION_COOKIE = re.compile(r"photo_wall_session=([A-Za-z0-9_-]{43}); Path=/v1/operator/; Max-Age=2592000; "
                            r"HttpOnly; SameSite=Strict")


def rows(registry, query, *params):
    with registry.db.transaction() as conn:
        return conn.execute(query, params).fetchall()


@pytest.fixture
def fake(registry):
    provider = FakeProvider(clock=registry.clock)
    provider.add_user("user-1", "Ada Owner")
    return provider


def app_of(registry, fake, admin_token=ADMIN, **settings):
    return create_app(registry.db, registry.clock, admin_token, run_scheduler=False,
                      oidc=fake.settings(**settings), identity_provider=fake)


@pytest.fixture
def client(registry, fake):
    with TestClient(app_of(registry, fake)) as client:
        yield client


def start(client, origin=ORIGIN, return_to="#/frames"):
    """`GET session/start`: (the response, the transaction cookie header to send back)."""
    params = {"origin": origin} | ({} if return_to is None else {"return": return_to})
    response = client.get(START_PATH, params=params, follow_redirects=False)
    client.cookies.clear()
    cookie = next((header.split(";", 1)[0] for header in response.headers.get_list("set-cookie")), None)
    return response, cookie


def come_back(client, back: str, transaction_cookie: str | None):
    """The provider's redirect back, followed as the browser does: `GET session/callback`."""
    parts = urlsplit(back)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == ORIGIN + CALLBACK_PATH
    headers = {} if transaction_cookie is None else {"Cookie": transaction_cookie}
    response = client.get(f"{parts.path}?{parts.query}", headers=headers, follow_redirects=False)
    client.cookies.clear()
    return response


def sign_in(client, fake, return_to="#/frames"):
    """The whole code flow: (the callback's response, the session's `Cookie` header)."""
    started, transaction_cookie = start(client, return_to=return_to)
    assert started.status_code == 302, started.text
    response = come_back(client, fake.approve(started.headers["location"]), transaction_cookie)
    issued = [header for header in response.headers.get_list("set-cookie") if header.startswith("photo_wall_session=")
              and "Max-Age=0" not in header]
    return response, ({"Cookie": issued[0].split(";", 1)[0]} if issued else None)


def signed_in(client, fake) -> dict[str, str]:
    response, cookie = sign_in(client, fake)
    assert response.headers["location"] == "/#/frames" and cookie is not None, response.headers
    return cookie


def write(client, cookie, **headers):
    return client.put(HOUSE, json=HOUSE_BODY, headers={**cookie, **MARK, "Origin": ORIGIN, **headers})


def session_rows(registry):
    return rows(registry, "SELECT * FROM operator_sessions ORDER BY created_at")


# ---- how to sign in ----

def test_central_says_how_to_sign_in_without_a_login(registry, client):
    response = client.get(SIGN_IN_PATH)
    assert response.status_code == 200 and response.json() == {"method": "provider", "name": "Fake ID"}
    assert response.headers["cache-control"] == "no-store"
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as token_mode:
        assert token_mode.get(SIGN_IN_PATH).json() == {"method": "token"}


# ---- the code flow ----

def test_start_sends_the_browser_to_the_provider_with_state_nonce_and_pkce(client, fake):
    response, transaction_cookie = start(client)
    assert response.status_code == 302 and response.headers["cache-control"] == "no-store"
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == fake.issuer + "/authorize"
    query = parse_qs(location.query)
    assert {key: query[key] for key in ("response_type", "scope", "client_id", "redirect_uri",
                                         "code_challenge_method")} == {
        "response_type": ["code"], "scope": ["openid"], "client_id": [CLIENT_ID],
        "redirect_uri": [ORIGIN + CALLBACK_PATH], "code_challenge_method": ["S256"]}
    state, nonce, challenge = query["state"][0], query["nonce"][0], query["code_challenge"][0]
    assert len({state, nonce, challenge}) == 3 and all(len(value) == 43 for value in (state, nonce, challenge))
    (header,) = response.headers.get_list("set-cookie")
    assert re.fullmatch(r"photo_wall_sign_in=[A-Za-z0-9_-]+; Path=/v1/operator/session/; Max-Age=600; "
                        r"HttpOnly; SameSite=Lax", header), header
    # The cookie is sealed: nothing of the transaction is readable in it, and it differs each time.
    assert state not in header and nonce not in header
    assert start(client)[1] != transaction_cookie


def test_an_https_console_gets_secure_prefixed_cookies(registry, fake):
    with TestClient(app_of(registry, fake), base_url="https://wall.example") as client:
        response = client.get(START_PATH, params={"origin": "https://wall.example"}, follow_redirects=False)
        client.cookies.clear()
        (header,) = response.headers.get_list("set-cookie")
        assert header.startswith("__Secure-photo_wall_sign_in=") and "; Secure;" in header
        back = urlsplit(fake.approve(response.headers["location"]))
        assert back.geturl().startswith("https://wall.example" + CALLBACK_PATH)
        landed = client.get(f"{back.path}?{back.query}", headers={"Cookie": header.split(";", 1)[0]},
                            follow_redirects=False)
        issued = [h for h in landed.headers.get_list("set-cookie") if h.startswith("__Secure-photo_wall_session=")
                  and "Max-Age=0" not in h]
        assert len(issued) == 1 and issued[0].endswith("; Secure; HttpOnly; SameSite=Strict")
        client.cookies.clear()
        cookie = {"Cookie": issued[0].split(";", 1)[0]}
        assert client.get(SESSION_PATH, headers=cookie).json()["name"] == "Ada Owner"


@pytest.mark.parametrize("origin", [None, "", "null", "testserver", "ftp://testserver", "http://testserver/path",
                                    "http://testserver?x=1", "http://test server", "http://" + "a" * 300])
def test_start_refuses_an_origin_a_session_cannot_be_bound_to(client, fake, origin):
    params = {} if origin is None else {"origin": origin}
    response = client.get(START_PATH, params=params, follow_redirects=False)
    assert (response.status_code, response.json()) == (403, {"error": "origin_mismatch"})
    assert response.headers.get_list("set-cookie") == []


def test_the_code_flow_signs_a_person_in_and_lands_on_the_return_route(registry, client, fake):
    response, cookie = sign_in(client, fake, return_to="#/frames/portrait?tab=power")
    assert response.status_code == 302
    assert response.headers["location"] == "/#/frames/portrait?tab=power"
    assert response.headers["cache-control"] == "no-store"
    headers = response.headers.get_list("set-cookie")
    (issued,) = [header for header in headers if SESSION_COOKIE.fullmatch(header)]
    # The transaction is over: its cookie is cleared under both names.
    assert "photo_wall_sign_in=; Path=/v1/operator/session/; Max-Age=0; HttpOnly; SameSite=Lax" in headers
    assert ("__Secure-photo_wall_sign_in=; Path=/v1/operator/session/; Max-Age=0; Secure; HttpOnly; "
            "SameSite=Lax") in headers
    assert not any("Domain" in header for header in headers)

    who = client.get(SESSION_PATH, headers=cookie)
    assert who.status_code == 200
    assert who.json() == {"method": "provider", "name": "Ada Owner", "roles": ["owner"], "provider": "Fake ID"}
    assert client.get(READ, headers=cookie).status_code == 200

    (operator,) = rows(registry, "SELECT * FROM operators")
    assert actor.is_operator_id(operator["operator_id"]) and "user-1" not in operator["operator_id"]
    assert (operator["issuer"], operator["subject"], operator["kind"], operator["name"]) == (
        fake.issuer, "user-1", "person", "Ada Owner")
    assert (operator["created_by"], operator["changed_by"], operator["created_at"]) == ("central", "central", 1000)

    (row,) = session_rows(registry)
    value = SESSION_COOKIE.fullmatch(issued).group(1)
    assert row["session_sha256"] == hashlib.sha256(value.encode()).hexdigest()
    assert value not in str(dict(row))                       # the row never holds the cookie's value
    assert (row["operator_id"], row["origin"], row["roles"]) == (operator["operator_id"], ORIGIN, ["owner"])
    assert (row["fresh_until"], row["hard_end"]) == (1000 + 300, 1000 + 30 * 24 * 3600)
    assert row["provider_session"] and row["refresh_token"] == fake.issued[-1]["refresh_token"]
    assert row["created_by"] == row["changed_by"] == f"person:{operator['operator_id']}"


def test_the_code_flow_through_the_production_client_over_http(registry):
    """The same flow with nothing injected but the settings: Central's own OpenID Connect client
    (central/infra/oidc_provider.py) asks the provider over HTTP, as the browser tests' Central does."""
    import httpx

    provider = FakeProvider(clock=registry.clock)
    provider.add_user("user-1", "Ada Owner")
    provider.add_service_account("reader-script")
    with provider.serving():
        app = create_app(registry.db, registry.clock, None, run_scheduler=False, oidc=provider.settings())
        with TestClient(app) as client:
            assert client.get(SIGN_IN_PATH).json() == {"method": "provider", "name": "Fake ID"}
            started, transaction_cookie = start(client)
            authorize = httpx.get(started.headers["location"], follow_redirects=False)
            landed = come_back(client, authorize.headers["location"], transaction_cookie)
            assert landed.headers["location"] == "/#/frames"
            (value,) = [match.group(1) for header in landed.headers.get_list("set-cookie")
                        if (match := SESSION_COOKIE.fullmatch(header))]
            cookie = {"Cookie": f"photo_wall_session={value}"}
            assert client.get(SESSION_PATH, headers=cookie).json()["name"] == "Ada Owner"
            assert write(client, cookie).status_code == 200
            registry.clock.advance(300)
            assert client.get(SESSION_PATH, headers=cookie).status_code == 200      # refreshed over HTTP
            assert [request["grant_type"] for request in provider.token_requests] == [
                "authorization_code", "refresh_token"]
            token = httpx.post(provider.issuer + "/token", data={"grant_type": "client_credentials"},
                               auth=("reader-script", "script-secret")).json()["access_token"]
            assert client.get(READ, headers={"Authorization": "Bearer " + token}).status_code == 200
            access = client.get(ACCESS_PATH, headers=cookie).json()["provider"]
            assert (access["issuer_host"], access["reachable"]) == ("127.0.0.1", True)
            provider.backchannel_url = "http://testserver" + BACKCHANNEL_LOGOUT_PATH
            sid = session_rows(registry)[0]["provider_session"]
            assert provider.send_backchannel_logout(sid=sid, post=client.post).status_code == 200
            assert client.get(SESSION_PATH, headers=cookie).status_code == 401


@pytest.mark.parametrize("return_to", [None, "", "/v1/operator/inventory", "https://evil.test/", "//evil.test",
                                       "#frames", "#/a\r\nSet-Cookie: x=1"])
def test_a_return_that_is_not_a_console_hash_route_lands_on_home(client, fake, return_to):
    response, cookie = sign_in(client, fake, return_to=return_to)
    assert response.headers["location"] == "/" and cookie is not None


def test_a_replayed_state_or_code_is_refused(registry, client, fake):
    started, transaction_cookie = start(client)
    back = fake.approve(started.headers["location"])
    first = come_back(client, back, transaction_cookie)
    assert first.headers["location"] == "/#/frames"
    # The same redirect again, with the transaction cookie a thief kept: the code was used.
    replayed = come_back(client, back, transaction_cookie)
    assert replayed.status_code == 302 and replayed.headers["location"] == "/?sign_in=failed"
    assert not any(SESSION_COOKIE.fullmatch(header) for header in replayed.headers.get_list("set-cookie"))
    # As the browser replays it: the transaction cookie was cleared.
    assert come_back(client, back, None).headers["location"] == "/?sign_in=failed"
    assert len(session_rows(registry)) == 1


def test_a_callback_for_another_transaction_is_refused(registry, client, fake):
    first, first_cookie = start(client)
    second, second_cookie = start(client)
    back_first = fake.approve(first.headers["location"])
    back_second = fake.approve(second.headers["location"])
    # A code and state from one sign-in, with the other's cookie (a login CSRF's shape).
    assert come_back(client, back_first, second_cookie).headers["location"] == "/?sign_in=failed"
    state = parse_qs(urlsplit(back_second).query)["state"][0]
    code = parse_qs(urlsplit(back_first).query)["code"][0]
    mixed = f"{ORIGIN}{CALLBACK_PATH}?code={code}&state={state}"
    assert come_back(client, mixed, second_cookie).headers["location"] == "/?sign_in=failed"
    for query in ("", "?state=x", "?code=x", "?error=access_denied&state=x"):
        assert come_back(client, ORIGIN + CALLBACK_PATH + query, first_cookie).headers["location"] \
            == "/?sign_in=failed"
    for cookie in ("photo_wall_sign_in=garbage", "photo_wall_sign_in=", first_cookie[:-3] + "AAA"):
        assert come_back(client, back_second, cookie).headers["location"] == "/?sign_in=failed"
    assert session_rows(registry) == [] and rows(registry, "SELECT 1 FROM operators") == []


def test_a_transaction_lasts_ten_minutes(registry, client, fake):
    started, transaction_cookie = start(client)
    back = fake.approve(started.headers["location"])
    registry.clock.advance(600)
    assert come_back(client, back, transaction_cookie).headers["location"] == "/?sign_in=failed"
    assert session_rows(registry) == []


def test_the_provider_declining_the_person_is_a_failed_sign_in(client, fake):
    fake.approves = None
    started, transaction_cookie = start(client)
    back = fake.approve(started.headers["location"])
    assert "error=access_denied" in back
    assert come_back(client, back, transaction_cookie).headers["location"] == "/?sign_in=failed"


def test_without_the_role_there_is_no_session(registry, client, fake):
    fake.add_user("user-2", "Guest Person", roles=())
    response, cookie = sign_in(client, fake)
    assert response.status_code == 302 and response.headers["location"] == "/?sign_in=not_allowed"
    assert cookie is None
    assert session_rows(registry) == [] and rows(registry, "SELECT 1 FROM operators") == []
    # A role Central does not know is no role either.
    fake.add_user("user-3", "Other Role", roles=("gardener",))
    assert sign_in(client, fake)[0].headers["location"] == "/?sign_in=not_allowed"


def test_an_access_token_not_addressed_to_central_is_not_allowed_and_one_refused_otherwise_is_failed(
        registry, client, fake, monkeypatch, caplog):
    """Keycloak leaves Central's client out of `aud` for a principal without its role: the person
    signed in (the ID token stands) and is told they are not allowed, not that sign-in broke."""
    real = fake._tokens

    def without_the_client(subject, sid, nonce):
        return {**real(subject, sid, nonce), "access_token": fake.access_token(
            subject, sid, aud="account", resource_access={CLIENT_ID: {"roles": ["owner"]}})}

    monkeypatch.setattr(fake, "_tokens", without_the_client)
    with caplog.at_level(logging.WARNING, logger="central.operator_auth"):
        response, cookie = sign_in(client, fake)
    assert response.headers["location"] == "/?sign_in=not_allowed" and cookie is None
    assert "operator sign-in refused: not_allowed (token: audience)" in caplog.text
    assert session_rows(registry) == []


def test_the_role_and_its_claim_are_the_deployments_to_name(registry, fake):
    with TestClient(app_of(registry, fake, role="wall-admin")) as client:
        assert sign_in(client, fake)[0].headers["location"] == "/?sign_in=not_allowed"
        fake.grant_role("user-1", "wall-admin")
        response, cookie = sign_in(client, fake)
        assert response.headers["location"] == "/#/frames"
        assert client.get(SESSION_PATH, headers=cookie).json()["roles"] == ["owner"]
    with TestClient(app_of(registry, fake, roles_claim=("realm_access", "roles"))) as client:
        assert sign_in(client, fake)[0].headers["location"] == "/?sign_in=not_allowed"


def test_a_provider_that_does_not_answer_says_so_and_signs_nobody_in(registry, client, fake):
    fake.unreachable = True
    response, transaction_cookie = start(client)
    assert response.status_code == 302 and response.headers["location"] == "/?sign_in=provider_unreachable"
    assert transaction_cookie is None
    fake.unreachable = False
    started, transaction_cookie = start(client)
    back = fake.approve(started.headers["location"])
    fake.unreachable = True
    assert come_back(client, back, transaction_cookie).headers["location"] == "/?sign_in=provider_unreachable"
    assert session_rows(registry) == []


@pytest.mark.parametrize("forge, detail", [
    (lambda fake, body: body.update(id_token=fake.id_token("user-1", nonce="another", sid="s")), "token: nonce"),
    (lambda fake, body: body.update(id_token=fake.id_token("user-2", nonce=None, sid="s")), "token: nonce"),
    (lambda fake, body: body.update(access_token=fake.access_token("user-1", iss="https://evil.test")),
     "token: issuer"),
    (lambda fake, body: body.update(access_token=fake.access_token("user-1", exp=900)), "token: expired"),
    (lambda fake, body: body.update(access_token=fake.access_token("user-2")), "two subjects"),
    (lambda fake, body: body.pop("id_token"), "no ID token"),
])
def test_a_token_central_does_not_accept_signs_nobody_in(registry, client, fake, monkeypatch, caplog, forge, detail):
    fake.users["user-2"] = fake.users["user-1"].__class__("user-2", "Someone Else")
    real = fake._tokens

    def forged(subject, sid, nonce):
        body = real(subject, sid, nonce)
        forge(fake, body)
        return body

    monkeypatch.setattr(fake, "_tokens", forged)
    with caplog.at_level(logging.WARNING, logger="central.operator_auth"):
        response, cookie = sign_in(client, fake)
    assert response.headers["location"] == "/?sign_in=failed" and cookie is None
    assert session_rows(registry) == []
    assert f"operator sign-in refused: failed ({detail})" in caplog.text


# ---- who changed a record ----

def test_a_write_names_the_person_in_its_audit_field(registry, client, fake):
    cookie = signed_in(client, fake)
    assert write(client, cookie).status_code == 200
    (operator,) = rows(registry, "SELECT operator_id FROM operators")
    (house,) = rows(registry, "SELECT changed_by FROM singletons WHERE name='house'")
    assert house["changed_by"] == f"person:{operator['operator_id']}"
    assert actor.parse(house["changed_by"]).kind == "person"
    # A reader that shows "who" asks the operators repository for the name.
    assert PgOperators(registry.db, registry.clock).name_of(operator["operator_id"]) == "Ada Owner"
    assert PgOperators(registry.db, registry.clock).name_of("op_aaaaaaaaaaaaaaaa") is None


def test_a_service_accounts_bearer_token_names_the_client(registry, client, fake):
    fake.add_service_account("reader-script")
    bearer = {"Authorization": "Bearer " + fake.service_token("reader-script")}
    # Bearer decides alone: no marker, no Origin.
    assert client.put(HOUSE, json=HOUSE_BODY, headers=bearer).status_code == 200
    (operator,) = rows(registry, "SELECT * FROM operators")
    assert (operator["kind"], operator["subject"]) == ("client", "service-account-reader-script")
    (house,) = rows(registry, "SELECT changed_by FROM singletons WHERE name='house'")
    assert house["changed_by"] == f"client:{operator['operator_id']}"
    who = client.get(SESSION_PATH, headers=bearer).json()
    assert who == {"method": "provider", "name": "service-account-reader-script", "roles": ["owner"],
                   "provider": "Fake ID"}
    # A machine opens no session, and a known one with an unchanged name writes nothing more.
    assert session_rows(registry) == []
    registry.clock.advance(10)
    assert client.get(READ, headers=bearer).status_code == 200
    assert rows(registry, "SELECT changed_at FROM operators") == [{"changed_at": 1000}]


def test_a_provider_token_central_does_not_accept_is_401(registry, client, fake):
    fake.add_service_account("reader-script")
    fake.add_service_account("no-role-script", roles=())
    subject = "service-account-reader-script"
    roles = {"resource_access": {CLIENT_ID: {"roles": ["owner"]}}}
    refused = {
        "no role": fake.service_token("no-role-script"),
        "addressed to Central, no role": fake.access_token(subject, resource_access={}),
        "a role Central does not know": fake.access_token(subject, resource_access={CLIENT_ID: {"roles": ["gardener"]}}),
        "another audience": fake.access_token(subject, aud=["account"]),
        "another issuer": fake.access_token(subject, iss="https://evil.test/realms/home"),
        "expired": fake.access_token(subject, exp=1000 - 61),
        "not yet valid": fake.access_token(subject, nbf=1000 + 120),
        "an ID token": fake.id_token(subject, nonce="n", sid="s", **roles),
        "a logout token": fake.logout_token(sub=subject, **roles),
        "unsigned": fake.access_token(subject).rsplit(".", 1)[0] + ".",
        "not a token": "x" * 40,
    }
    for name, token in refused.items():
        response = client.get(READ, headers={"Authorization": "Bearer " + token})
        assert (response.status_code, response.json()) == (401, {"error": "unauthorized"}), name
    # Refused beside a valid cookie too: Bearer decides alone.
    cookie = signed_in(client, fake)
    both = {**cookie, "Authorization": "Bearer " + refused["another audience"]}
    assert client.get(READ, headers=both).status_code == 401
    assert client.get(READ, headers={"Authorization": "Bearer " + fake.service_token("reader-script")}).status_code == 200


# ---- the break-glass token ----

def test_the_break_glass_token_works_beside_a_provider_even_when_it_is_down(registry, client, fake):
    fake.unreachable = True
    bearer = {"Authorization": "Bearer " + ADMIN}
    assert client.put(HOUSE, json=HOUSE_BODY, headers=bearer).status_code == 200
    (house,) = rows(registry, "SELECT changed_by FROM singletons WHERE name='house'")
    assert house["changed_by"] == "api-token"
    assert client.get(SESSION_PATH, headers=bearer).json() == {
        "method": "token", "name": None, "roles": ["owner"], "provider": None}
    assert rows(registry, "SELECT 1 FROM operators") == []


def test_without_a_break_glass_token_only_the_provider_lets_anyone_in(registry, fake):
    fake.add_service_account("reader-script")
    with TestClient(app_of(registry, fake, admin_token=None)) as client:
        assert client.get(READ, headers={"Authorization": "Bearer " + ADMIN}).status_code == 401
        assert client.get(READ, headers={"Authorization": "Bearer " + fake.service_token()}).status_code == 200
        assert client.get(SESSION_PATH, headers=signed_in(client, fake)).status_code == 200


def test_the_token_never_signs_a_browser_in_when_a_provider_is_configured(registry, client):
    response = client.post(SESSION_PATH, json={"token": ADMIN}, headers={**MARK, "Origin": ORIGIN})
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})
    assert response.headers.get_list("set-cookie") == []
    # A cookie token mode would have minted for this very token is no session here.
    minted = SessionCodec(ADMIN, registry.clock).mint(ORIGIN)
    assert client.get(READ, headers={"Cookie": f"photo_wall_session={minted}"}).status_code == 401


# ---- a cookie write ----

@pytest.mark.parametrize("headers, code", [
    ({"X-Photo-Wall-Console": None}, "request_unmarked"),
    ({"Origin": None}, "origin_mismatch"),
    ({"Origin": "http://evil.test"}, "origin_mismatch"),
    ({"Origin": "https://testserver"}, "origin_mismatch"),
    ({"Origin": "null"}, "origin_mismatch"),
    ({"Sec-Fetch-Site": "cross-site"}, "origin_mismatch"),
    ({"Sec-Fetch-Site": "same-site"}, "origin_mismatch"),
])
def test_a_provider_sessions_write_keeps_pass_as_three_checks(registry, client, fake, headers, code):
    cookie = signed_in(client, fake)
    sent = {**cookie, **MARK, "Origin": ORIGIN, **headers}
    response = client.put(HOUSE, json=HOUSE_BODY, headers={k: v for k, v in sent.items() if v is not None})
    assert (response.status_code, response.json()) == (403, {"error": code})
    assert rows(registry, "SELECT changed_by FROM singletons WHERE name='house'") in ([], [{"changed_by": "central"}])
    assert write(client, cookie, **{"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert client.get(READ, headers=cookie).status_code == 200      # a read needs none of them


def test_a_role_without_the_permission_a_route_asks_for_is_403(registry, fake, monkeypatch):
    """The seam for a guest role (decision 0035): one row of the policy table, no route touched.
    Here the owner's row is cut down to show each dependency asks for its own permission."""
    from fastapi import Depends

    app = app_of(registry, fake)
    auth = app.state.operator_auth
    app.post("/v1/operator/test-skip")(lambda by=Depends(auth.operate): {"by": by})
    with TestClient(app) as client:
        cookie = signed_in(client, fake)
        fake.add_service_account("reader-script")
        bearer = {"Authorization": "Bearer " + fake.service_token("reader-script")}
        skip = client.post("/v1/operator/test-skip", headers={**cookie, **MARK, "Origin": ORIGIN})
        assert skip.status_code == 200 and skip.json()["by"].startswith("person:op_")
        assert client.post("/v1/operator/test-skip", headers=cookie).status_code == 403   # pass A's checks

        monkeypatch.setitem(policy.PERMISSIONS, "owner", frozenset({"read", "operate"}))
        assert client.get(READ, headers=cookie).status_code == 200
        for headers in ({**cookie, **MARK, "Origin": ORIGIN}, bearer):
            refused = client.put(HOUSE, json=HOUSE_BODY, headers=headers)
            assert (refused.status_code, refused.json()) == (403, {"error": "forbidden"})
            assert client.post("/v1/operator/test-skip", headers=headers).status_code == 200
        monkeypatch.setitem(policy.PERMISSIONS, "owner", frozenset({"read"}))
        assert client.post("/v1/operator/test-skip", headers=bearer).status_code == 403
        assert client.get(READ, headers=bearer).status_code == 200
        # The break-glass token holds every permission whatever the table says.
        assert client.put(HOUSE, json=HOUSE_BODY, headers={"Authorization": "Bearer " + ADMIN}).status_code == 200
        monkeypatch.setitem(policy.PERMISSIONS, "owner", frozenset())
        assert client.get(READ, headers=cookie).status_code == 403


# ---- no token leaves Central ----

def test_no_token_is_readable_through_any_route_answer_or_log(registry, client, fake, caplog):
    fake.add_service_account("reader-script")
    answers = []

    def said(response):
        answers.append(f"{response.status_code} {response.headers.multi_items()!r} {response.text}")
        return response

    with caplog.at_level(logging.DEBUG):
        started, transaction_cookie = start(client)
        said(started)
        back = fake.approve(started.headers["location"])
        landed = said(come_back(client, back, transaction_cookie))
        (cookie_value,) = [match.group(1) for header in landed.headers.get_list("set-cookie")
                           if (match := SESSION_COOKIE.fullmatch(header))]
        cookie = {"Cookie": f"photo_wall_session={cookie_value}"}
        for path in (SESSION_PATH, ACCESS_PATH, SIGN_IN_PATH, READ, HOUSE, "/healthz"):
            said(client.get(path, headers=cookie))
        said(write(client, cookie))
        registry.clock.advance(301)                                   # a refresh: new tokens
        said(client.get(SESSION_PATH, headers=cookie))
        machine_token = fake.service_token("reader-script")
        said(client.get(SESSION_PATH, headers={"Authorization": "Bearer " + machine_token}))
        said(client.get(SESSION_PATH, headers={"Authorization": "Bearer " + machine_token[:-2] + "xx"}))
        said(come_back(client, back, transaction_cookie))             # a refused sign-in, logged
        said(fake.send_backchannel_logout(sid="nobody's", post=client.post, url=BACKCHANNEL_LOGOUT_PATH))
        said(client.post(BACKCHANNEL_LOGOUT_PATH, data={"logout_token": machine_token}))
        (row,) = session_rows(registry)
        logged_out = client.delete(SESSION_PATH, headers={**cookie, **MARK})
    assert len(fake.issued) == 2 and fake.refresh_calls == 1
    everything = "\n".join(answers)
    secrets_never_told = [CLIENT_SECRET, machine_token, row["refresh_token"],
                          *(body[key] for body in fake.issued for key in ("access_token", "refresh_token"))]
    for secret in secrets_never_told:
        assert secret not in everything and secret not in caplog.text and secret not in logged_out.text
    # The ID token is told once, to the person's own browser: Log out's hint for the provider.
    for id_token in (body["id_token"] for body in fake.issued):
        assert id_token not in everything and id_token not in caplog.text
    assert row["id_token"] in logged_out.json()["end_session"]
    # The cookie's value is the browser's alone: told at sign-in, never logged or stored.
    assert cookie_value not in caplog.text and cookie_value not in str(dict(row))
    assert everything.count(cookie_value) == 1
    assert "operator signed in: op_" in caplog.text


# ---- staying judged ----

def test_a_fresh_session_asks_the_provider_nothing_and_writes_nothing(registry, client, fake):
    cookie = signed_in(client, fake)
    (before,) = session_rows(registry)
    registry.clock.advance(299)
    for _ in range(5):
        assert client.get(SESSION_PATH, headers=cookie).status_code == 200
    assert fake.refresh_calls == 0 and session_rows(registry) == [before]
    assert rows(registry, "SELECT changed_at FROM operators") == [{"changed_at": 1000}]


def test_a_stale_session_is_re_judged_with_its_refresh_token_once(registry, client, fake):
    cookie = signed_in(client, fake)
    (before,) = session_rows(registry)
    registry.clock.advance(300)
    for _ in range(3):
        assert client.get(SESSION_PATH, headers=cookie).status_code == 200
    assert fake.refresh_calls == 1
    (after,) = session_rows(registry)
    assert after["fresh_until"] == 1300 + 300 and after["hard_end"] == before["hard_end"]
    assert after["refresh_token"] == fake.issued[-1]["refresh_token"] != before["refresh_token"]
    assert after["id_token"] == fake.issued[-1]["id_token"]
    assert (after["changed_at"], after["changed_by"], after["created_at"]) == (1300, "central", 1000)
    # And again five minutes on, with the token the last refresh kept.
    registry.clock.advance(300)
    assert write(client, cookie).status_code == 200
    assert fake.refresh_calls == 2


def test_a_refresh_the_provider_refuses_ends_the_session_within_one_request(registry, client, fake):
    cookie = signed_in(client, fake)
    fake.disable("user-1")
    assert client.get(SESSION_PATH, headers=cookie).status_code == 200     # still fresh: nothing asked
    registry.clock.advance(300)
    response = client.get(SESSION_PATH, headers=cookie)
    assert (response.status_code, response.json()) == (401, {"error": "unauthorized"})
    assert session_rows(registry) == [] and fake.refresh_calls == 1
    assert write(client, cookie).status_code == 401
    assert fake.refresh_calls == 1                                         # a gone session asks nothing


def test_a_role_taken_away_ends_the_session_at_the_next_refresh(registry, client, fake):
    cookie = signed_in(client, fake)
    fake.drop_role("user-1")
    registry.clock.advance(300)
    assert client.get(READ, headers=cookie).status_code == 401
    assert session_rows(registry) == []


def test_the_providers_session_ending_ends_centrals_at_the_next_refresh(registry, client, fake):
    cookie = signed_in(client, fake)
    fake.end_session(session_rows(registry)[0]["provider_session"])
    registry.clock.advance(300)
    assert client.get(READ, headers=cookie).status_code == 401
    assert session_rows(registry) == []


def test_a_provider_that_does_not_answer_keeps_the_session_and_is_asked_again_a_minute_later(registry, client, fake):
    cookie = signed_in(client, fake)
    (before,) = session_rows(registry)
    registry.clock.advance(300)
    fake.unreachable = True
    assert client.get(SESSION_PATH, headers=cookie).status_code == 200
    assert write(client, cookie).status_code == 200
    registry.clock.advance(59)
    assert client.get(SESSION_PATH, headers=cookie).status_code == 200
    assert fake.refresh_calls == 1                       # one try, then none inside the minute
    assert session_rows(registry) == [before]            # and nothing written
    registry.clock.advance(1)
    assert client.get(SESSION_PATH, headers=cookie).status_code == 200
    assert fake.refresh_calls == 2
    # It answers again: the judgement is re-made, the role still there.
    fake.unreachable = False
    registry.clock.advance(60)
    assert client.get(SESSION_PATH, headers=cookie).status_code == 200
    assert fake.refresh_calls == 3 and session_rows(registry)[0]["fresh_until"] == 1420 + 300
    # Or it answers no: the minutes it was silent promised nothing.
    registry.clock.advance(300)
    fake.unreachable = True
    assert client.get(SESSION_PATH, headers=cookie).status_code == 200
    fake.unreachable = False
    fake.disable("user-1")
    registry.clock.advance(60)
    assert client.get(SESSION_PATH, headers=cookie).status_code == 401


def test_a_session_ends_thirty_days_after_sign_in_whatever_the_provider_says(registry, client, fake):
    cookie = signed_in(client, fake)
    registry.clock.advance(30 * 24 * 3600 - 1)
    assert client.get(SESSION_PATH, headers=cookie).status_code == 200    # refreshed: the provider still says yes
    assert fake.refresh_calls == 1
    registry.clock.advance(1)
    assert client.get(SESSION_PATH, headers=cookie).status_code == 401
    assert write(client, cookie).status_code == 401
    assert fake.refresh_calls == 1                                         # the hard end asks nobody
    # The row is the retention step's to delete, in its own transaction.
    assert len(session_rows(registry)) == 1
    retention = SessionRetention(registry.db.transaction, registry.clock)
    assert retention.prune() == {"operator_sessions": 1} and session_rows(registry) == []
    assert retention.prune() == {"operator_sessions": 0}


def test_retention_deletes_only_sessions_past_their_hard_end_in_bounded_batches(registry, client, fake):
    for _ in range(3):
        signed_in(client, fake)
        registry.clock.advance(10)
    kept = signed_in(client, fake)
    retention = SessionRetention(registry.db.transaction, registry.clock, batch_rows=2, batches=1)
    assert retention.prune() == {"operator_sessions": 0}
    registry.clock.advance(30 * 24 * 3600 - 10)        # the first three are past their end, the fourth is not
    assert retention.prune() == {"operator_sessions": 2}
    assert retention.prune() == {"operator_sessions": 1}
    assert retention.prune() == {"operator_sessions": 0}
    assert len(session_rows(registry)) == 1
    assert client.get(SESSION_PATH, headers=kept).status_code == 200


def test_one_refresh_per_session_at_a_time(registry, fake):
    """A console polls every few seconds from several requests at once: while one request's
    refresh is with the provider, the others carry on with the last judgement and ask nothing."""
    entered, release = threading.Event(), threading.Event()
    real_refresh = fake.refresh

    def slow_refresh(refresh_token):
        entered.set()
        assert release.wait(10)
        return real_refresh(refresh_token)

    fake.refresh = slow_refresh
    with TestClient(app_of(registry, fake)) as client:
        cookie = signed_in(client, fake)
        registry.clock.advance(300)
        with ThreadPoolExecutor(max_workers=8) as pool:
            first = pool.submit(lambda: client.get(SESSION_PATH, headers=cookie).status_code)
            assert entered.wait(10)
            others = [pool.submit(lambda: client.get(READ, headers=cookie).status_code) for _ in range(6)]
            assert [other.result(timeout=10) for other in others] == [200] * 6
            assert fake.refresh_calls == 0 and not first.done()
            release.set()
            assert first.result(timeout=10) == 200
        assert fake.refresh_calls == 1
        assert client.get(SESSION_PATH, headers=cookie).status_code == 200
        assert fake.refresh_calls == 1


# ---- back-channel logout ----

def logout(client, token):
    return client.post(BACKCHANNEL_LOGOUT_PATH, data={"logout_token": token})


def test_back_channel_logout_ends_the_named_session_at_once(registry, client, fake):
    first, second = signed_in(client, fake), signed_in(client, fake)
    sid = session_rows(registry)[0]["provider_session"]
    response = fake.send_backchannel_logout(sid=sid, post=client.post, url=BACKCHANNEL_LOGOUT_PATH)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert client.get(SESSION_PATH, headers=first).status_code == 401      # still "fresh": ended at once
    assert client.get(SESSION_PATH, headers=second).status_code == 200
    assert fake.refresh_calls == 0
    # The same notice again ends nothing more and is still answered 200.
    assert logout(client, fake.logout_token(sid=sid)).status_code == 200
    assert len(session_rows(registry)) == 1


def test_back_channel_logout_by_subject_ends_every_session_of_that_person(registry, client, fake):
    first, second = signed_in(client, fake), signed_in(client, fake)
    fake.add_user("user-2", "Bo Owner")
    other = signed_in(client, fake)
    assert logout(client, fake.logout_token(sub="user-1")).status_code == 200
    assert [client.get(SESSION_PATH, headers=cookie).status_code for cookie in (first, second, other)] == [401, 401, 200]
    assert logout(client, fake.logout_token(sub="nobody")).status_code == 200


def test_what_is_not_this_issuers_logout_token_is_400_and_ends_nothing(registry, client, fake):
    cookie = signed_in(client, fake)
    sid = session_rows(registry)[0]["provider_session"]
    stranger = FakeProvider(clock=registry.clock)
    stranger.rotate_keys()
    refused = {
        "another audience": fake.logout_token(sid=sid, aud="another-client"),
        "another issuer": fake.logout_token(sid=sid, iss="https://evil.test/realms/home"),
        "a nonce": fake.logout_token(sid=sid, nonce="n"),
        "no event": fake.logout_token(sid=sid, events={}),
        "no session or subject": fake.logout_token(),
        "expired": fake.logout_token(sid=sid, exp=1000 - 61),
        "an access token": fake.access_token("user-1", sid),
        "an ID token": fake.id_token("user-1", nonce=None, sid=sid),
        "another key": stranger.sign({**fake._times(120), "aud": CLIENT_ID, "sid": sid,
                                      "events": {policy.BACKCHANNEL_LOGOUT_EVENT: {}}}, key_id=fake.key_id),
        "unsigned": fake.logout_token(sid=sid).rsplit(".", 1)[0] + ".",
        "not a token": "x",
    }
    for name, token in refused.items():
        response = logout(client, token)
        assert (response.status_code, response.json()) == (400, {"error": "invalid_request"}), name
    assert client.post(BACKCHANNEL_LOGOUT_PATH).status_code == 400
    assert client.post(BACKCHANNEL_LOGOUT_PATH, data={"other": "x"}).status_code == 400
    assert client.post(BACKCHANNEL_LOGOUT_PATH, json={"logout_token": fake.logout_token(sid=sid)}).status_code == 400
    assert client.post(BACKCHANNEL_LOGOUT_PATH, content=b"logout_token=" + b"x" * 70_000).status_code == 400
    assert client.get(SESSION_PATH, headers=cookie).status_code == 200


# ---- Log out ----

def test_log_out_ends_the_session_and_answers_the_providers_end_session_address(registry, client, fake):
    cookie = signed_in(client, fake)
    (row,) = session_rows(registry)
    assert client.delete(SESSION_PATH, headers=cookie).status_code == 403     # unmarked, as pass A
    response = client.delete(SESSION_PATH, headers={**cookie, **MARK, "Origin": ORIGIN})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    address = urlsplit(response.json()["end_session"])
    assert f"{address.scheme}://{address.netloc}{address.path}" == fake.issuer + "/logout"
    assert parse_qs(address.query) == {"client_id": [CLIENT_ID], "id_token_hint": [row["id_token"]],
                                       "post_logout_redirect_uri": [ORIGIN + "/"]}
    assert response.headers.get_list("set-cookie") == [
        "__Secure-photo_wall_session=; Path=/v1/operator/; Max-Age=0; Secure; HttpOnly; SameSite=Strict",
        "photo_wall_session=; Path=/v1/operator/; Max-Age=0; HttpOnly; SameSite=Strict",
        "__Host-photo_wall_session=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict",
        "photo_wall_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict",
    ]
    assert session_rows(registry) == []
    assert client.get(SESSION_PATH, headers=cookie).status_code == 401
    # Logging out again, or with no session at all, still clears and answers no address.
    for headers in ({**cookie, **MARK}, MARK):
        again = client.delete(SESSION_PATH, headers=headers)
        assert (again.status_code, again.json()) == (200, {"end_session": None})


# ---- People and access ----

def test_people_and_access_reads_the_providers_check(registry, client, fake):
    assert client.get(ACCESS_PATH).status_code == 401
    cookie = signed_in(client, fake)
    registry.clock.advance(5)
    response = client.get(ACCESS_PATH, headers=cookie)
    assert response.status_code == 200
    assert response.json() == {"method": "provider", "session_cap_days": 30, "provider": {
        "name": "Fake ID", "issuer_host": "id.test", "reachable": True, "last_reached_at": 1005}}
    registry.clock.advance(5)
    fake.unreachable = True
    assert client.get(ACCESS_PATH, headers=cookie).json()["provider"] == {
        "name": "Fake ID", "issuer_host": "id.test", "reachable": False, "last_reached_at": 1005}


# ---- token mode ----

def test_token_mode_is_unchanged_and_keeps_no_operator(registry, fake):
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        for path in (START_PATH + "?origin=" + ORIGIN, CALLBACK_PATH + "?code=x&state=y"):
            assert client.get(path, follow_redirects=False).status_code == 404
        assert client.post(BACKCHANNEL_LOGOUT_PATH, data={"logout_token": "x"}).status_code == 404
        assert client.get(SESSION_PATH).status_code == 401
        # A provider's token is nothing here.
        assert client.get(READ, headers={"Authorization": "Bearer " + fake.access_token("user-1")}).status_code == 401
        signed = client.post(SESSION_PATH, json={"token": ADMIN}, headers={**MARK, "Origin": ORIGIN})
        client.cookies.clear()
        assert signed.status_code == 204
        cookie = {"Cookie": signed.headers.get_list("set-cookie")[0].split(";", 1)[0]}
        assert client.get(SESSION_PATH, headers=cookie).json() == {
            "method": "token", "name": None, "roles": ["owner"], "provider": None}
        assert client.get(SESSION_PATH, headers={"Authorization": "Bearer " + ADMIN}).json()["method"] == "token"
        assert client.get(ACCESS_PATH, headers=cookie).json() == {
            "method": "token", "provider": None, "session_cap_days": 30}
        assert write(client, cookie).status_code == 200
        assert client.put(HOUSE, json=HOUSE_BODY, headers=cookie).status_code == 403
        (house,) = rows(registry, "SELECT changed_by FROM singletons WHERE name='house'")
        assert house["changed_by"] == "console"
        logged_out = client.delete(SESSION_PATH, headers=MARK)
        assert logged_out.status_code == 204 and logged_out.content == b""
    assert rows(registry, "SELECT 1 FROM operators") == [] and session_rows(registry) == []


# ---- the two repositories ----

def test_an_operator_is_written_at_first_sight_and_when_its_name_changes(registry):
    operators = PgOperators(registry.db, registry.clock)
    first = operators.seen(issuer="https://id.test", subject="user-1", kind="person", name="Ada", by="central")
    registry.clock.advance(10)
    assert operators.seen(issuer="https://id.test", subject="user-1", kind="person", name="Ada", by="central") == first
    assert rows(registry, "SELECT created_at, changed_at FROM operators") == [{"created_at": 1000, "changed_at": 1000}]
    # The provider renamed them: one write; the id, and the kind first seen, stay.
    renamed = operators.seen(issuer="https://id.test", subject="user-1", kind="client", name="Ada L.", by="central")
    assert (renamed.operator_id, renamed.kind, renamed.name) == (first.operator_id, "person", "Ada L.")
    assert rows(registry, "SELECT created_at, changed_at FROM operators") == [{"created_at": 1000, "changed_at": 1010}]
    # The same subject at another issuer is somebody else.
    other = operators.seen(issuer="https://other.test", subject="user-1", kind="person", name="Ada", by="central")
    assert other.operator_id != first.operator_id
    assert operators.find(issuer="https://id.test", subject="user-1") == renamed
    assert operators.find(issuer="https://id.test", subject="nobody") is None


def test_a_re_judgement_is_written_only_by_the_refresh_that_holds_the_kept_token(registry, client, fake):
    signed_in(client, fake)
    sessions = PgOperatorSessions(registry.db, registry.clock)
    (row,) = session_rows(registry)
    session_id = row["session_sha256"]
    kept = sessions.secrets(session_id)
    assert kept == SessionSecrets(row["refresh_token"], row["id_token"])
    assert row["refresh_token"] not in repr(kept) and row["id_token"] not in repr(kept)
    registry.clock.advance(7)
    # The same judgement again changes nothing: nothing is written.
    same = Rejudged(frozenset({"owner"}), row["fresh_until"], kept)
    assert sessions.rejudged(session_id, was=kept, now=same, by="central") is True
    assert session_rows(registry) == [row]
    moved = Rejudged(frozenset({"owner"}), 9999.0, SessionSecrets("refresh-2", kept.id_token))
    assert sessions.rejudged(session_id, was=kept, now=moved, by="central") is True
    (after,) = session_rows(registry)
    assert (after["fresh_until"], after["refresh_token"], after["changed_at"]) == (9999.0, "refresh-2", 1007)
    # A refresh that lost the race holds a token the row no longer keeps: it writes nothing.
    late = Rejudged(frozenset(), 1.0, SessionSecrets("refresh-3", None))
    assert sessions.rejudged(session_id, was=kept, now=late, by="central") is False
    assert sessions.rejudged("no-such-session", was=kept, now=late, by="central") is False
    assert session_rows(registry) == [after]
    assert sessions.end(session_id) == SessionSecrets("refresh-2", kept.id_token)
    assert sessions.end(session_id) is None and sessions.find(session_id) is None
    with pytest.raises(ValueError):
        sessions.end_named()
