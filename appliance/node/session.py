"""Owner-scoped boot session cache with exact bootstrap retry and expiry CAS."""
from __future__ import annotations

import secrets
from dataclasses import replace
from uuid import UUID, uuid4

from appliance.node.clock import boottime_ms
from appliance.node.http import NodeHTTP
from appliance.node.storage import BootStore
from contracts.node_commands import (
    NodeSessionClaim,
    NodeSessionGrant,
    encode_session_claim,
    encode_session_grant,
    parse_session_claim,
    parse_session_grant,
)


class NodeSession:
    def __init__(self, store: BootStore, transport: NodeHTTP, *, owner: str,
                 serial: str, offer_id: UUID, kernel_boot_id: UUID,
                 incarnation_id: UUID | None = None):
        self.store, self.transport = store, transport
        row = store.read("session")
        self.claim = (parse_session_claim(row["claim"].encode()) if row else
                      NodeSessionClaim(serial, offer_id, kernel_boot_id, owner, incarnation_id or uuid4(),
                                       uuid4(), secrets.token_hex(32), boottime_ms()))
        if self.claim.owner != owner or self.claim.serial != serial or self.claim.offer_id != offer_id or self.claim.kernel_boot_id != kernel_boot_id:
            raise ValueError("node_session_binding")
        if incarnation_id is not None and self.claim.incarnation_id != incarnation_id:
            raise ValueError("node_session_incarnation_mismatch")
        self.grant: NodeSessionGrant | None = parse_session_grant(row["grant"].encode()) if row and row.get("grant") else None
        if self.grant is not None:
            self._validate_grant(self.grant)
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
                                     "grant": encode_session_grant(self.grant).decode() if self.grant else None})

    def ensure(self) -> NodeSessionGrant | None:
        now = boottime_ms()
        if self.grant is not None and now < self.grant.expires_boottime_ms:
            return self.grant
        if self.grant is not None:
            previous = self.claim
            self.claim = replace(previous, session_id=uuid4(), credential=secrets.token_hex(32),
                                 sampled_boottime_ms=now)
            self.grant = None
            self._save()
        status, raw = self.transport.request("POST", "/v2/node/sessions", encode_session_claim(self.claim))
        if status != 200:
            return None
        grant = parse_session_grant(raw)
        self._validate_grant(grant)
        if grant.expires_boottime_ms <= now:
            raise ValueError("node_session_grant_mismatch")
        self.grant = grant
        self._save()
        return grant
