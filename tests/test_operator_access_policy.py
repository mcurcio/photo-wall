"""Operator access's rules (decision 0035), pure: `central/operator_access/policy.py` judged with
real signed tokens (tests/operator_provider_fake.py signs them; the signature check is
central/infra/jwt_signature.py) and a hand-held clock. No database, no HTTP.
"""
import base64
import hashlib
import hmac
import json

import pytest
from operator_provider_fake import CLIENT_ID, ISSUER, FakeProvider

from central import actor
from central.operator_access import policy
from central.operator_access.model import (
    BreakGlass,
    CarriedOn,
    Ended,
    Machine,
    Person,
    ProviderRefused,
    ProviderUnreachable,
    Rejudged,
    Session,
    SessionSecrets,
    SharedConsole,
    SignatureRefusal,
    TokenRefusal,
    TokenRefused,
    TokenSet,
)
from central.operator_access.settings import OidcSettings, oidc_settings_in
from contracts.time import ManualClock

NOW = 1_800_000_000.0
PATH = ("resource_access", CLIENT_ID, "roles")


@pytest.fixture
def provider():
    fake = FakeProvider(clock=ManualClock(NOW))
    fake.add_user("user-1", "Ada Owner")
    return fake


def judge_of(provider, **overrides):
    return policy.TokenJudge(**{"issuer": ISSUER, "audience": CLIENT_ID, "roles_path": PATH,
                                "role_names": {"owner": "owner"}, "signature": provider.signature,
                                **overrides})


def refusal(call):
    with pytest.raises(TokenRefused) as refused:
        call()
    return refused.value.reason


def _part(value) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


# ---- roles and permissions ----

def test_roles_are_read_from_the_claim_path():
    claims = {"resource_access": {CLIENT_ID: {"roles": ["owner"]}}}
    assert policy.roles_in(claims, PATH) == {"owner"}


def test_a_missing_path_is_no_roles_never_an_error():
    for claims in ({}, {"resource_access": {}}, {"resource_access": {CLIENT_ID: {}}},
                   {"resource_access": "owner"}, {"resource_access": {CLIENT_ID: {"roles": "owner"}}},
                   {"resource_access": {CLIENT_ID: {"roles": None}}}):
        assert policy.roles_in(claims, PATH) == frozenset()


def test_unknown_role_names_are_ignored():
    claims = {"roles": ["guest", "owner", 7, None, {"owner": 1}, "OWNER"]}
    assert policy.roles_in(claims, ("roles",)) == {"owner"}


def test_the_provider_may_call_the_owner_role_something_else():
    claims = {"roles": ["photo-wall-admin", "owner"]}
    assert policy.roles_in(claims, ("roles",), {"photo-wall-admin": "owner"}) == {"owner"}
    assert policy.roles_in({"roles": ["owner"]}, ("roles",), {"photo-wall-admin": "owner"}) == frozenset()


def test_owner_holds_every_permission_and_no_role_holds_none():
    for permission in ("read", "operate", "configure"):
        assert policy.allows(frozenset({"owner"}), permission)
        assert not policy.allows(frozenset(), permission)


def test_a_method_asks_for_read_or_configure():
    assert [policy.permission_for(method) for method in ("GET", "HEAD", "OPTIONS")] == ["read"] * 3
    assert [policy.permission_for(method) for method in ("POST", "PUT", "PATCH", "DELETE")] == ["configure"] * 4


def test_each_principal_is_named_in_audit_by_the_grammar():
    operator_id = policy.new_operator_id()
    owner = frozenset({"owner"})
    assert policy.actor_of(Person(operator_id, "Ada", owner, "s", "http://x")) == f"person:{operator_id}"
    assert policy.actor_of(Machine(operator_id, "script", owner)) == f"client:{operator_id}"
    assert policy.actor_of(BreakGlass()) == "api-token"
    assert policy.actor_of(SharedConsole("http://x")) == "console"
    assert policy.permits(BreakGlass(), "configure") and policy.permits(SharedConsole("http://x"), "configure")
    assert not policy.permits(Machine(operator_id, "script", frozenset()), "read")


