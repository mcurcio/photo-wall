"""The one OpenID Connect client (decision 0035): the only module that speaks to the identity
provider (httpx), implementing `central.operator_access.ports.IdentityProvider`. Only
`central.app` and `central.operator_auth` import it (import-linter contract "Only the operator's
door reaches the identity provider's client").

- **Metadata** (`<issuer>/.well-known/openid-configuration`) is read at first use and again
  after a call fails, never at Central's start: a provider that is down does not stop Central.
  Metadata naming another issuer is not this provider's answer.
- **Keys** (the JWKS) are fetched at first use. A token naming a key id the set in hand does not
  hold refetches it once, at most once every `JWKS_REFETCH_SECONDS`, then is refused.
- **Refusal or silence.** A 400, 401 or 403 from the token endpoint is the provider's "no"
  (`ProviderRefused`, with its `error` word). No connection, a timeout, any other status and an
  answer that is not JSON of the expected shape are `ProviderUnreachable`: never a refusal.
- **Secrets.** The client secret travels as HTTP Basic credentials; tokens travel in bodies. No
  log line here carries a body, a token or the secret (httpx's own request log line names the
  method, address and status only, and no address here carries a secret).

It judges nothing: `signature` checks a signature under the issuer's keys
(central/infra/jwt_signature.py), and every claim is `policy.TokenJudge`'s.
"""
from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import quote, urlencode

import httpx

from central.infra.jwt_signature import Jwks, verified_claims
from central.operator_access.model import (
    Claims,
    ProviderRefused,
    ProviderUnreachable,
    Reach,
    SignatureRefusal,
    TokenSet,
)
from contracts.time import Clock

TIMEOUT_SECONDS: Final = 5.0
JWKS_REFETCH_SECONDS: Final = 60.0
DISCOVERY_PATH: Final = "/.well-known/openid-configuration"
_REFUSAL_STATUSES: Final = frozenset({400, 401, 403})
_ERROR_WORD: Final = re.compile(r"[a-z_]{1,40}")   # RFC 6749 §5.2's error codes; the only provider text logged


