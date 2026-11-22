"""A signed token's signature, checked with PyJWT: the one module that imports it (decision 0035;
import-linter contract "Only the signature check imports PyJWT"). No I/O: the keys are handed in.

It checks the signature and nothing else. Every claim (`iss`, `aud`, `exp`, `nbf`, `nonce`) is
judged on Central's clock by `central/operator_access/policy.py` `TokenJudge`, which also owns the
algorithm allow-list; this module refuses an algorithm outside it too, so a caller that forgot
cannot make it verify an HMAC or unsigned token.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import jwt

from central.operator_access.model import Claims, SignatureRefusal
from central.operator_access.policy import ALGORITHMS

Jwks = Mapping[str, object]
"""A JSON Web Key Set as the issuer serves it: `{"keys": [...]}`."""

# Only the signature: every time and audience rule is the policy's, on Central's clock.
_SIGNATURE_ONLY: Final = {
    "verify_signature": True, "verify_exp": False, "verify_nbf": False, "verify_iat": False,
    "verify_aud": False, "verify_iss": False, "verify_sub": False, "verify_jti": False,
    "require": [],
}


def _candidates(jwks: Jwks, key_id: str | None) -> list[Mapping[str, object]]:
    keys = jwks.get("keys")
    listed = [key for key in keys if isinstance(key, Mapping)] if isinstance(keys, list) else []
    signing = [key for key in listed if key.get("use", "sig") == "sig"]
    return signing if key_id is None else [key for key in signing if key.get("kid") == key_id]


def verified_claims(token: str, algorithm: str, key_id: str | None, jwks: Jwks) -> Claims | SignatureRefusal:
    """The token's claims when its signature verifies with exactly `algorithm` under the key
    `key_id` names in `jwks` (any signing key when the token names none). `UNKNOWN_KEY` when the
    set holds no such key, `SIGNATURE` otherwise. Total: never raises."""
    if algorithm not in ALGORITHMS:
        return SignatureRefusal.SIGNATURE
    candidates = _candidates(jwks, key_id)
    if not candidates:
        return SignatureRefusal.UNKNOWN_KEY
    for jwk in candidates:
        try:
            key = jwt.PyJWK(dict(jwk), algorithm=algorithm).key
            claims = jwt.decode(token, key, algorithms=[algorithm], options=_SIGNATURE_ONLY)
        except (jwt.PyJWTError, ValueError, TypeError):
            continue
        if isinstance(claims, dict):
            return claims
    return SignatureRefusal.SIGNATURE