def test_the_actor_grammar_reads_what_it_writes_and_nothing_else():
    operator_id = policy.new_operator_id()
    assert actor.is_operator_id(operator_id)
    assert actor.parse(actor.encode("person", operator_id)) == actor.ParsedActor(
        "person", operator_id, f"person:{operator_id}")
    assert actor.parse(actor.encode("client", operator_id)).kind == "client"
    for word in actor.FIXED_ACTORS:
        assert actor.parse(word) == actor.ParsedActor("fixed", None, word)
    for value in ("unknown", "person:", "person:someone@example.com", f"owner:{operator_id}",
                  f"person:{operator_id}x", f" person:{operator_id}", "console\n", ""):
        assert actor.parse(value) is None, value
    for kind, value in (("owner", operator_id), ("person", "user-1"), ("person", "op_UPPERCASE0000000")):
        with pytest.raises(ValueError):
            actor.encode(kind, value)


def test_an_actor_field_refuses_a_value_outside_the_grammar():
    from pydantic import TypeAdapter, ValidationError
    adapter = TypeAdapter(actor.Actor)
    operator_id = policy.new_operator_id()
    for value in ("console", "api-token", "home-assistant", f"person:{operator_id}", f"client:{operator_id}"):
        assert adapter.validate_python(value) == value
    for value in ("consol", "person:ada", "console\n", f"person:{operator_id}\n"):
        with pytest.raises(ValidationError):
            adapter.validate_python(value)


# ---- validating a token ----

def test_a_valid_access_token_is_judged_with_its_roles_and_its_expiry(provider):
    judgement = judge_of(provider).access(provider.access_token("user-1", "sid-1"), NOW)
    assert (judgement.subject, judgement.name, judgement.roles, judgement.provider_session) == (
        "user-1", "Ada Owner", {"owner"}, "sid-1")
    assert judgement.fresh_until == NOW + 300


@pytest.mark.parametrize("algorithm", ["none", "None", "HS256", "HS384", "HS512", "RS384", "ES384", "", None, 7])
def test_an_algorithm_off_the_allow_list_is_refused_before_any_key_is_asked_for(provider, algorithm):
    payload = provider.access_claims("user-1")
    token = f"{_part({'alg': algorithm, 'kid': provider.key_id})}.{_part(payload)}."

    def no_key(*_):
        raise AssertionError("the signature was asked for")

    assert refusal(lambda: judge_of(provider, signature=no_key).access(token, NOW)) is TokenRefusal.ALGORITHM


def test_a_token_signed_with_the_public_key_as_an_hmac_secret_is_refused(provider):
    """The classic confusion: HS256 keyed by the issuer's public key. Refused by the allow-list,
    and the signature check refuses the algorithm on its own too."""
    head, body = _part({"alg": "HS256", "kid": provider.key_id}), _part(provider.access_claims("user-1"))
    secret = json.dumps(provider.jwks()["keys"][0]).encode()
    mac = base64.urlsafe_b64encode(hmac.new(secret, f"{head}.{body}".encode(), hashlib.sha256).digest())
    token = f"{head}.{body}.{mac.rstrip(b'=').decode()}"
    assert refusal(lambda: judge_of(provider).access(token, NOW)) is TokenRefusal.ALGORITHM
    assert provider.signature(token, "HS256", provider.key_id) is SignatureRefusal.SIGNATURE
    assert provider.signature(token, "none", provider.key_id) is SignatureRefusal.SIGNATURE


