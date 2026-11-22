"""One behaviour suite for both `IdentityProvider`s (decision 0035): `FakeProvider` in process
(tests/operator_provider_fake.py), which Central's sign-in tests run against, and the production
OpenID Connect client (`central/infra/oidc_provider.py`) speaking HTTP to the same fake served on
a loopback port. Each case runs once per form and asserts only what the port
(central/operator_access/ports.py) promises, so a sign-in test that passes on the fake means the
same through the real client.

The cases after the shared suite hold the production client alone to what only it does: the
metadata and key fetches, and which answers are a refusal.
"""
from __future__ import annotations

import socket
from typing import NamedTuple
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from operator_provider_fake import CLIENT_ID, CLIENT_SECRET, FakeProvider

from central.infra.oidc_provider import JWKS_REFETCH_SECONDS, OidcProvider
from central.operator_access import policy
from central.operator_access.model import (
    ProviderRefused,
    ProviderUnreachable,
    Reach,
    SignatureRefusal,
    TokenSet,
)
from central.operator_access.ports import IdentityProvider
from contracts.time import ManualClock

NOW = 1_800_000_000.0
REDIRECT = "http://wall.test/v1/operator/session/callback"
VERIFIER = "v" * 43


class Harness(NamedTuple):
    provider: IdentityProvider   # the port under test
    fake: FakeProvider           # the realm behind it, for the switches
    clock: ManualClock

    def visit(self, authorization_url: str) -> dict[str, str]:
        """The browser's visit to the authorization address: the redirect back's query."""
        if isinstance(self.provider, FakeProvider):
            back = self.fake.approve(authorization_url)
        else:
            response = httpx.get(authorization_url, follow_redirects=False)
            assert response.status_code == 302, response.text
            back = response.headers["location"]
        assert back.startswith(REDIRECT + "?")
        return {key: values[0] for key, values in parse_qs(urlsplit(back).query).items()}

    def code(self, *, nonce: str = "nonce-1", verifier: str = VERIFIER) -> str:
        url = self.provider.authorization_url(redirect_uri=REDIRECT, state="state-1", nonce=nonce,
                                              code_challenge=policy.code_challenge(verifier))
        return self.visit(url)["code"]

    def signed_in(self) -> TokenSet:
        return self.provider.exchange(code=self.code(), redirect_uri=REDIRECT, code_verifier=VERIFIER)

    def judge(self) -> policy.TokenJudge:
        return policy.TokenJudge(issuer=self.fake.issuer, audience=CLIENT_ID,
                                 roles_path=("resource_access", CLIENT_ID, "roles"),
                                 role_names={"owner": "owner"}, signature=self.provider.signature)


@pytest.fixture(params=["fake", "client"])
def harness(request):
    clock = ManualClock(NOW)
    fake = FakeProvider(clock=clock)
    fake.add_user("user-1", "Ada Owner")
    if request.param == "fake":
        yield Harness(fake, fake, clock)
        return
    with fake.serving():
        yield Harness(OidcProvider(issuer=fake.issuer, client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                                   clock=clock), fake, clock)


def test_the_authorization_address_asks_for_a_code_with_pkce_state_and_nonce(harness):
    url = harness.provider.authorization_url(redirect_uri=REDIRECT, state="state-1", nonce="nonce-1",
                                             code_challenge="challenge-1")
    assert url.startswith(harness.fake.issuer + "/authorize?")
    query = {key: values for key, values in parse_qs(urlsplit(url).query).items()}
    assert query == {"response_type": ["code"], "scope": ["openid"], "client_id": [CLIENT_ID],
                     "redirect_uri": [REDIRECT], "state": ["state-1"], "nonce": ["nonce-1"],
                     "code_challenge": ["challenge-1"], "code_challenge_method": ["S256"]}
    assert CLIENT_SECRET not in url


