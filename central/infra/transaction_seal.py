"""The sign-in transaction's cookie value, sealed (decision 0035): AES-256-GCM under a key
derived from the client secret with HKDF-SHA256, so the browser can neither read the PKCE
verifier and nonce nor change the origin and return route. No I/O. A new client secret makes
every sealed value unreadable, which only ends sign-ins under way.

`open` is total: the transaction, or None for anything this seal did not make. Whether the
transaction still stands (its state, its ten minutes) is `policy.transaction_stands`.
"""
from __future__ import annotations

import base64
import binascii
import json
import secrets
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from central.operator_access.model import Transaction

_SALT: Final = b"photo-wall/sign-in-transaction/v1"
_INFO: Final = b"transaction cookie"
_NONCE_BYTES: Final = 12
MAX_VALUE_CHARS: Final = 2048
_TEXT_FIELDS: Final = ("state", "nonce", "code_verifier", "origin", "return_to")


class AesGcmTransactionSeal:
    def __init__(self, client_secret: str) -> None:
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=_SALT, info=_INFO).derive(
            client_secret.encode("utf-8"))
        self._aead = AESGCM(key)

    def seal(self, transaction: Transaction) -> str:
        plain = json.dumps({**{name: getattr(transaction, name) for name in _TEXT_FIELDS},
                            "expires_at": transaction.expires_at}, separators=(",", ":")).encode()
        nonce = secrets.token_bytes(_NONCE_BYTES)
        sealed = nonce + self._aead.encrypt(nonce, plain, _INFO)
        return base64.urlsafe_b64encode(sealed).rstrip(b"=").decode("ascii")

    def open(self, value: object) -> Transaction | None:
        if not isinstance(value, str) or not value or len(value) > MAX_VALUE_CHARS:
            return None
        try:
            sealed = base64.urlsafe_b64decode(value.encode("ascii") + b"=" * (-len(value) % 4))
            fields = json.loads(self._aead.decrypt(sealed[:_NONCE_BYTES], sealed[_NONCE_BYTES:], _INFO))
        except (InvalidTag, ValueError, binascii.Error, UnicodeError):
            return None
        if (not isinstance(fields, dict) or any(not isinstance(fields.get(name), str) for name in _TEXT_FIELDS)
                or isinstance(fields.get("expires_at"), bool)
                or not isinstance(fields.get("expires_at"), (int, float))):
            return None
        return Transaction(**{name: fields[name] for name in _TEXT_FIELDS},
                           expires_at=float(fields["expires_at"]))
