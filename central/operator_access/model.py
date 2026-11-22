"""Operator access's vocabulary (decision 0035): the principals, roles and permissions, a session
and a sign-in transaction as Central keeps them, what the provider hands back, and the one refusal
vocabulary. Values only; every rule is `policy.py`.

A principal is who a request is. Central names it in audit through `central/actor.py`
(`actor_of` in `policy.py`), never by a name or e-mail, which the provider may change.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal, get_args

Role = Literal["owner"]
"""Central's roles. One today; #136 adds more as rows of `policy.PERMISSIONS`."""
ROLES: Final[frozenset[Role]] = frozenset(get_args(Role))

Permission = Literal["read", "operate", "configure"]
"""What a route asks for: reads `read`, everyday actions `operate`, every other write
`configure`."""

OperatorKind = Literal["person", "client"]
"""How an operator was first seen: a person through the console, or a machine (a service
account) with a provider token."""

Claims = Mapping[str, object]
"""A token's payload, read only after its signature verified."""


@dataclass(frozen=True)
class Person:
    """A signed-in person: one `operator_sessions` row, presented as the session cookie."""
    operator_id: str
    name: str
    roles: frozenset[Role]
    session_id: str
    origin: str


@dataclass(frozen=True)
class Machine:
    """A machine with a provider access token (`Authorization: Bearer`)."""
    operator_id: str
    name: str
    roles: frozenset[Role]


@dataclass(frozen=True)
class BreakGlass:
    """The environment token (`PHOTO_WALL_ADMIN_TOKEN`) as a Bearer credential: every permission,
    named `api-token` in audit."""


@dataclass(frozen=True)
class SharedConsole:
    """The console in token mode (no provider): pass A's signed cookie, which names nobody. Every
    permission, named `console` in audit."""
    origin: str


Principal = Person | Machine | BreakGlass | SharedConsole


@dataclass(frozen=True)
class Operator:
    """Central's own record of one provider identity `(issuer, subject)`."""
    operator_id: str
    kind: OperatorKind
    name: str


@dataclass(frozen=True)
class Session:
    """A signed-in browser as a request reads it. It holds no token: the refresh and ID tokens
    are `SessionSecrets`, read only where they are used."""
    session_id: str          # the SHA-256 of the cookie's value, hex: the row's key
    operator_id: str
    name: str                # the operator's name as last stated
    origin: str              # the Origin that signed in; a write must carry it
    provider_session: str | None   # the provider's session id (`sid`)
    roles: frozenset[Role]   # as last judged
    fresh_until: float       # the judgement is re-made from this instant on
    hard_end: float          # the session is over from this instant on, whatever the provider says


@dataclass(frozen=True)
class SessionSecrets:
    """The provider's tokens a session keeps: secrets at rest (decision 0020's rule). Never in a
    route's answer or a log line, so `repr` hides them."""
    refresh_token: str | None
    id_token: str | None

    def __repr__(self) -> str:
        return "SessionSecrets(<hidden>)"


@dataclass(frozen=True)
class NewSession:
    """What sign-in writes: the session and its secrets together."""
    session: Session
    secrets: SessionSecrets


@dataclass(frozen=True)
class Transaction:
    """One sign-in under way, carried sealed in the browser's transaction cookie; nothing is
    written to the database for a visitor who never finishes."""
    state: str
    nonce: str
    code_verifier: str
    origin: str
    return_to: str           # a console hash route ("#/…"), or "" for Home
    expires_at: float


@dataclass(frozen=True)
class TokenSet:
    """The provider's answer to a code exchange or a refresh."""
    access_token: str
    id_token: str | None
    refresh_token: str | None

    def __repr__(self) -> str:
        return "TokenSet(<hidden>)"


@dataclass(frozen=True)
class Reach:
    """Whether the provider answered Central's last question, and when it last did."""
    reachable: bool
    last_reached_at: float | None


class TokenRefusal(StrEnum):
    """Why a token is refused. One vocabulary for the ID token, an access token and a logout
    token."""
    MALFORMED = "malformed"
    ALGORITHM = "algorithm"          # not on the allow-list (`none` and HMAC among them)
    UNKNOWN_KEY = "unknown_key"
    SIGNATURE = "signature"
    ISSUER = "issuer"
    AUDIENCE = "audience"
    EXPIRED = "expired"
    NOT_YET_VALID = "not_yet_valid"
    NONCE = "nonce"
    SUBJECT = "subject"              # no `sub`
    NOT_AN_ACCESS_TOKEN = "not_an_access_token"   # an ID or logout token sent as a credential
    ISSUED_TO_CENTRAL = "issued_to_central"   # a person's sign-in token sent as a Bearer credential
    NOT_A_LOGOUT = "not_a_logout"    # a logout token without its event, or with a nonce
    NO_SESSION_NAMED = "no_session_named"   # a logout token with neither `sid` nor `sub`


class SignatureRefusal(StrEnum):
    """What a signature check answers instead of claims."""
    UNKNOWN_KEY = "unknown_key"
    SIGNATURE = "signature"


class SignInRefusal(StrEnum):
    """Why a sign-in did not sign in: the console's `?sign_in=<code>`."""
    NOT_ALLOWED = "not_allowed"                    # signed in at the provider, lacks the role
    PROVIDER_UNREACHABLE = "provider_unreachable"
    FAILED = "failed"                              # anything else


class TokenRefused(Exception):
    """A token Central does not accept."""

    def __init__(self, reason: TokenRefusal) -> None:
        self.reason = reason
        super().__init__(reason.value)


class ProviderRefused(Exception):
    """The provider answered and said no (a used code, a revoked or expired refresh token, a
    disabled user). `code` is the provider's error word, never a token."""

    def __init__(self, code: str = "refused") -> None:
        self.code = code
        super().__init__(code)


class ProviderUnreachable(Exception):
    """The provider did not answer, or not as a provider does (no connection, a timeout, a 5xx,
    an answer that is not its protocol's). Never a refusal."""


class NotAllowed(Exception):
    """A valid token whose principal holds no role Central knows."""


@dataclass(frozen=True)
class Rejudged:
    """A stale session's refresh answered and the role still stands."""
    roles: frozenset[Role]
    fresh_until: float
    secrets: SessionSecrets


@dataclass(frozen=True)
class Ended:
    """The session is over: the provider refused, the role is gone, or the hard end came."""


@dataclass(frozen=True)
class CarriedOn:
    """The provider did not answer: the last judgement stands; ask again from `retry_at`."""
    retry_at: float


Standing = Literal["fresh", "stale", "ended"]