def test_each_allowed_algorithm_is_accepted_under_its_own_key(provider):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import ec

    from central.infra.jwt_signature import verified_claims

    claims = provider.access_claims("user-1")
    ps256 = provider.sign(claims, algorithm="PS256")
    assert judge_of(provider).access(ps256, NOW).subject == "user-1"
    key = ec.generate_private_key(ec.SECP256R1())
    jwks = {"keys": [{**jwt.algorithms.ECAlgorithm.to_jwk(key.public_key(), as_dict=True), "kid": "ec-1"}]}
    es256 = jwt.encode(claims, key, algorithm="ES256", headers={"kid": "ec-1"})
    judge = judge_of(provider, signature=lambda token, algorithm, key_id: verified_claims(
        token, algorithm, key_id, jwks))
    assert judge.access(es256, NOW).subject == "user-1"
    # A token says RS256 over an EC key's id: the key cannot verify it.
    assert refusal(lambda: judge.access(provider.sign(claims, key_id="ec-1"), NOW)) is TokenRefusal.SIGNATURE


def test_a_changed_token_and_a_token_of_another_key_are_refused(provider):
    token = provider.access_token("user-1")
    head, body, signature = token.split(".")
    forged = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    forged["sub"] = "someone-else"
    assert refusal(lambda: judge_of(provider).access(f"{head}.{_part(forged)}.{signature}", NOW)) \
        is TokenRefusal.SIGNATURE
    other = FakeProvider(clock=ManualClock(NOW))
    other.add_user("user-1")
    other.rotate_keys()
    assert refusal(lambda: judge_of(provider).access(other.access_token("user-1"), NOW)) is TokenRefusal.UNKNOWN_KEY
    same_id = other.sign(other.access_claims("user-1"), key_id=provider.key_id)
    assert refusal(lambda: judge_of(provider).access(same_id, NOW)) is TokenRefusal.SIGNATURE


@pytest.mark.parametrize("token", ["", "a.b", "a.b.c.d", "!!.b.c", _part([1]) + ".b.c", "é.b.c", None, 7,
                                   "a" * 20_000 + ".b.c"])
def test_what_is_not_a_signed_token_is_refused_as_malformed(provider, token):
    assert refusal(lambda: judge_of(provider).access(token, NOW)) is TokenRefusal.MALFORMED


@pytest.mark.parametrize("issuer", [ISSUER + "/", ISSUER.upper(), "https://evil.test/realms/home", None])
def test_another_issuer_is_refused(provider, issuer):
    token = provider.access_token("user-1", iss=issuer)
    assert refusal(lambda: judge_of(provider).access(token, NOW)) is TokenRefusal.ISSUER


NOT_CENTRAL = ["account", ["account"], ["photo-wall-2"], [], None, {"photo-wall": 1}, CLIENT_ID + "x"]
"""An `aud`, and likewise an `azp`, that does not name Central's client."""


def test_a_token_is_addressed_to_central_when_its_audience_holds_the_client(provider):
    """`aud` as one string or a list holding the client, whoever the token was issued to: a
    machine's token (another client's service account that holds one of Central's roles)."""
    for aud in (CLIENT_ID, [CLIENT_ID], ["account", CLIENT_ID]):
        for azp in ("reader-script", None):
            token = provider.access_token("user-1", aud=aud, azp=azp)
            assert judge_of(provider).access(token, NOW).subject == "user-1"


@pytest.mark.parametrize("aud", NOT_CENTRAL)
def test_a_token_issued_to_centrals_own_client_is_addressed_to_central_whatever_its_audience(provider, aud):
    """Keycloak leaves the requesting client out of its own token's `aud`: a person's token."""
    token = provider.access_token("user-1", aud=aud, azp=CLIENT_ID)
    assert judge_of(provider).access(token, NOW).subject == "user-1"


def test_a_persons_token_as_the_provider_issues_it_is_addressed_by_azp_alone(provider):
    claims = provider.access_claims("user-1")
    assert claims["azp"] == CLIENT_ID and "aud" not in claims
    assert policy.addressed_to(claims, CLIENT_ID)
    assert not policy.addressed_to({**claims, "azp": "reader-script"}, CLIENT_ID)


