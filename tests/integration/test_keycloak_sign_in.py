"""Operator sign-in against a real Keycloak (decision 0035, Acceptance and "Not verified"): the
production `create_app` with its own OpenID Connect client, under uvicorn, and the pinned Keycloak
of tests/integration/compose.keycloak.yml with the realm tests/integration/keycloak/realm.json
(the rig: integration/keycloak_rig.py). No browser: an HTTP client with a cookie jar walks the
code flow through Keycloak's own login page.

What it pins is what the record leans on in Keycloak, at the version the compose file names:
where an access token carries a client role, whom it is addressed to (`aud`, `azp`) with no
mapper of the realm's own, what Keycloak calls each token, that
a refresh re-judges the role, that an administrator's sign-out reaches Central by back-channel
logout, and that Keycloak takes the end-session address Central answers at Log out.

Skips with "set PHOTO_WALL_KEYCLOAK" where no Keycloak runs; software-e2e.yml's `keycloak` job
runs it. The tests share one realm and one Central port, so they run on one worker, in order
(tests/conftest.py); each starts from the realm as imported.
"""
from __future__ import annotations

import time
from urllib.parse import parse_qs, urlsplit

import pytest
from integration.keycloak_rig import (
    ACCESS_TOKEN_SECONDS,
    AGENT,
    CENTRAL_ORIGIN,
    CLIENT_ID,
    KEYCLOAK_VARIABLE,
    NO_ROLE_USER,
    OWNER_USER,
    PROVIDER_NAME,
    ROLE,
    SKIP_REASON,
    STRANGER,
    Browser,
    Central,
    Keycloak,
    browser,
    claims_of,
    header_of,
    keycloak_address,
    name_of,
    shows_login_form,
    until,
)

from central import actor
from central.operator_auth import CALLBACK_PATH, SESSION_PATH

HOUSE = "/v1/operator/house"
READ = "/v1/operator/inventory"
HOUSE_BODY = {"time": {"zone": "Europe/London", "place": None}, "units": "in", "clock": "12h"}
BACKCHANNEL_SECONDS = 15.0


@pytest.fixture(scope="session")
def keycloak():
    """The Keycloak under test; skips without PHOTO_WALL_KEYCLOAK. A test whose fixtures hold it
    is a `keycloak` test (tests/conftest.py)."""
    try:
        address = keycloak_address()
    except LookupError as unset:
        pytest.skip(str(unset))
    server = Keycloak(address)
    yield server
    server.close()


@pytest.fixture
def realm(keycloak):
    """The realm as imported: nobody signed in, the role where the realm file grants it. Every
    test names it before `database`, so without Keycloak the skip is Keycloak's, whatever the
    database."""
    keycloak.as_imported()
    yield keycloak
    keycloak.grant_role(OWNER_USER)


@pytest.fixture
def central(realm, database):
    with Central(database, realm) as running:
        yield running


@pytest.fixture
def visitor(central):
    with browser() as opened:
        yield opened


def rows(database, query, *params):
    with database.transaction() as conn:
        return conn.execute(query, params).fetchall()


def sessions(database):
    return rows(database, "SELECT * FROM operator_sessions")


def signed_in(visitor: Browser) -> dict:
    """`owner-user` signed in through Keycloak's login page; Central's answer to who it is."""
    landed = visitor.sign_in(OWNER_USER, "#/frames")
    assert (landed.status_code, landed.headers["location"]) == (302, "/#/frames")
    who = visitor.session()
    assert who.status_code == 200, who.text
    return who.json()


def test_an_unset_keycloak_address_is_the_skip_ci_allows(monkeypatch):
    from conftest import CI_SKIP_ALLOWLIST

    monkeypatch.delenv(KEYCLOAK_VARIABLE, raising=False)
    with pytest.raises(LookupError, match=SKIP_REASON):
        keycloak_address()
    assert SKIP_REASON.startswith(CI_SKIP_ALLOWLIST)


# ---- people ----

def test_a_person_with_the_role_signs_in_and_is_named(realm, database, central, visitor):
    assert signed_in(visitor) == {"method": "provider", "name": name_of(OWNER_USER), "roles": ["owner"],
                                  "provider": PROVIDER_NAME}
    assert visitor.get(CENTRAL_ORIGIN + READ).status_code == 200
    # Central knows the person by Keycloak's subject, never by a name, and holds Keycloak's session id.
    (operator,) = rows(database, "SELECT * FROM operators")
    assert (operator["issuer"], operator["subject"], operator["kind"], operator["name"]) == (
        realm.issuer, realm.user_id(OWNER_USER), "person", name_of(OWNER_USER))
    (session,) = sessions(database)
    (at_keycloak,) = realm.sessions(OWNER_USER)
    assert session["provider_session"] == at_keycloak["id"] and session["roles"] == ["owner"]
    assert session["refresh_token"] and session["id_token"]
    # A write names the person.
    assert visitor.central("PUT", HOUSE, json=HOUSE_BODY).status_code == 200
    (house,) = rows(database, "SELECT changed_by FROM singletons WHERE name='house'")
    assert house["changed_by"] == f"person:{operator['operator_id']}"
    assert actor.parse(house["changed_by"]).kind == "person"