class OidcProvider:
    """`client`: the httpx client to ask with (a test's transport); None makes one that follows
    no redirect and waits `TIMEOUT_SECONDS`."""

    def __init__(self, *, issuer: str, client_id: str, client_secret: str, clock: Clock,
                 client: httpx.Client | None = None) -> None:
        self._issuer = issuer
        self._client_id = client_id
        # RFC 6749 §2.3.1: each part form-encoded before Basic's base64.
        self._auth = httpx.BasicAuth(quote(client_id, safe=""), quote(client_secret, safe=""))
        self._clock = clock
        self._http = client or httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False)
        self._lock = threading.Lock()
        self._metadata: Mapping[str, Any] | None = None
        self._jwks: Jwks | None = None
        self._jwks_fetched_at: float | None = None   # monotonic
        self._reachable = False
        self._last_reached_at: float | None = None

    # ---- asking ----

    def _ask(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """One answer from the provider. ProviderUnreachable for no answer or a 5xx; the metadata
        in hand is dropped then, so the next call reads it again."""
        try:
            response = self._http.request(method, url, **kwargs)
        except (httpx.HTTPError, httpx.InvalidURL):
            self._unreached()
            raise ProviderUnreachable() from None
        if response.status_code >= 500:
            self._unreached()
            raise ProviderUnreachable()
        with self._lock:
            self._reachable, self._last_reached_at = True, self._clock.utc()
        return response

    def _unreached(self) -> None:
        with self._lock:
            self._reachable, self._metadata = False, None

    def _json(self, response: httpx.Response) -> Mapping[str, Any]:
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code != 200 or not isinstance(body, dict):
            self._unreached()
            raise ProviderUnreachable()
        return body

    def _read_metadata(self) -> Mapping[str, Any]:
        metadata = self._json(self._ask("GET", self._issuer.rstrip("/") + DISCOVERY_PATH))
        if metadata.get("issuer") != self._issuer:
            self._unreached()
            raise ProviderUnreachable()
        with self._lock:
            self._metadata = metadata
        return metadata

    def _endpoint(self, name: str) -> str:
        with self._lock:
            metadata = self._metadata
        if metadata is None:
            metadata = self._read_metadata()
        endpoint = metadata.get(name)
        if not isinstance(endpoint, str) or not endpoint.startswith(("https://", "http://")):
            self._unreached()
            raise ProviderUnreachable()
        return endpoint

    # ---- the port ----

    def authorization_url(self, *, redirect_uri: str, state: str, nonce: str,
                          code_challenge: str) -> str:
        endpoint = self._endpoint("authorization_endpoint")
        query = urlencode({
            "response_type": "code", "scope": "openid", "client_id": self._client_id,
            "redirect_uri": redirect_uri, "state": state, "nonce": nonce,
            "code_challenge": code_challenge, "code_challenge_method": "S256"})
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"

    def exchange(self, *, code: str, redirect_uri: str, code_verifier: str) -> TokenSet:
        return self._tokens({"grant_type": "authorization_code", "code": code,
                             "redirect_uri": redirect_uri, "code_verifier": code_verifier})

    def refresh(self, refresh_token: str) -> TokenSet:
        return self._tokens({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def _tokens(self, form: Mapping[str, str]) -> TokenSet:
        response = self._ask("POST", self._endpoint("token_endpoint"), data=dict(form),
                             auth=self._auth, headers={"Accept": "application/json"})
        if response.status_code in _REFUSAL_STATUSES:
            try:
                error = response.json().get("error")
            except (ValueError, AttributeError):
                error = None
            raise ProviderRefused(error if isinstance(error, str) and _ERROR_WORD.fullmatch(error) else "refused")
        body = self._json(response)
        access_token = body.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            self._unreached()
            raise ProviderUnreachable()
        return TokenSet(access_token=access_token, id_token=_text(body.get("id_token")),
                        refresh_token=_text(body.get("refresh_token")))

    def signature(self, token: str, algorithm: str, key_id: str | None) -> Claims | SignatureRefusal:
        with self._lock:
            jwks, fetched_at = self._jwks, self._jwks_fetched_at
        if jwks is None:
            jwks, fetched_at = self._fetch_jwks(), None
        answer = verified_claims(token, algorithm, key_id, jwks)
        if answer is not SignatureRefusal.UNKNOWN_KEY or fetched_at is None:
            return answer   # fetched_at None: these keys were fetched for this very token
        if self._clock.monotonic() - fetched_at < JWKS_REFETCH_SECONDS:
            return answer
        return verified_claims(token, algorithm, key_id, self._fetch_jwks())

    def _fetch_jwks(self) -> Jwks:
        jwks = self._json(self._ask("GET", self._endpoint("jwks_uri")))
        with self._lock:
            self._jwks, self._jwks_fetched_at = jwks, self._clock.monotonic()
        return jwks

    def end_session_url(self, *, id_token_hint: str | None,
                        post_logout_redirect_uri: str) -> str | None:
        with self._lock:
            metadata = self._metadata
        try:
            endpoint = (metadata if metadata is not None else self._read_metadata()).get("end_session_endpoint")
        except ProviderUnreachable:
            return None
        if not isinstance(endpoint, str) or not endpoint.startswith(("https://", "http://")):
            return None
        query = {"client_id": self._client_id, "post_logout_redirect_uri": post_logout_redirect_uri}
        if id_token_hint:
            query["id_token_hint"] = id_token_hint
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{urlencode(query)}"

    def reach(self) -> Reach:
        try:
            self._read_metadata()
        except ProviderUnreachable:
            pass
        with self._lock:
            return Reach(reachable=self._reachable, last_reached_at=self._last_reached_at)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
