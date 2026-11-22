"""Operator access's ports (decision 0035). The adapters: central/infra/oidc_provider.py (the one
OpenID Connect client) and central/infra/operator_session_store.py (`operators`,
`operator_sessions`); tests/operator_provider_fake.py is the provider's substitute, held to
`IdentityProvider` by tests/test_operator_provider_conformance.py.
"""

from __future__ import annotations

from typing import Protocol

from central.actor import Actor
from central.operator_access.model import (
    Claims,
    NewSession,
    Operator,
    OperatorKind,
    Reach,
    Rejudged,
    Session,
    SessionSecrets,
    SignatureRefusal,
    TokenSet,
    Transaction,
)


class IdentityProvider(Protocol):
    """The configured issuer. Every method that asks the provider raises `ProviderUnreachable`
    when it does not answer (no connection, a timeout, a 5xx, an answer outside its protocol);
    only an answered "no" is `ProviderRefused`. It judges nothing: a token it hands back is
    judged by `policy.TokenJudge`."""

    def authorization_url(self, *, redirect_uri: str, state: str, nonce: str,
                          code_challenge: str) -> str:
        """The address a browser is sent to, to sign in: the authorization endpoint with
        `response_type=code`, `scope=openid`, Central's client id, `redirect_uri`, `state`,
        `nonce` and the PKCE challenge with method `S256`. ProviderUnreachable when the
        provider's metadata cannot be read."""

    def exchange(self, *, code: str, redirect_uri: str, code_verifier: str) -> TokenSet:
        """The tokens for an authorization code, with an ID token. ProviderRefused for a code the
        provider does not take (unknown, used before, another redirect address, a wrong
        verifier)."""

    def refresh(self, refresh_token: str) -> TokenSet:
        """New tokens for a refresh token. ProviderRefused when the provider no longer takes it
        (the user disabled, the provider's session ended, the token used up or unknown)."""

    def signature(self, token: str, algorithm: str, key_id: str | None) -> Claims | SignatureRefusal:
        """`policy.Signature` over the issuer's keys. A key id the keys in hand do not hold
        refetches them once, at most once a minute, then answers `UNKNOWN_KEY`."""

    def end_session_url(self, *, id_token_hint: str | None,
                        post_logout_redirect_uri: str) -> str | None:
        """The provider's end-session address to send the browser to at Log out, or None when the
        provider states none or its metadata is not in hand (never raises: Log out has already
        ended Central's session)."""

    def reach(self) -> Reach:
        """Ask the provider for its metadata now, and answer whether it did and when it last
        answered anything. Never raises."""


class Operators(Protocol):
    """`operators`: one row per provider identity `(issuer, subject)`."""

    def seen(self, *, issuer: str, subject: str, kind: OperatorKind, name: str,
             by: Actor) -> Operator:
        """The operator for this identity, created at first sight (with `kind`, never rewritten)
        and its name rewritten when the stated one differs; nothing is written otherwise."""

    def find(self, *, issuer: str, subject: str) -> Operator | None: ...

    def name_of(self, operator_id: str) -> str | None:
        """The operator's name as last stated: what a reader that shows "who" asks for."""


class Sessions(Protocol):
    """`operator_sessions`: one row per signed-in browser, keyed by `policy.session_id_of` its
    cookie. A request reads its row and writes nothing."""

    def open(self, new: NewSession, *, by: Actor) -> None: ...

    def find(self, session_id: str) -> Session | None:
        """The session, whatever its standing (`policy.standing` judges it)."""

    def secrets(self, session_id: str) -> SessionSecrets | None:
        """The tokens the session keeps: for the refresh and for Log out's hint only."""

    def rejudged(self, session_id: str, *, was: SessionSecrets, now: Rejudged, by: Actor) -> bool:
        """Write a re-made judgement; nothing is written when it changes nothing. False when the
        session is gone or no longer keeps the refresh token `was` held (another refresh won)."""

    def end(self, session_id: str) -> SessionSecrets | None:
        """Delete the session; what it kept (Log out's ID token hint), or None when there was
        none."""

    def end_named(self, *, provider_session: str | None = None,
                  operator_id: str | None = None) -> int:
        """Delete every session of one provider session (`sid`), or of one operator; how many."""


class TransactionSeal(Protocol):
    """The sign-in transaction's cookie value: sealed so the browser can neither read nor change
    it."""

    def seal(self, transaction: Transaction) -> str: ...

    def open(self, value: object) -> Transaction | None:
        """Total: the transaction, or None for anything this seal did not make."""