@pytest.mark.parametrize("aud", NOT_CENTRAL)
@pytest.mark.parametrize("azp", ["reader-script", [CLIENT_ID], {CLIENT_ID: 1}, CLIENT_ID + "x", "", None, True])
def test_a_token_neither_addressed_nor_issued_to_centrals_client_is_refused(provider, aud, azp):
    """Another client's `azp` with an `aud` that lacks Central's client is the role-less machine;
    no `azp` at all, or one that is not exactly the client id, addresses nothing."""
    token = provider.access_token("user-1", aud=aud, azp=azp)
    assert refusal(lambda: judge_of(provider).access(token, NOW)) is TokenRefusal.AUDIENCE


def test_a_machines_token_is_addressed_to_central_only_while_it_holds_a_role(provider):
    provider.add_service_account("reader-script")
    provider.add_service_account("no-role-script", roles=())
    judge = judge_of(provider)
    held = provider.access_claims("service-account-reader-script")
    assert (held["azp"], held["aud"]) == ("reader-script", CLIENT_ID)
    assert judge.access(provider.service_token("reader-script"), NOW).roles == {"owner"}
    without = provider.access_claims("service-account-no-role-script")
    assert without["azp"] == "no-role-script" and "aud" not in without
    assert refusal(lambda: judge.access(provider.service_token("no-role-script"), NOW)) is TokenRefusal.AUDIENCE


def test_expiry_is_read_on_centrals_clock_with_a_minute_of_leeway(provider):
    judge, token = judge_of(provider), provider.access_token("user-1")   # exp = NOW + 300
    assert judge.access(token, NOW + 300 + 59).subject == "user-1"
    assert refusal(lambda: judge.access(token, NOW + 300 + 60)) is TokenRefusal.EXPIRED
    for exp in (None, "soon", True):
        assert refusal(lambda exp=exp: judge.access(provider.access_token("user-1", exp=exp), NOW)) \
            is TokenRefusal.MALFORMED


def test_a_token_not_yet_valid_is_refused_past_the_leeway(provider):
    judge = judge_of(provider)
    assert judge.access(provider.access_token("user-1", nbf=int(NOW) + 60), NOW).subject == "user-1"
    assert refusal(lambda: judge.access(provider.access_token("user-1", nbf=int(NOW) + 61), NOW)) \
        is TokenRefusal.NOT_YET_VALID
    assert refusal(lambda: judge.access(provider.access_token("user-1", nbf="later"), NOW)) is TokenRefusal.MALFORMED


def test_an_access_token_needs_a_subject(provider):
    assert refusal(lambda: judge_of(provider).access(provider.access_token("user-1", sub=None), NOW)) \
        is TokenRefusal.SUBJECT


def test_an_id_token_needs_exactly_the_transactions_nonce(provider):
    judge = judge_of(provider)
    token = provider.id_token("user-1", nonce="n-1", sid="sid-1")
    assert judge.identity(token, "n-1", NOW) == policy.Identity("user-1", "Ada Owner", "sid-1")
    for stated in ("n-2", None, ["n-1"]):
        wrong = provider.id_token("user-1", nonce=stated, sid="sid-1")
        assert refusal(lambda wrong=wrong: judge.identity(wrong, "n-1", NOW)) is TokenRefusal.NONCE
    for_another = provider.id_token("user-1", nonce="n-1", sid="sid-1", aud="another-client",
                                    azp="another-client")
    assert refusal(lambda: judge.identity(for_another, "n-1", NOW)) is TokenRefusal.AUDIENCE


