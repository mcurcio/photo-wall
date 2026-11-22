"""Every rule of operator access, each with one home (decision 0035). Pure: a token's signature
is checked by a `Signature` it is handed (central/operator_access/ports.py; PyJWT lives behind it
in central/infra/jwt_signature.py), and time is the instant it is handed, read from Central's
clock by the caller.

- **Roles and permissions:** `roles_in`, `allows` (the one policy table, `PERMISSIONS`),
  `permits`, `permission_for`, `actor_of`.
- **Validating a token:** `TokenJudge`: the algorithm allow-list before any key is asked for,
  then the signature, then `iss`, the address (`addressed_to`: `aud` or `azp`), `exp` and `nbf`
  on Central's clock; the ID token's nonce, a logout token's event; a Bearer credential
  (`TokenJudge.credential`), which a token issued to Central's own client never is.
- **The Bearer order:** `bearer_principal`.
- **A cookie write:** `write_refusal`, pass A's three checks.
- **Staying judged:** `standing`, `rejudge`, the hard end.
- **The sign-in transaction:** `new_transaction`, `transaction_stands`, `return_route`,
  `code_challenge`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from central.actor import Actor, encode
from central.operator_access.model import (
    ROLES,
    BreakGlass,
    CarriedOn,
    Claims,
    Ended,
    Machine,
    Permission,
    Person,
    Principal,
    ProviderRefused,
    ProviderUnreachable,
    Rejudged,
    Role,
    Session,
    SessionSecrets,
    SharedConsole,
    SignatureRefusal,
    Standing,
    TokenRefusal,
    TokenRefused,
    TokenSet,
    Transaction,
)

# ---- roles and permissions -------------------------------------------------------------------

PERMISSIONS: Final[Mapping[Role, frozenset[Permission]]] = {
    "owner": frozenset({"read", "operate", "configure"}),
}
"""The one policy table: what each role may do. A guest role is one more row."""

READ_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
IDENTITY_ROLE_NAMES: Final[Mapping[str, Role]] = {role: role for role in ROLES}


def roles_in(claims: Claims, path: Sequence[str],
             names: Mapping[str, Role] = IDENTITY_ROLE_NAMES) -> frozenset[Role]:
    """The roles a token states at `path` (each part one key down), read through `names` (the
    provider's name for each of Central's roles). A missing path, a value that is not a list and
    names Central does not know are no roles, never an error: the realm may carry roles Central
    does not know yet."""
    value: object = claims
    for part in path:
        if not isinstance(value, Mapping) or part not in value:
            return frozenset()
        value = value[part]
    if not isinstance(value, (list, tuple)):
        return frozenset()
    return frozenset(names[name] for name in value if isinstance(name, str) and name in names)


def allows(roles: frozenset[Role], permission: Permission) -> bool:
    return any(permission in PERMISSIONS.get(role, frozenset()) for role in roles)


def permits(principal: Principal, permission: Permission) -> bool:
    """The break-glass token and token mode's console hold every permission; a person and a
    machine hold what their roles allow."""
    if isinstance(principal, (BreakGlass, SharedConsole)):
        return True
    return allows(principal.roles, permission)


def permission_for(method: str) -> Permission:
    """What an existing route asks for by its method: a read `read`, any other `configure`."""
    return "read" if method.upper() in READ_METHODS else "configure"


def actor_of(principal: Principal) -> Actor:
    """The audit value a principal's writes are recorded under (central/actor.py)."""
    if isinstance(principal, Person):
        return encode("person", principal.operator_id)
    if isinstance(principal, Machine):
        return encode("client", principal.operator_id)
    if isinstance(principal, BreakGlass):
        return "api-token"
    return "console"


def new_operator_id() -> str:
    """Central's own id for a provider identity (central/actor.py `OPERATOR_ID_PATTERN`)."""
    return "op_" + base64.b32encode(secrets.token_bytes(10)).decode("ascii").lower()


# ---- validating a token ----------------------------------------------------------------------

ALGORITHMS: Final = frozenset({"RS256", "PS256", "ES256"})
"""The allow-list. `none` and the HMAC family are refused whatever the provider's metadata says."""
LEEWAY_SECONDS: Final = 60.0
BACKCHANNEL_LOGOUT_EVENT: Final = "http://schemas.openid.net/event/backchannel-logout"
MAX_TOKEN_CHARS: Final = 16 * 1024

