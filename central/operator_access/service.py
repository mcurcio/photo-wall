"""Provider mode's use cases (decision 0035), over the ports alone: sign-in's two steps, who a
cookie or a Bearer token is, staying judged, Log out and back-channel logout. Pure of I/O
libraries; every rule it applies is `policy.py`'s.

Staying judged, one refresh per session at a time: a stale session's refresh runs under
`_refreshing` (this process's set of sessions being refreshed; Central's API is one process).
A request that finds its session there, or inside the minute after an unanswered refresh,
carries on with the last judgement and asks nothing. The store's write is a compare-and-set on
the refresh token it replaces, and happens only when the re-judgement changes the row.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Final

from central.actor import Actor, encode
from central.operator_access import policy
from central.operator_access.model import (
    CarriedOn,
    Ended,
    Machine,
    NewSession,
    Person,
    ProviderRefused,
    ProviderUnreachable,
    Reach,
    Session,
    SessionSecrets,
    SignInRefusal,
    TokenRefused,
    Transaction,
)
from central.operator_access.ports import IdentityProvider, Operators, Sessions
from central.operator_access.settings import OidcSettings
from contracts.time import Clock

CENTRAL: Final[Actor] = "central"   # an operator first seen and a session re-judged: Central's own writes


class SignInRefused(Exception):
    """A sign-in that signed nobody in, as the console's `?sign_in=<code>` says it."""

    def __init__(self, refusal: SignInRefusal, detail: str = "") -> None:
        self.refusal = refusal
        self.detail = detail   # the rule that refused, for the log: never a token
        super().__init__(refusal.value)


@dataclass(frozen=True)
class SignedIn:
    """A finished sign-in: the cookie's value (told once, never kept) and the session."""
    cookie_value: str
    session: Session