def test_an_id_token_or_a_logout_token_is_never_a_credential(provider):
    """Both are signed by the issuer for Central's client, and Log out hands the ID token to the
    browser: neither passes as an access token, whatever roles a mapper put in it."""
    judge = judge_of(provider)
    roles = {"resource_access": {CLIENT_ID: {"roles": ["owner"]}}}
    id_token = provider.id_token("user-1", nonce="n-1", sid="sid-1", **roles)
    assert refusal(lambda: judge.access(id_token, NOW)) is TokenRefusal.NOT_AN_ACCESS_TOKEN
    without_hash = provider.id_token("user-1", nonce="n-1", sid="sid-1", at_hash=None, **roles)
    assert refusal(lambda: judge.access(without_hash, NOW)) is TokenRefusal.NOT_AN_ACCESS_TOKEN   # its typ
    logout = provider.logout_token(sid="sid-1", typ=None, **roles)
    assert refusal(lambda: judge.access(logout, NOW)) is TokenRefusal.NOT_AN_ACCESS_TOKEN
    # A provider that states no `typ` claim, or RFC 9068's, is an access token.
    for typ in (None, "at+jwt", "Bearer"):
        assert judge.access(provider.access_token("user-1", typ=typ), NOW).subject == "user-1"


def test_a_token_issued_to_centrals_own_client_is_never_a_bearer_credential(provider):
    """A person's sign-in token stands for the sign-in (`access`) and is refused as a Bearer
    (`credential`), whatever role or audience it states; a machine's own client's token is both."""
    provider.add_service_account("reader-script")
    judge = judge_of(provider)
    roles = {"resource_access": {CLIENT_ID: {"roles": ["owner"]}}}
    persons = [provider.access_token("user-1"), provider.access_token("user-1", aud=CLIENT_ID),
               provider.access_token("user-1", aud=["account", CLIENT_ID], **roles),
               provider.access_token("service-account-reader-script", azp=CLIENT_ID)]
    for token in persons:
        assert judge.access(token, NOW).roles == {"owner"}
        assert refusal(lambda token=token: judge.credential(token, NOW)) is TokenRefusal.ISSUED_TO_CENTRAL
    machines = provider.service_token("reader-script")
    assert judge.credential(machines, NOW) == judge.access(machines, NOW)
    assert judge.credential(provider.access_token("user-1", aud=CLIENT_ID, azp=None), NOW).subject == "user-1"


def test_a_bearer_credential_is_refused_by_every_rule_an_access_token_is_and_by_those_first(provider):
    judge = judge_of(provider)
    own = {"azp": CLIENT_ID}
    assert refusal(lambda: judge.credential(provider.access_token("user-1", exp=NOW - 61), NOW)) is TokenRefusal.EXPIRED
    assert refusal(lambda: judge.credential(
        provider.access_token("user-1", aud="account", azp="another"), NOW)) is TokenRefusal.AUDIENCE
    id_token = provider.id_token("user-1", nonce="n-1", sid="sid-1", **own)
    assert refusal(lambda: judge.credential(id_token, NOW)) is TokenRefusal.NOT_AN_ACCESS_TOKEN
    assert refusal(lambda: judge.credential(provider.access_token("user-1", sub=None), NOW)) is TokenRefusal.SUBJECT


def test_a_logout_token_needs_its_event_a_session_or_subject_and_no_nonce(provider):
    judge = judge_of(provider)
    assert judge.logout(provider.logout_token(sid="sid-1"), NOW) == policy.LogoutNotice("sid-1", None)
    assert judge.logout(provider.logout_token(sub="user-1"), NOW) == policy.LogoutNotice(None, "user-1")
    assert refusal(lambda: judge.logout(provider.logout_token(), NOW)) is TokenRefusal.NO_SESSION_NAMED
    assert refusal(lambda: judge.logout(provider.logout_token(sid="sid-1", nonce="n"), NOW)) \
        is TokenRefusal.NOT_A_LOGOUT
    for events in (None, {}, {"http://schemas.openid.net/event/other": {}}, ["x"]):
        token = provider.logout_token(sid="sid-1", events=events)
        assert refusal(lambda token=token: judge.logout(token, NOW)) is TokenRefusal.NOT_A_LOGOUT
    assert refusal(lambda: judge.logout(provider.access_token("user-1", "sid-1"), NOW)) is TokenRefusal.NOT_A_LOGOUT
    assert refusal(lambda: judge.logout(provider.logout_token(sid="sid-1", aud="another"), NOW)) \
        is TokenRefusal.AUDIENCE
    assert refusal(lambda: judge.logout(provider.logout_token(sid="sid-1", iss="https://evil"), NOW)) \
        is TokenRefusal.ISSUER