def test_a_code_is_exchanged_for_tokens_the_issuer_signed(harness):
    tokens = harness.signed_in()
    assert tokens.id_token and tokens.refresh_token
    judge = harness.judge()
    identity = judge.identity(tokens.id_token, "nonce-1", NOW)
    judgement = judge.access(tokens.access_token, NOW)
    assert identity.subject == judgement.subject == "user-1"
    assert identity.name == "Ada Owner" and judgement.roles == {"owner"}
    assert identity.provider_session and identity.provider_session == judgement.provider_session
    assert tokens.access_token not in repr(tokens) and tokens.refresh_token not in repr(tokens)


def test_a_code_is_used_once(harness):
    code = harness.code()
    harness.provider.exchange(code=code, redirect_uri=REDIRECT, code_verifier=VERIFIER)
    with pytest.raises(ProviderRefused) as refused:
        harness.provider.exchange(code=code, redirect_uri=REDIRECT, code_verifier=VERIFIER)
    assert refused.value.code == "invalid_grant"


def test_a_code_needs_its_own_verifier_and_redirect_address(harness):
    with pytest.raises(ProviderRefused):
        harness.provider.exchange(code=harness.code(), redirect_uri=REDIRECT, code_verifier="w" * 43)
    with pytest.raises(ProviderRefused):
        harness.provider.exchange(code=harness.code(), redirect_uri=REDIRECT + "x", code_verifier=VERIFIER)
    with pytest.raises(ProviderRefused):
        harness.provider.exchange(code="never-issued", redirect_uri=REDIRECT, code_verifier=VERIFIER)


def test_a_refresh_answers_new_tokens_and_the_roles_as_they_are_now(harness):
    first = harness.signed_in()
    harness.clock.advance(200)
    second = harness.provider.refresh(first.refresh_token)
    assert second.access_token != first.access_token
    judgement = harness.judge().access(second.access_token, harness.clock.utc())
    assert judgement.roles == {"owner"} and judgement.fresh_until == NOW + 200 + 300
    harness.fake.drop_role("user-1")
    third = harness.provider.refresh(second.refresh_token)
    # Without the role the token is still Central's (`azp`): it stands and carries no role.
    assert harness.judge().access(third.access_token, harness.clock.utc()).roles == frozenset()


def test_a_refresh_is_refused_for_a_disabled_user_an_ended_session_and_a_used_token(harness):
    tokens = harness.signed_in()
    harness.fake.disable("user-1")
    with pytest.raises(ProviderRefused) as refused:
        harness.provider.refresh(tokens.refresh_token)
    assert refused.value.code == "invalid_grant"

    harness.fake.users["user-1"].enabled = True
    tokens = harness.signed_in()
    sid = harness.judge().access(tokens.access_token, NOW).provider_session
    harness.fake.end_session(sid)
    with pytest.raises(ProviderRefused):
        harness.provider.refresh(tokens.refresh_token)

    tokens = harness.signed_in()
    harness.provider.refresh(tokens.refresh_token)
    with pytest.raises(ProviderRefused):
        harness.provider.refresh(tokens.refresh_token)
    with pytest.raises(ProviderRefused):
        harness.provider.refresh("never-issued")


def test_a_provider_that_does_not_answer_is_unreachable_never_a_refusal(harness):
    tokens = harness.signed_in()
    code = harness.code()
    harness.fake.unreachable = True
    with pytest.raises(ProviderUnreachable):
        harness.provider.refresh(tokens.refresh_token)
    with pytest.raises(ProviderUnreachable):
        harness.provider.exchange(code=code, redirect_uri=REDIRECT, code_verifier=VERIFIER)
    with pytest.raises(ProviderUnreachable):
        harness.provider.authorization_url(redirect_uri=REDIRECT, state="s", nonce="n", code_challenge="c")
    assert harness.provider.reach().reachable is False
    harness.fake.unreachable = False
    # Nothing was used up while it was silent.
    assert harness.provider.refresh(tokens.refresh_token).access_token
    assert harness.provider.exchange(code=code, redirect_uri=REDIRECT, code_verifier=VERIFIER).id_token