def test_a_person_without_the_role_is_told_not_allowed_and_has_no_session(realm, database, central, visitor):
    landed = visitor.sign_in(NO_ROLE_USER)
    assert (landed.status_code, landed.headers["location"]) == (302, "/?sign_in=not_allowed")
    assert not any(header.startswith("photo_wall_session=") and "Max-Age=0" not in header
                   for header in landed.headers.get_list("set-cookie"))
    assert visitor.session().status_code == 401
    assert sessions(database) == [] and rows(database, "SELECT * FROM operators") == []


# ---- machines ----

def test_a_service_accounts_access_token_is_accepted_and_its_write_names_the_client(realm, database, central):
    token = realm.service_token(AGENT)
    bearer = {"Authorization": "Bearer " + token}
    with browser() as script:      # no cookie, no marker, no Origin: Bearer decides alone
        assert script.get(CENTRAL_ORIGIN + READ, headers=bearer).status_code == 200
        wrote = script.http.put(CENTRAL_ORIGIN + HOUSE, json=HOUSE_BODY, headers=bearer)
        assert wrote.status_code == 200, wrote.text
        who = script.get(CENTRAL_ORIGIN + SESSION_PATH, headers=bearer).json()
    (operator,) = rows(database, "SELECT * FROM operators")
    assert (operator["kind"], operator["subject"], operator["issuer"]) == (
        "client", claims_of(token)["sub"], realm.issuer)
    (house,) = rows(database, "SELECT changed_by FROM singletons WHERE name='house'")
    assert house["changed_by"] == f"client:{operator['operator_id']}"
    assert who == {"method": "provider", "name": f"service-account-{AGENT}", "roles": ["owner"],
                   "provider": PROVIDER_NAME}
    assert sessions(database) == []     # a machine opens no session


def test_a_service_account_without_the_role_is_401(realm, database, central):
    bearer = {"Authorization": "Bearer " + realm.service_token(STRANGER)}
    with browser() as script:
        for method, path in (("GET", READ), ("GET", SESSION_PATH), ("PUT", HOUSE)):
            refused = script.http.request(method, CENTRAL_ORIGIN + path, headers=bearer,
                                          json=HOUSE_BODY if method == "PUT" else None)
            assert (refused.status_code, refused.json()) == (401, {"error": "unauthorized"}), (method, path)
    assert rows(database, "SELECT * FROM operators") == []


# ---- what Keycloak states, and where ----

def test_a_service_accounts_access_token_states_the_role_and_the_audience_where_central_reads_them(realm):
    """Keycloak's defaults: a client role under `resource_access.<client>.roles`, and that client
    in `aud`, in the token of ANOTHER client's service account that holds the role (`azp` is that
    other client). Without the role: neither, so nothing addresses the token to Central."""
    token = realm.service_token(AGENT)
    claims = claims_of(token)
    assert header_of(token)["alg"] == "RS256"
    assert claims["iss"] == realm.issuer and claims["azp"] == AGENT and claims["typ"] == "Bearer"
    assert claims["resource_access"][CLIENT_ID]["roles"] == [ROLE]
    assert CLIENT_ID in _audiences(claims)
    stranger = claims_of(realm.service_token(STRANGER))
    assert stranger["azp"] == STRANGER
    assert CLIENT_ID not in stranger.get("resource_access", {}) and CLIENT_ID not in _audiences(stranger)