Signature = Callable[[str, str, str | None], Claims | SignatureRefusal]
"""(token, algorithm, key id) -> the token's claims once its signature verified under one of the
issuer's keys with exactly that algorithm, or why not. It judges nothing else. It may raise
`ProviderUnreachable` when the keys could not be fetched."""


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def token_header(token: object) -> Mapping[str, object]:
    """A compact JWS's header, unverified: read only to choose the algorithm and the key.
    TokenRefused(MALFORMED) for anything that is not three base64url parts with a JSON object
    first."""
    if not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS or token.count(".") != 2:
        raise TokenRefused(TokenRefusal.MALFORMED)
    first = token.split(".", 1)[0]
    try:
        header = json.loads(base64.urlsafe_b64decode(first + "=" * (-len(first) % 4)))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise TokenRefused(TokenRefusal.MALFORMED) from None
    if not isinstance(header, dict):
        raise TokenRefused(TokenRefusal.MALFORMED)
    return header


def addressed_to(claims: Claims, client_id: str) -> bool:
    """Whether a token is addressed to the client `client_id` (Central's): its `aud` contains the
    client id (one string, or a list holding it), or its `azp` equals it.

    `azp` is the party the token was issued to. Only Central, holding its client secret, can be
    issued a token as its own client, so such a token is Central's. The `azp` arm is needed
    because Keycloak's default audience mapper ("audience resolve",
    `AudienceResolveProtocolMapper.setAudience`: "Don't add client itself to the audience")
    leaves the requesting client itself out of `aud`: a person's access token issued to Central's
    client carries `azp` = that client and no `aud` naming it, while a machine's (another
    client's service account) carries Central's client in `aud` while it holds one of its roles.
    Pinned against Keycloak 26.8.2 by tests/integration/test_keycloak_sign_in.py."""
    aud = claims.get("aud")
    in_audience = aud == client_id if isinstance(aud, str) else (
        isinstance(aud, (list, tuple)) and client_id in aud)
    return in_audience or issued_to(claims, client_id)


def issued_to(claims: Claims, client_id: str) -> bool:
    """Whether a token was issued to the client `client_id`: its `azp` is exactly that id."""
    return claims.get("azp") == client_id