# ---- the Bearer order ----

def test_the_bearer_order_is_break_glass_then_the_provider_then_nobody():
    machine = Machine("op_aaaaaaaaaaaaaaaa", "script", frozenset({"owner"}))
    asked = []

    def provider_token(candidate):
        asked.append(candidate)
        return machine if candidate == b"provider-token" else None

    def is_token(candidate):
        return candidate == b"the-token"

    assert policy.bearer_principal(b"the-token", break_glass=is_token, machine=provider_token) == BreakGlass()
    assert asked == []   # the break-glass token is never shown to the provider's judge
    assert policy.bearer_principal(b"provider-token", break_glass=is_token, machine=provider_token) is machine
    assert policy.bearer_principal(b"other", break_glass=is_token, machine=provider_token) is None
    # Token mode: no provider. Provider mode without a break-glass token: only the provider.
    assert policy.bearer_principal(b"provider-token", break_glass=is_token, machine=None) is None
    assert policy.bearer_principal(b"the-token", break_glass=None, machine=provider_token) is None
    assert policy.bearer_principal(b"the-token", break_glass=None, machine=None) is None


# ---- a cookie write ----

def test_a_cookie_write_needs_the_marker_the_sign_in_origin_and_a_same_origin_fetch():
    def check(**overrides):
        return policy.write_refusal(**{"marked": True, "origin": "http://wall", "session_origin": "http://wall",
                                       "fetch_site": None, **overrides})

    assert check() is None and check(fetch_site="same-origin") is None
    assert check(marked=False) == "request_unmarked"
    assert check(marked=False, origin="http://evil") == "request_unmarked"
    for origin in (None, "http://evil", "https://wall", "http://wall:81", "null"):
        assert check(origin=origin) == "origin_mismatch"
    for site in ("cross-site", "same-site", "none", ""):
        assert check(fetch_site=site) == "origin_mismatch"


# ---- staying judged ----

def session_of(**overrides) -> Session:
    return Session(**{"session_id": "s", "operator_id": "op_aaaaaaaaaaaaaaaa", "name": "Ada", "origin": "http://wall",
                      "provider_session": "sid-1", "roles": frozenset({"owner"}), "fresh_until": NOW + 300,
                      "hard_end": policy.hard_end(NOW), **overrides})


def test_a_session_is_fresh_then_stale_and_ended_at_the_hard_end_whatever_its_freshness():
    session = session_of()
    assert policy.standing(session, NOW) == "fresh"
    assert policy.standing(session, NOW + 299.9) == "fresh"
    assert policy.standing(session, NOW + 300) == "stale"
    assert policy.HARD_END_SECONDS == 30 * 24 * 3600
    end = NOW + policy.HARD_END_SECONDS
    assert policy.standing(session, end - 1) == "stale"
    assert policy.standing(session, end) == "ended"
    assert policy.standing(session_of(fresh_until=end + 999), end) == "ended"   # fresh, and over


KEPT = SessionSecrets(refresh_token="refresh-1", id_token="id-1")
OWNER_JUDGEMENT = policy.Judgement("user-1", "Ada", frozenset({"owner"}), NOW + 600, "sid-1")


def test_a_refresh_that_answers_with_the_role_re_makes_the_judgement_and_keeps_the_new_tokens():
    used = []

    def refresh(token):
        used.append(token)
        return TokenSet("access-2", None, "refresh-2")

    outcome = policy.rejudge(KEPT, refresh=refresh, judge=lambda token: OWNER_JUDGEMENT, now=NOW)
    assert used == ["refresh-1"]
    assert outcome == Rejudged(frozenset({"owner"}), NOW + 600, SessionSecrets("refresh-2", "id-1"))
    # A provider that does not rotate the refresh token: the kept one stays.
    kept = policy.rejudge(KEPT, refresh=lambda token: TokenSet("access-2", "id-2", None),
                          judge=lambda token: OWNER_JUDGEMENT, now=NOW)
    assert kept.secrets == SessionSecrets("refresh-1", "id-2")