def test_a_persons_access_token_is_issued_to_centrals_client_and_leaves_it_out_of_the_audience(
        realm, central, visitor):
    """The tokens Keycloak hands Central for a person, read with the test in Central's place; the
    realm has no mapper of its own. Keycloak's default audience mapper ("audience resolve") leaves
    the requesting client out of its own token's `aud`, so the access token names Central's client
    in `azp` alone: that is the arm of `policy.addressed_to` a person signs in by. It carries
    `typ` "Bearer", the role at `resource_access.photo-wall.roles` and the session id Keycloak
    names at back-channel logout. The ID token is `typ` "ID" and never a credential."""
    tokens = visitor.tokens_as_central_gets_them(realm, OWNER_USER)
    access, identity = claims_of(tokens["access_token"]), claims_of(tokens["id_token"])
    assert header_of(tokens["access_token"])["alg"] == "RS256"
    assert access["iss"] == identity["iss"] == realm.issuer
    assert (access["typ"], access["azp"]) == ("Bearer", CLIENT_ID)
    assert CLIENT_ID not in _audiences(access)
    assert access["resource_access"][CLIENT_ID]["roles"] == [ROLE]
    assert identity["typ"] == "ID" and _audiences(identity) == [CLIENT_ID] and identity["azp"] == CLIENT_ID
    assert access["sub"] == identity["sub"] == realm.user_id(OWNER_USER)
    assert access["sid"] == identity["sid"] == realm.sessions(OWNER_USER)[0]["id"]
    assert identity["name"] == name_of(OWNER_USER)
    assert tokens["refresh_token"]
    # Neither is a Bearer credential: the access token is issued to Central's own client, so it
    # is a person's sign-in and never a machine's (`TokenJudge.credential`); the ID token never is.
    with browser() as script:
        as_bearer = {kind: script.http.get(CENTRAL_ORIGIN + READ,
                                           headers={"Authorization": "Bearer " + tokens[kind]}).status_code
                     for kind in ("access_token", "id_token")}
    assert as_bearer == {"access_token": 401, "id_token": 401}


def test_a_person_without_the_roles_access_token_is_centrals_and_carries_no_role(realm, visitor):
    """Why such a person is told "not allowed" and not that sign-in failed: the token is addressed
    to Central (`azp`), and states no role."""
    access = claims_of(visitor.tokens_as_central_gets_them(realm, NO_ROLE_USER)["access_token"])
    assert access["azp"] == CLIENT_ID and CLIENT_ID not in _audiences(access)
    assert CLIENT_ID not in access.get("resource_access", {})


def _audiences(claims) -> list[str]:
    aud = claims.get("aud", [])
    return [aud] if isinstance(aud, str) else list(aud)


# ---- staying judged ----

def _wait_until_stale(database) -> float:
    """Sleep until Central's clock (the system's) passes the session's judgement; that instant."""
    (session,) = sessions(database)
    time.sleep(max(0.0, session["fresh_until"] - time.time()) + 0.5)
    return session["fresh_until"]


def test_a_session_is_re_judged_after_the_access_tokens_life_and_ends_once_the_role_is_removed(
        realm, database, central, visitor):
    """Keycloak's access token lives `accessTokenLifespan` (the realm file's few seconds); both
    clocks are real, so the test waits it out twice."""
    signed_in(visitor)
    judged_until = _wait_until_stale(database)
    assert visitor.session().status_code == 200          # refreshed at Keycloak: the role stands
    (session,) = sessions(database)
    assert judged_until < session["fresh_until"] <= time.time() + ACCESS_TOKEN_SECONDS + 1

    realm.remove_role(OWNER_USER)
    _wait_until_stale(database)
    assert visitor.session().status_code == 401          # refreshed again: no role, no session
    assert sessions(database) == []
    # Keycloak's own session is still there: it was the role that ended Central's, not a sign-out.
    assert len(realm.sessions(OWNER_USER)) == 1


def test_an_administrators_sign_out_in_keycloak_ends_centrals_session_by_back_channel_logout(
        realm, database, central, visitor):
    """Nothing asks Central anything between the sign-out and the session row's end, so no refresh
    can have ended it: only Keycloak's logout token, posted to Central's back-channel address."""
    signed_in(visitor)
    assert len(sessions(database)) == 1
    realm.log_out(OWNER_USER)
    until(lambda: sessions(database) == [], BACKCHANNEL_SECONDS,
          "Keycloak's back-channel logout ended Central's session (can the Keycloak container reach "
          "Central at the realm's backchannel.logout.url?)")
    assert visitor.session().status_code == 401


# ---- Log out ----

def test_log_out_answers_an_end_session_address_keycloak_takes_and_keycloaks_session_ends(
        realm, database, central, visitor):
    signed_in(visitor)
    # Keycloak knows this browser: a second sign-in passes straight through, no login page.
    again = visitor.start()
    assert again.status_code == 302 and again.headers["location"].startswith(CENTRAL_ORIGIN + CALLBACK_PATH)

    logged_out = visitor.central("DELETE", SESSION_PATH)
    assert logged_out.status_code == 200, logged_out.text
    address = logged_out.json()["end_session"]
    parts = urlsplit(address)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == realm.issuer + "/protocol/openid-connect/logout"
    assert set(parse_qs(parts.query)) == {"client_id", "id_token_hint", "post_logout_redirect_uri"}
    assert visitor.session().status_code == 401 and sessions(database) == []

    # Keycloak takes the address as it is: no confirmation page, straight back to the console.
    ended = visitor.get(address)
    assert (ended.status_code, ended.headers.get("location")) == (302, CENTRAL_ORIGIN + "/"), ended.text[:600]
    assert realm.sessions(OWNER_USER) == []
    assert shows_login_form(visitor.start())