def name_in(claims: Claims) -> str:
    """What a token calls its principal, for people to read: never a key."""
    for key in ("name", "preferred_username", "client_id", "azp", "sub"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return ""


@dataclass(frozen=True)
class Judgement:
    """An access token judged: who it is, the roles it carries, and until when it says so."""
    subject: str
    name: str
    roles: frozenset[Role]
    fresh_until: float
    provider_session: str | None


@dataclass(frozen=True)
class Identity:
    """An ID token judged: who signed in, as the provider names them."""
    subject: str
    name: str
    provider_session: str | None


@dataclass(frozen=True)
class LogoutNotice:
    """A logout token judged: the provider session to end, else every session of the subject."""
    provider_session: str | None
    subject: str | None


@dataclass(frozen=True)
class TokenJudge:
    """The one judge of the configured issuer's tokens, for a person and a machine alike.
    `audience` is Central's client id; `roles_path` and `role_names` are where the roles are and
    what the provider calls them."""
    issuer: str
    audience: str
    roles_path: tuple[str, ...]
    role_names: Mapping[str, Role]
    signature: Signature

    def claims(self, token: str, now: float) -> Claims:
        """The claims of a token this issuer signed for Central that is valid at `now`.
        TokenRefused otherwise, by the first rule that fails, in this order: the shape, the
        algorithm allow-list (before any key is asked for), the key and signature, `iss`
        (exactly), the address (`addressed_to`: `aud` contains Central's client id, or `azp` is
        it), `exp` and `nbf` with `LEEWAY_SECONDS`."""
        header = token_header(token)
        algorithm = header.get("alg")
        if not isinstance(algorithm, str) or algorithm not in ALGORITHMS:
            raise TokenRefused(TokenRefusal.ALGORITHM)
        key_id = header.get("kid")
        verified = self.signature(token, algorithm, key_id if isinstance(key_id, str) else None)
        if isinstance(verified, SignatureRefusal):
            raise TokenRefused(TokenRefusal(verified.value))
        if verified.get("iss") != self.issuer:
            raise TokenRefused(TokenRefusal.ISSUER)
        if not addressed_to(verified, self.audience):
            raise TokenRefused(TokenRefusal.AUDIENCE)
        expires = _number(verified.get("exp"))
        if expires is None:
            raise TokenRefused(TokenRefusal.MALFORMED)
        if now >= expires + LEEWAY_SECONDS:
            raise TokenRefused(TokenRefusal.EXPIRED)
        if "nbf" in verified:
            not_before = _number(verified["nbf"])
            if not_before is None:
                raise TokenRefused(TokenRefusal.MALFORMED)
            if now < not_before - LEEWAY_SECONDS:
                raise TokenRefused(TokenRefusal.NOT_YET_VALID)
        return verified

    def access(self, token: str, now: float) -> Judgement:
        """An access token: valid, with a subject. Its roles may be none; the caller decides
        what no role means (no session, 401, the session's end)."""
        return self._access(self.claims(token, now))

    def credential(self, token: str, now: float) -> Judgement:
        """An access token presented as a Bearer credential: `access`, and not one issued to
        Central's own client (`issued_to`), which is refused.

        A token issued to Central's client is a person's, from a sign-in: Central asked for it
        with its client secret and never hands it to the browser or to anyone, so nobody is meant
        to hold it. A person is let in by the session that sign-in opened, which carries the
        origin and marker checks, the hard end and the provider's logout; a Bearer has none of
        them. A machine has its own client, so its token's `azp` is never Central's."""
        claims = self.claims(token, now)
        judgement = self._access(claims)
        if issued_to(claims, self.audience):
            raise TokenRefused(TokenRefusal.ISSUED_TO_CENTRAL)
        return judgement

    def _access(self, claims: Claims) -> Judgement:
        if not _may_be_an_access_token(claims):
            raise TokenRefused(TokenRefusal.NOT_AN_ACCESS_TOKEN)
        return Judgement(subject=_subject(claims), name=name_in(claims),
                         roles=roles_in(claims, self.roles_path, self.role_names),
                         fresh_until=float(claims["exp"]),  # a number: `claims` checked it
                         provider_session=_text(claims.get("sid")))

    def identity(self, token: str, nonce: str, now: float) -> Identity:
        """An ID token: valid, with a subject and exactly the transaction's nonce."""
        claims = self.claims(token, now)
        stated = claims.get("nonce")
        if not isinstance(stated, str) or not hmac.compare_digest(stated.encode(), nonce.encode()):
            raise TokenRefused(TokenRefusal.NONCE)
        return Identity(subject=_subject(claims), name=name_in(claims),
                        provider_session=_text(claims.get("sid")))

    def logout(self, token: str, now: float) -> LogoutNotice:
        """A logout token (OpenID Connect Back-Channel Logout 1.0 §2.6): valid, carrying the
        back-channel logout event, no nonce, and a `sid` or a `sub`."""
        claims = self.claims(token, now)
        events = claims.get("events")
        if (not isinstance(events, Mapping) or BACKCHANNEL_LOGOUT_EVENT not in events
                or "nonce" in claims):
            raise TokenRefused(TokenRefusal.NOT_A_LOGOUT)
        notice = LogoutNotice(provider_session=_text(claims.get("sid")),
                              subject=_text(claims.get("sub")))
        if notice.provider_session is None and notice.subject is None:
            raise TokenRefused(TokenRefusal.NO_SESSION_NAMED)
        return notice


# Claims only an ID token (OpenID Connect Core §2, §3.3.2.11) or a logout token carries, and the
# `typ` claim's values for an access token where a provider states one (RFC 9068; "Bearer").
_NOT_ACCESS_CLAIMS: Final = ("at_hash", "c_hash", "events")
_ACCESS_TYPES: Final = frozenset({"bearer", "at+jwt", "application/at+jwt"})


def _may_be_an_access_token(claims: Claims) -> bool:
    """An ID token and a logout token are signed by the same keys for the same audience, and
    Log out hands the ID token to the browser: neither is ever a credential, whatever roles a
    provider's mapper put in it."""
    if any(name in claims for name in _NOT_ACCESS_CLAIMS):
        return False
    stated = claims.get("typ")
    return stated is None or (isinstance(stated, str) and stated.lower() in _ACCESS_TYPES)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _subject(claims: Claims) -> str:
    subject = _text(claims.get("sub"))
    if subject is None:
        raise TokenRefused(TokenRefusal.SUBJECT)
    return subject


# ---- the Bearer order ------------------------------------------------------------------------

def bearer_principal(
    candidate: bytes,
    *,
    break_glass: Callable[[bytes], bool] | None,
    machine: Callable[[bytes], Machine | None] | None,
) -> Principal | None:
    """Who a non-empty Bearer credential is; it decides alone, and None is 401.

    1. The break-glass token is set (`break_glass`, its constant-time compare) and the bytes
       equal it: `BreakGlass`.
    2. A provider is configured (`machine`, its access-token judge): `Machine`, or None.
    3. Otherwise None.
    """
    if break_glass is not None and break_glass(candidate):
        return BreakGlass()
    if machine is not None:
        return machine(candidate)
    return None


# ---- a cookie write --------------------------------------------------------------------------

def write_refusal(*, marked: bool, origin: str | None, session_origin: str,
                  fetch_site: str | None) -> str | None:
    """Why a write authorized by a session cookie is refused, or None (pass A, unchanged): it
    must carry the console's marker, exactly the Origin that signed in (a missing one is
    refused), and `Sec-Fetch-Site: same-origin` when that header is sent."""
    if not marked:
        return "request_unmarked"
    if origin != session_origin:
        return "origin_mismatch"
    if fetch_site is not None and fetch_site != "same-origin":
        return "origin_mismatch"
    return None


# ---- staying judged --------------------------------------------------------------------------

HARD_END_SECONDS: Final = 30 * 24 * 3600
"""A session's ceiling from sign-in, whatever the provider says (the old setting #138)."""
RETRY_SECONDS: Final = 60.0
"""After a refresh the provider did not answer, the next one is no sooner than this."""


def hard_end(signed_in_at: float) -> float:
    return signed_in_at + HARD_END_SECONDS


def standing(session: Session, now: float) -> Standing:
    """`ended` from the hard end on; else `fresh` until the judgement's instant, then `stale`:
    the judgement is to be re-made before it is trusted on."""
    if now >= session.hard_end:
        return "ended"
    return "fresh" if now < session.fresh_until else "stale"


def rejudge(secrets_kept: SessionSecrets, *, refresh: Callable[[str], TokenSet],
            judge: Callable[[str], Judgement], now: float) -> Rejudged | Ended | CarriedOn:
    """A stale session's judgement re-made with its refresh token, used once.

    - `Rejudged`: the provider answered a valid access token that still carries a role; with the
      tokens to keep (a refresh or ID token the provider did not send again stays).
    - `Ended`: no refresh token was kept; the provider refused (`ProviderRefused`: the user was
      disabled, the realm session ended); the new access token is refused; or it carries no role
      that may read.
    - `CarriedOn`: the provider did not answer (`ProviderUnreachable`). **Not a refusal**: the
      last judgement stands, and the next refresh is no sooner than `RETRY_SECONDS` after `now`.
    """
    if secrets_kept.refresh_token is None:
        return Ended()
    try:
        tokens = refresh(secrets_kept.refresh_token)
        judgement = judge(tokens.access_token)
    except ProviderUnreachable:
        return CarriedOn(retry_at=now + RETRY_SECONDS)
    except (ProviderRefused, TokenRefused):
        return Ended()
    if not allows(judgement.roles, "read"):
        return Ended()
    return Rejudged(roles=judgement.roles, fresh_until=judgement.fresh_until,
                    secrets=SessionSecrets(
                        refresh_token=tokens.refresh_token or secrets_kept.refresh_token,
                        id_token=tokens.id_token or secrets_kept.id_token))


# ---- the sign-in transaction -----------------------------------------------------------------

TRANSACTION_SECONDS: Final = 600.0
MAX_RETURN_CHARS: Final = 512
# A console hash route: "#/" then unreserved and path characters only, so it is safe in a
# Location header and can never name another site.
_RETURN: Final = re.compile(r"#/[A-Za-z0-9\-._~/?&=%:+,@!$'()*;]*", re.ASCII)


def return_route(value: str | None) -> str:
    """The console hash route to land on after sign-in; "" (Home) for anything that is not one."""
    if value is None or len(value) > MAX_RETURN_CHARS or _RETURN.fullmatch(value) is None:
        return ""
    return value


def code_challenge(code_verifier: str) -> str:
    """PKCE's S256 challenge of a verifier (RFC 7636 §4.2)."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_transaction(origin: str, return_to: str | None, now: float) -> Transaction:
    """One sign-in's state, nonce and PKCE verifier, each 256 random bits, for ten minutes."""
    return Transaction(state=secrets.token_urlsafe(32), nonce=secrets.token_urlsafe(32),
                       code_verifier=secrets.token_urlsafe(32), origin=origin,
                       return_to=return_route(return_to), expires_at=now + TRANSACTION_SECONDS)


def transaction_stands(transaction: Transaction, state: str | None, now: float) -> bool:
    """The redirect back answers this transaction: its exact `state`, within its ten minutes."""
    return (isinstance(state, str) and now < transaction.expires_at
            and hmac.compare_digest(state.encode("utf-8", "replace"), transaction.state.encode()))


def redirect_address(origin: str, callback_path: str) -> str:
    """The redirect address the provider must have registered, exactly: the console's origin and
    Central's callback path."""
    return origin + callback_path


def session_id_of(cookie_value: str) -> str:
    """A session's key: the SHA-256 of its cookie's value. The row never holds the value."""
    return hashlib.sha256(cookie_value.encode("utf-8", "replace")).hexdigest()


def new_cookie_value() -> str:
    """A session cookie's value: 256 random bits."""
    return secrets.token_urlsafe(32)