def test_a_provider_that_refuses_the_refresh_ends_the_session():
    def refused(token):
        raise ProviderRefused("invalid_grant")

    assert policy.rejudge(KEPT, refresh=refused, judge=lambda token: OWNER_JUDGEMENT, now=NOW) == Ended()


def test_a_refresh_without_the_role_or_with_a_refused_token_ends_the_session():
    def tokens(token):
        return TokenSet("access-2", None, "refresh-2")

    no_role = policy.Judgement("user-1", "Ada", frozenset(), NOW + 600, "sid-1")
    assert policy.rejudge(KEPT, refresh=tokens, judge=lambda token: no_role, now=NOW) == Ended()

    def refused(token):
        raise TokenRefused(TokenRefusal.AUDIENCE)

    assert policy.rejudge(KEPT, refresh=tokens, judge=refused, now=NOW) == Ended()
    never = []
    assert policy.rejudge(SessionSecrets(None, "id-1"), refresh=never.append,
                          judge=lambda token: OWNER_JUDGEMENT, now=NOW) == Ended()
    assert never == []


def test_a_provider_that_does_not_answer_is_not_a_refusal():
    def silent(token):
        raise ProviderUnreachable()

    outcome = policy.rejudge(KEPT, refresh=silent, judge=lambda token: OWNER_JUDGEMENT, now=NOW)
    assert outcome == CarriedOn(retry_at=NOW + 60)

    def tokens(token):
        return TokenSet("access-2", None, "refresh-2")

    def keys_unreachable(token):   # the refresh answered, the keys could not be fetched
        raise ProviderUnreachable()

    assert policy.rejudge(KEPT, refresh=tokens, judge=keys_unreachable, now=NOW) == CarriedOn(NOW + 60)


# ---- the sign-in transaction ----

def test_a_return_route_is_a_console_hash_route_or_home():
    for route in ("#/", "#/frames", "#/frames/portrait?tab=power", "#/settings/people-and-access"):
        assert policy.return_route(route) == route
    for route in (None, "", "/", "#", "#frames", "https://evil.test/#/", "//evil.test/#/", "#/a b",
                  "#/a\r\nSet-Cookie: x=1", "#/é", "#/" + "a" * 600, "#/a#b", "#/a\\b"):
        assert policy.return_route(route) == "", route


def test_a_transaction_stands_for_its_own_state_for_ten_minutes():
    transaction = policy.new_transaction("http://wall", "#/frames", NOW)
    assert transaction.return_to == "#/frames" and transaction.origin == "http://wall"
    assert policy.transaction_stands(transaction, transaction.state, NOW + 599)
    assert not policy.transaction_stands(transaction, transaction.state, NOW + 600)
    for state in (None, "", transaction.state + "x", transaction.state[:-1], "é", transaction.nonce):
        assert not policy.transaction_stands(transaction, state, NOW)
    other = policy.new_transaction("http://wall", "https://evil.test/", NOW)
    assert other.return_to == ""
    assert len({transaction.state, transaction.nonce, transaction.code_verifier,
                other.state, other.nonce, other.code_verifier}) == 6


def test_the_pkce_challenge_is_rfc_7636s_s256():
    # RFC 7636 appendix B.
    assert policy.code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == \
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_a_session_is_keyed_by_its_cookies_hash_and_a_cookie_is_256_random_bits():
    value = policy.new_cookie_value()
    assert len(base64.urlsafe_b64decode(value + "=")) == 32 and value != policy.new_cookie_value()
    assert policy.session_id_of(value) == hashlib.sha256(value.encode()).hexdigest()