def test_reach_says_whether_the_provider_answered_and_when_it_last_did(harness):
    assert harness.provider.reach() == Reach(True, NOW)
    harness.clock.advance(30)
    harness.fake.unreachable = True
    assert harness.provider.reach() == Reach(False, NOW)
    harness.fake.unreachable = False
    assert harness.provider.reach() == Reach(True, NOW + 30)


def test_a_signature_is_checked_under_the_issuers_keys_alone(harness):
    token = harness.fake.access_token("user-1")
    claims = harness.provider.signature(token, "RS256", harness.fake.key_id)
    assert claims["sub"] == "user-1"
    assert harness.provider.signature(token, "RS256", "another-key") is SignatureRefusal.UNKNOWN_KEY
    assert harness.provider.signature(token[:-3] + "AAA", "RS256", harness.fake.key_id) is SignatureRefusal.SIGNATURE
    assert harness.provider.signature(token, "HS256", harness.fake.key_id) is SignatureRefusal.SIGNATURE
    stranger = FakeProvider(clock=harness.clock)
    stranger.add_user("user-1")
    stranger.rotate_keys()
    forged = stranger.sign(stranger.access_claims("user-1"), key_id=harness.fake.key_id)
    assert harness.provider.signature(forged, "RS256", harness.fake.key_id) is SignatureRefusal.SIGNATURE


def test_the_end_session_address_carries_the_hint_and_the_way_back(harness):
    harness.provider.reach()
    url = harness.provider.end_session_url(id_token_hint="the-id-token",
                                           post_logout_redirect_uri="http://wall.test/")
    assert url.startswith(harness.fake.issuer + "/logout?")
    assert parse_qs(urlsplit(url).query) == {
        "client_id": [CLIENT_ID], "id_token_hint": ["the-id-token"],
        "post_logout_redirect_uri": ["http://wall.test/"]}
    without = harness.provider.end_session_url(id_token_hint=None, post_logout_redirect_uri="http://wall.test/")
    assert "id_token_hint" not in without


# ---- the production client alone ----

@pytest.fixture
def served():
    clock = ManualClock(NOW)
    fake = FakeProvider(clock=clock)
    fake.add_user("user-1", "Ada Owner")
    with fake.serving():
        yield Harness(OidcProvider(issuer=fake.issuer, client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                                   clock=clock), fake, clock)


def test_a_provider_that_is_down_at_construction_is_only_unreachable_until_it_answers():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    closed = f"http://127.0.0.1:{listener.getsockname()[1]}"
    listener.close()
    client = OidcProvider(issuer=closed, client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                          clock=ManualClock(NOW))
    with pytest.raises(ProviderUnreachable):
        client.authorization_url(redirect_uri=REDIRECT, state="s", nonce="n", code_challenge="c")
    with pytest.raises(ProviderUnreachable):
        client.refresh("refresh-token")
    with pytest.raises(ProviderUnreachable):
        client.signature("a.b.c", "RS256", "k")
    assert client.reach() == Reach(False, None)
    assert client.end_session_url(id_token_hint=None, post_logout_redirect_uri="http://wall.test/") is None


def test_an_unknown_key_refetches_the_keys_once_at_most_once_a_minute(served):
    old = served.fake.access_token("user-1")
    assert served.provider.signature(old, "RS256", served.fake.key_id)["sub"] == "user-1"
    served.fake.rotate_keys()
    new = served.fake.access_token("user-1")
    # Inside the minute the keys in hand stand: the new key is unknown, and nothing is fetched.
    served.clock.advance(JWKS_REFETCH_SECONDS - 1)
    assert served.provider.signature(new, "RS256", served.fake.key_id) is SignatureRefusal.UNKNOWN_KEY
    served.clock.advance(1)
    assert served.provider.signature(new, "RS256", served.fake.key_id)["sub"] == "user-1"
    # The refetch just happened: a key nobody has is refused without another one.
    served.fake.unreachable = True
    assert served.provider.signature(new, "RS256", "nobody's") is SignatureRefusal.UNKNOWN_KEY
    served.clock.advance(JWKS_REFETCH_SECONDS)
    with pytest.raises(ProviderUnreachable):
        served.provider.signature(new, "RS256", "nobody's")


