"""Owner-scoped boot session cache: exact bootstrap retry, own-clock expiry, re-enroll.

Central never states node time. A grant carries a duration; this owner samples
its own boot clock just before each enrollment POST and expires the grant at
that sample plus the duration, which is never later than Central's own expiry.
A 401/403 from Central means the session is no longer honoured (expired,
superseded or revoked): the next ensure() enrolls a fresh session claim.
"""
from __future__ import annotations

import secrets
from dataclasses import replace
from uuid import UUID, uuid4

from appliance.central_session.http import NodeHTTP
from appliance.kernel.boot_store import BootStore
from appliance.kernel.clock import boottime_ms
from contracts.node_commands import (
    NodeSessionClaim,
    NodeSessionGrant,
    encode_session_claim,
    encode_session_grant,
    parse_session_claim,
    parse_session_grant,
)

REFUSED = (401, 403)


class NodeSession:
    def __init__(self, store: BootStore, transport: NodeHTTP, *, owner: str,
                 serial: str, offer_id: UUID, kernel_boot_id: UUID,
                 incarnation_id: UUID | None = None):
        self.store, self.transport = store, transport
        row = store.read("session")
        self.claim = (parse_session_claim(row["claim"].encode()) if row else
                      NodeSessionClaim(serial, offer_id, kernel_boot_id, owner, incarnation_id or uuid4(),
                                       uuid4(), secrets.token_hex(32)))
        if self.claim.owner != owner or self.claim.serial != serial or self.claim.offer_id != offer_id or self.claim.kernel_boot_id != kernel_boot_id:
            raise ValueError("node_session_binding")
        if incarnation_id is not None and self.claim.incarnation_id != incarnation_id:
            raise ValueError("node_session_incarnation_mismatch")
        self.grant: NodeSessionGrant | None = parse_session_grant(row["grant"].encode()) if row and row.get("grant") else None
        if self.grant is not None:
            self._validate_grant(self.grant)
        self.expires_ms: int | None = row.get("expires_ms") if row else None
        self.refused: bool = bool(row.get("refused")) if row else False
        if row is None:
            self._save()

    def _validate_grant(self, grant: NodeSessionGrant) -> None:
        if (grant.session_id != self.claim.session_id or grant.offer_id != self.claim.offer_id
                or grant.producer.kernel_boot_id != self.claim.kernel_boot_id
                or grant.producer.incarnation_id != self.claim.incarnation_id
                or grant.producer.owner != self.claim.owner):
            raise ValueError("node_session_grant_mismatch")

    def _save(self) -> None:
        self.store.write("session", {"claim": encode_session_claim(self.claim).decode(),
                                     "grant": encode_session_grant(self.grant).decode() if self.grant else None,
                                     "expires_ms": self.expires_ms, "refused": self.refused})

    def ensure(self) -> NodeSessionGrant | None:
        if self.grant is not None and self.expires_ms is not None and boottime_ms() < self.expires_ms:
            return self.grant
        if self.grant is not None or self.refused:
            # Never replay a claim Central has expired or refused: enroll a new session.
            self.claim = replace(self.claim, session_id=uuid4(), credential=secrets.token_hex(32))
            self.grant, self.expires_ms, self.refused = None, None, False
            self._save()
        sampled = boottime_ms()  # Taken just before the POST: the local expiry is conservative.
        status, raw = self.transport.request("POST", "/v2/node/sessions", encode_session_claim(self.claim))
        if status in REFUSED:
            self.refused = True
            self._save()
            return None
        if status != 200:
            return None
        grant = parse_session_grant(raw)
        self._validate_grant(grant)
        self.grant, self.expires_ms = grant, sampled + grant.valid_for_ms
        self._save()
        return grant

    def request(self, method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
        """One authenticated exchange; a refused session is re-enrolled by the next ensure()."""
        status, raw = self.transport.request(method, path, body, self.claim)
        if status in REFUSED and self.grant is not None:
            self.grant, self.expires_ms, self.refused = None, None, True
            self._save()
        return status, raw
