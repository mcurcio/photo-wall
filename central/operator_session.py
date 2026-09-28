"""The operator sign-in: the admin-token compare and the signed session value (pass A §4-§5).

Stdlib only. The session value never holds the token: it holds an expiry, a nonce and the Origin
that signed in, under an HMAC keyed by scrypt(admin token). Rotating the token changes the key, so
every value ever minted stops verifying; a captured value costs a full scrypt per offline guess
at the token.

`SessionCodec.verify` is TOTAL: it returns the session or None and never raises, whatever the
input. The MAC is compared before anything in the value is parsed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from functools import lru_cache

from contracts.time import Clock

SESSION_SECONDS = 30 * 24 * 3600
MAX_VALUE_BYTES = 512

# The KDF (pass A §3). Constants of production code: tests stay fast through the per-process key
# cache below, never by lowering these.
_SALT = b"photo-wall/operator-session/v1"
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024

_B64 = "[A-Za-z0-9_-]"
# ASCII only, explicit classes (no \d, so Unicode digits fail); origin is 1-344 base64url chars.
_VALUE = re.compile(
    rf"v1\.[0-9]{{10}}\.{_B64}{{22}}\.{_B64}{{1,344}}\.{_B64}{{43}}", re.ASCII,
)
# The Origins a sign-in may bind: `http(s)://host[:port]`, ASCII, nothing after the authority.
_ORIGIN = re.compile(r"(https?)://[A-Za-z0-9._~\-\[\]:]+", re.ASCII)
_MAX_ORIGIN = 258  # 344 base64url characters


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@lru_cache(maxsize=16)
def session_key(token_bytes: bytes) -> bytes:
    """The 32-byte session key, derived once per process per distinct token (about 60 ms)."""
    return hashlib.scrypt(
        token_bytes, salt=_SALT, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM, dklen=32,
    )


def origin_scheme(origin: str | None) -> str | None:
    """"https" or "http" for an Origin a sign-in may bind; None for any other value.

    Bindable means `http(s)://authority` in ASCII and short enough that its base64url fits the
    value's 344-character origin part (258 bytes). `null`, other schemes and paths are not.
    """
    if origin is None or len(origin) > _MAX_ORIGIN:
        return None
    match = _ORIGIN.fullmatch(origin)
    return match.group(1) if match else None


@dataclass(frozen=True)
class OperatorSession:
    origin: str
    expires_at: int


class SessionCodec:
    """Mints and verifies session values under one admin token."""

    def __init__(self, admin_token: str, clock: Clock):
        # Strict UTF-8: a token that cannot be encoded fails at construction, not per request.
        self._token = admin_token.encode("utf-8")
        self._key = session_key(self._token)
        self._clock = clock

    def token_matches(self, candidate: bytes) -> bool:
        """The ONE token compare (bearer and sign-in): constant-time over bytes, never raises."""
        return secrets.compare_digest(candidate, self._token)

    def _mac(self, prefix: bytes) -> str:
        return _b64(hmac.new(self._key, prefix, hashlib.sha256).digest())

    def mint(self, origin: str) -> str | None:
        """A value binding `origin` for SESSION_SECONDS; None when the Origin cannot be bound."""
        if origin_scheme(origin) is None:
            return None
        encoded = _b64(origin.encode("ascii"))
        expires_at = int(self._clock.utc()) + SESSION_SECONDS
        prefix = f"v1.{expires_at:010d}.{_b64(secrets.token_bytes(16))}.{encoded}"
        return f"{prefix}.{self._mac(prefix.encode('ascii'))}"

    def verify(self, value: object) -> OperatorSession | None:
        # Total by construction, not by a catch-all: step 1 admits only ASCII of the one shape,
        # and step 3 parses only a value this key minted, so no step below can raise.
        # 1. One ASCII-only shape check over the whole value (bounded length first).
        if not isinstance(value, str) or len(value) > MAX_VALUE_BYTES:
            return None
        if _VALUE.fullmatch(value) is None:
            return None
        # 2. The MAC over the raw prefix bytes, in constant time, before anything is parsed.
        prefix, _, mac = value.rpartition(".")
        expected = self._mac(prefix.encode("ascii"))
        if not hmac.compare_digest(mac.encode("ascii"), expected.encode("ascii")):
            return None
        # 3. Parse.
        _, expires, _, origin = prefix.split(".")
        expires_at = int(expires)
        bound = _unb64(origin).decode("ascii")
        # 4. Expiry, with a cap on how far in the future it may be.
        now = self._clock.utc()
        if not now < expires_at <= now + SESSION_SECONDS:
            return None
        return OperatorSession(origin=bound, expires_at=expires_at)