def _client(handler) -> OidcProvider:
    return OidcProvider(issuer="https://id.test/realms/home", client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                        clock=ManualClock(NOW), client=httpx.Client(transport=httpx.MockTransport(handler)))


METADATA = {"issuer": "https://id.test/realms/home", "token_endpoint": "https://id.test/token",
            "authorization_endpoint": "https://id.test/authorize", "jwks_uri": "https://id.test/jwks"}


@pytest.mark.parametrize("status, refused", [(400, True), (401, True), (403, True), (404, False), (429, False),
                                             (500, False), (502, False), (503, False)])
def test_only_an_answered_no_is_a_refusal(status, refused):
    def handler(request):
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=METADATA)
        return httpx.Response(status, json={"error": "invalid_grant"})

    with pytest.raises(ProviderRefused if refused else ProviderUnreachable):
        _client(handler).refresh("refresh-token")


@pytest.mark.parametrize("answer", [
    httpx.Response(200, text="<html>a proxy's page</html>"),
    httpx.Response(200, json=["not", "an", "object"]),
    httpx.Response(200, json={"token_type": "Bearer"}),
    httpx.Response(200, json={"access_token": 7}),
])
def test_an_answer_that_is_not_the_token_endpoints_is_unreachable_not_a_refusal(answer):
    def handler(request):
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=METADATA)
        return answer

    with pytest.raises(ProviderUnreachable):
        _client(handler).refresh("refresh-token")


def test_a_timeout_and_a_dropped_connection_are_unreachable():
    for error in (httpx.ConnectTimeout("slow"), httpx.ReadTimeout("slow"), httpx.ConnectError("refused"),
                  httpx.RemoteProtocolError("dropped")):
        def handler(request, error=error):
            if request.url.path.endswith("openid-configuration"):
                return httpx.Response(200, json=METADATA)
            raise error

        with pytest.raises(ProviderUnreachable):
            _client(handler).refresh("refresh-token")


def test_metadata_naming_another_issuer_is_not_this_providers_answer():
    def handler(request):
        return httpx.Response(200, json={**METADATA, "issuer": "https://evil.test/realms/home"})

    with pytest.raises(ProviderUnreachable):
        _client(handler).authorization_url(redirect_uri=REDIRECT, state="s", nonce="n", code_challenge="c")


def test_the_metadata_is_read_at_first_use_and_again_after_a_call_fails():
    reads, fail = [], [False]

    def handler(request):
        if request.url.path.endswith("openid-configuration"):
            reads.append(1)
            return httpx.Response(200, json=METADATA)
        if fail[0]:
            return httpx.Response(503)
        return httpx.Response(200, json={"access_token": "a.b.c"})

    client = _client(handler)
    assert reads == []                       # nothing is asked at construction
    client.refresh("r")
    client.refresh("r")
    assert len(reads) == 1
    fail[0] = True
    with pytest.raises(ProviderUnreachable):
        client.refresh("r")
    fail[0] = False
    client.refresh("r")
    assert len(reads) == 2


def test_the_client_secret_travels_as_basic_credentials_and_the_provider_error_is_a_word():
    seen = []

    def handler(request):
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=METADATA)
        seen.append(request)
        return httpx.Response(400, json={"error": "<script>\nnot a word"})

    with pytest.raises(ProviderRefused) as refused:
        _client(handler).exchange(code="c", redirect_uri=REDIRECT, code_verifier=VERIFIER)
    assert refused.value.code == "refused"
    request = seen[0]
    assert request.headers["authorization"].startswith("Basic ")
    body = parse_qs(request.content.decode())
    assert body == {"grant_type": ["authorization_code"], "code": ["c"], "redirect_uri": [REDIRECT],
                    "code_verifier": [VERIFIER]}
    assert CLIENT_SECRET not in str(request.url) and CLIENT_SECRET not in request.content.decode()