class ProviderAccess:
    """Operator access through the configured provider."""

    def __init__(self, settings: OidcSettings, provider: IdentityProvider, sessions: Sessions,
                 operators: Operators, clock: Clock) -> None:
        self.settings = settings
        self._provider = provider
        self._sessions = sessions
        self._operators = operators
        self._clock = clock
        self._judge = policy.TokenJudge(
            issuer=settings.issuer, audience=settings.client_id, roles_path=settings.roles_claim,
            role_names=settings.role_names, signature=provider.signature)
        self._mutex = threading.Lock()
        self._refreshing: set[str] = set()
        self._retry_at: dict[str, float] = {}

    # ---- sign-in ----

    def begin(self, origin: str, return_to: str | None, callback_path: str) -> tuple[str, Transaction]:
        """The provider's address to send the browser to, and the transaction to seal into its
        cookie. ProviderUnreachable when the provider's metadata cannot be read."""
        transaction = policy.new_transaction(origin, return_to, self._clock.utc())
        url = self._provider.authorization_url(
            redirect_uri=policy.redirect_address(origin, callback_path), state=transaction.state,
            nonce=transaction.nonce, code_challenge=policy.code_challenge(transaction.code_verifier))
        return url, transaction

    def finish(self, transaction: Transaction | None, *, code: str | None, state: str | None,
               callback_path: str) -> SignedIn:
        """Exchange the code, judge both tokens and the role, and open the session.
        SignInRefused: `failed` (no or another transaction, no code, a code the provider refuses,
        a token refused), `provider_unreachable`, `not_allowed` (signed in, lacks the role);
        nothing is written for any of them."""
        now = self._clock.utc()
        if transaction is None or not code or not policy.transaction_stands(transaction, state, now):
            raise SignInRefused(SignInRefusal.FAILED, "transaction")
        try:
            tokens = self._provider.exchange(
                code=code, redirect_uri=policy.redirect_address(transaction.origin, callback_path),
                code_verifier=transaction.code_verifier)
            if tokens.id_token is None:
                raise SignInRefused(SignInRefusal.FAILED, "no ID token")
            identity = self._judge.identity(tokens.id_token, transaction.nonce, now)
            judgement = self._judge.access(tokens.access_token, now)
        except ProviderUnreachable:
            raise SignInRefused(SignInRefusal.PROVIDER_UNREACHABLE) from None
        except ProviderRefused as refused:
            raise SignInRefused(SignInRefusal.FAILED, f"provider: {refused.code}") from None
        except TokenRefused as refused:
            raise SignInRefused(SignInRefusal.FAILED, f"token: {refused.reason.value}") from None
        if judgement.subject != identity.subject:
            raise SignInRefused(SignInRefusal.FAILED, "two subjects")
        if not policy.allows(judgement.roles, "read"):
            raise SignInRefused(SignInRefusal.NOT_ALLOWED)
        operator = self._operators.seen(issuer=self.settings.issuer, subject=identity.subject,
                                        kind="person", name=identity.name, by=CENTRAL)
        cookie_value = policy.new_cookie_value()
        session = Session(
            session_id=policy.session_id_of(cookie_value), operator_id=operator.operator_id,
            name=operator.name, origin=transaction.origin,
            provider_session=identity.provider_session or judgement.provider_session,
            roles=judgement.roles, fresh_until=judgement.fresh_until, hard_end=policy.hard_end(now))
        self._sessions.open(
            NewSession(session, SessionSecrets(tokens.refresh_token, tokens.id_token)),
            by=encode("person", operator.operator_id))
        return SignedIn(cookie_value, session)

    # ---- who a request is ----

    def person(self, cookie_value: str | None) -> Person | None:
        """Who a session cookie is, its judgement re-made first when it is stale; None when it
        names no session, or one that is over."""
        if not cookie_value:
            return None
        session = self._sessions.find(policy.session_id_of(cookie_value))
        if session is None:
            return None
        now = self._clock.utc()
        standing = policy.standing(session, now)
        if standing == "stale":
            session = self._refreshed(session, now)
        if session is None or standing == "ended":
            return None
        return Person(operator_id=session.operator_id, name=session.name, roles=session.roles,
                      session_id=session.session_id, origin=session.origin)

    def machine(self, token: str) -> Machine | None:
        """Who a provider access token sent as a Bearer credential is: a machine holding a role
        that may read, or None. A token issued to Central's own client is a person's and never a
        machine's (`TokenJudge.credential`). A provider whose keys cannot be fetched lets no
        machine in."""
        try:
            judgement = self._judge.credential(token, self._clock.utc())
        except (TokenRefused, ProviderUnreachable):
            return None
        if not policy.allows(judgement.roles, "read"):
            return None
        operator = self._operators.seen(issuer=self.settings.issuer, subject=judgement.subject,
                                        kind="client", name=judgement.name, by=CENTRAL)
        return Machine(operator_id=operator.operator_id, name=operator.name, roles=judgement.roles)

    def _refreshed(self, session: Session, now: float) -> Session | None:
        """The stale session after its refresh: re-judged, ended (None), or carried on as it is."""
        session_id = session.session_id
        with self._mutex:
            if session_id in self._refreshing or self._retry_at.get(session_id, 0.0) > now:
                return session
            self._refreshing.add(session_id)
        try:
            # Read again inside the turn: the refresh this request waited behind may have landed.
            current = self._sessions.find(session_id)
            secrets_kept = self._sessions.secrets(session_id)
            if current is None or secrets_kept is None:
                return None
            if policy.standing(current, now) != "stale":
                return current
            outcome = policy.rejudge(secrets_kept, refresh=self._provider.refresh,
                                     judge=lambda token: self._judge.access(token, now), now=now)
            if isinstance(outcome, CarriedOn):
                with self._mutex:
                    self._retry_at = {key: at for key, at in self._retry_at.items() if at > now}
                    self._retry_at[session_id] = outcome.retry_at
                return current
            if isinstance(outcome, Ended):
                self._sessions.end(session_id)
                return None
            if not self._sessions.rejudged(session_id, was=secrets_kept, now=outcome, by=CENTRAL):
                return self._sessions.find(session_id)   # another refresh won: its judgement stands
            return replace(current, roles=outcome.roles, fresh_until=outcome.fresh_until)
        finally:
            with self._mutex:
                self._refreshing.discard(session_id)

    # ---- the end ----

    def log_out(self, cookie_value: str | None, console_path: str) -> str | None:
        """End the cookie's session and answer the provider's end-session address to go to (with
        the kept ID token as its hint, coming back to the console), or None."""
        if not cookie_value:
            return None
        session_id = policy.session_id_of(cookie_value)
        session = self._sessions.find(session_id)
        kept = self._sessions.end(session_id)
        if session is None or kept is None:
            return None
        return self._provider.end_session_url(
            id_token_hint=kept.id_token, post_logout_redirect_uri=session.origin + console_path)

    def backchannel_logout(self, logout_token: str) -> int:
        """The provider says a session ended: end every session it names, at once; how many.
        TokenRefused for a token that is not this issuer's logout token; ProviderUnreachable when
        its keys cannot be fetched."""
        notice = self._judge.logout(logout_token, self._clock.utc())
        if notice.provider_session is not None:
            return self._sessions.end_named(provider_session=notice.provider_session)
        operator = self._operators.find(issuer=self.settings.issuer, subject=notice.subject or "")
        return 0 if operator is None else self._sessions.end_named(operator_id=operator.operator_id)

    def reach(self) -> Reach:
        return self._provider.reach()