def test_the_transaction_seal_opens_only_what_it_sealed():
    from central.infra.transaction_seal import AesGcmTransactionSeal

    seal = AesGcmTransactionSeal("client-secret-1")
    transaction = policy.new_transaction("http://wall", "#/frames", NOW)
    value = seal.seal(transaction)
    assert seal.open(value) == transaction
    assert value != seal.seal(transaction)                      # a fresh nonce each time
    for secret in (transaction.state, transaction.nonce, transaction.code_verifier):
        assert secret not in value and secret.encode() not in base64.urlsafe_b64decode(value + "==")
    assert AesGcmTransactionSeal("client-secret-2").open(value) is None
    changed = value[:-2] + ("A" if value[-2] != "A" else "B") + value[-1]
    for bad in (changed, value[:-4], "", "x", None, 7, "é" * 20, "A" * 5000, value + "A"):
        assert seal.open(bad) is None, bad


# ---- the deployment's settings ----

ENV = {"PHOTO_WALL_OIDC_ISSUER": "https://id.example/realms/home", "PHOTO_WALL_OIDC_CLIENT_ID": "photo-wall",
       "PHOTO_WALL_OIDC_CLIENT_SECRET": "s3cret"}


def test_no_provider_variable_is_token_mode():
    assert oidc_settings_in({}) is None
    assert oidc_settings_in({"PHOTO_WALL_ADMIN_TOKEN": "x", "PHOTO_WALL_OIDC_ISSUER": "  "}) is None


def test_the_three_required_variables_configure_a_provider_with_the_defaults():
    settings = oidc_settings_in(ENV)
    assert settings == OidcSettings("https://id.example/realms/home", "photo-wall", "s3cret")
    assert (settings.role, settings.name, settings.issuer_host) == ("owner", "id.example", "id.example")
    assert settings.roles_claim == ("resource_access", "photo-wall", "roles")
    assert settings.role_names == {"owner": "owner"}
    assert "s3cret" not in repr(settings)


def test_the_optional_variables_name_the_role_the_claim_and_the_provider():
    settings = oidc_settings_in({**ENV, "PHOTO_WALL_OIDC_ROLE": "wall-admin",
                                 "PHOTO_WALL_OIDC_ROLES_CLAIM": "realm_access.roles",
                                 "PHOTO_WALL_OIDC_NAME": "Home ID"})
    assert (settings.roles_claim, settings.name) == (("realm_access", "roles"), "Home ID")
    assert settings.role_names == {"wall-admin": "owner"}
    # A client id with a dot still reads its own entry of resource_access.
    dotted = oidc_settings_in({**ENV, "PHOTO_WALL_OIDC_CLIENT_ID": "wall.example"})
    assert dotted.roles_claim == ("resource_access", "wall.example", "roles")


@pytest.mark.parametrize("given, missing", [
    (("PHOTO_WALL_OIDC_ISSUER",), "PHOTO_WALL_OIDC_CLIENT_ID, PHOTO_WALL_OIDC_CLIENT_SECRET missing"),
    (("PHOTO_WALL_OIDC_ISSUER", "PHOTO_WALL_OIDC_CLIENT_ID"), "PHOTO_WALL_OIDC_CLIENT_SECRET missing"),
    (("PHOTO_WALL_OIDC_CLIENT_ID", "PHOTO_WALL_OIDC_CLIENT_SECRET"), "PHOTO_WALL_OIDC_ISSUER missing"),
])
def test_a_partial_setting_is_refused_in_one_line_naming_what_is_missing(given, missing):
    with pytest.raises(ValueError) as refused:
        oidc_settings_in({name: ENV[name] for name in given})
    message = str(refused.value)
    assert message.endswith(missing) and "\n" not in message and "s3cret" not in message
    with pytest.raises(ValueError, match="PHOTO_WALL_OIDC_ISSUER.*missing"):
        oidc_settings_in({"PHOTO_WALL_OIDC_NAME": "Home ID"})
    with pytest.raises(ValueError, match="http"):
        oidc_settings_in({**ENV, "PHOTO_WALL_OIDC_ISSUER": "id.example/realms/home"})
