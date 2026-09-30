"""Exact accepted-app retention for a future managed update transaction.

An accepted row is evidence recorded by a separately trusted acceptance owner. This module
cannot create that evidence or issue an OS command. It preserves the offer's immutable locator
and checks exact bytes on the calling pod before an attempt may use the fallback.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from central.db import Database
from central.fleet.bytes import OfferByteReader
from central.fleet.locks import lock_fleet_assets_in
from central.fleet.models import T0_AUDIENCE, FleetError, OfferAsset
from contracts.player_payload import FORMAT as PAYLOAD_FORMAT
from contracts.time import Clock


def _fallback_owner(device_id: str) -> str:
    return f"fleet-fallback:{device_id}"


def retire_device_fallback_references(conn, device_id: str) -> None:
    """Forget locators on retirement, retaining accepted evidence as history.

    The caller holds the fleet offer lock in its device-retirement transaction. This shared
    lock serializes retirement with reservations and cache eviction.
    """
    conn.execute("DELETE FROM asset_references WHERE kind='player-payload' AND owner=%s",
                 (_fallback_owner(device_id),))


@dataclass(frozen=True, slots=True)
class ReservedFallback:
    device_id: str
    sha256: str
    size: int
    base_abi: str
    trust_mode: str
    evidence_ref: str

    @property
    def asset(self) -> OfferAsset:
        # The tag is only an internal display alias; byte selection uses the digest.
        return OfferAsset("app", "accepted-fallback", self.sha256, self.sha256,
                          self.size, PAYLOAD_FORMAT)


class AcceptedFallbackService:
    def __init__(self, db: Database, clock: Clock, *, audience: str = T0_AUDIENCE) -> None:
        self.db, self.clock, self.audience = db, clock, audience

    def reserve_app(self, *, device_id: str, sha256: str, base_abi: str) -> ReservedFallback:
        """Root one explicitly accepted exact payload and its frozen offer locator.

        This is a durable cache obligation, not evidence that any serving pod currently has
        the bytes. A missing accepted record, source offer, or matching ABI is a refusal.
        """
        now = self.clock.utc()
        with self.db.transaction() as conn:
            lock_fleet_assets_in(conn)
            device = conn.execute("SELECT retired_at FROM devices WHERE device_id=%s "
                                  "FOR UPDATE", (device_id,)).fetchone()
            lifecycle = conn.execute("SELECT revoked_at FROM fleet_device_lifecycle "
                                     "WHERE device_id=%s FOR UPDATE",
                                     (device_id,)).fetchone()
            if (device is None or device["retired_at"] is not None
                    or lifecycle is None or lifecycle["revoked_at"] is not None):
                raise FleetError("fallback_device_unavailable", 409)
            accepted = conn.execute(
                "SELECT content_key,sha256,size,base_abi,trust_mode,evidence_ref "
                "FROM fleet_accepted_artifacts WHERE device_id=%s AND kind='app' "
                "AND sha256=%s FOR SHARE", (device_id, sha256),
            ).fetchone()
            if (accepted is None or accepted["content_key"] != sha256
                    or accepted["base_abi"] != base_abi
                    or accepted["trust_mode"] not in ("t1", "t2")
                    or not accepted["evidence_ref"]):
                raise FleetError("fallback_not_qualified", 409)
            owner = _fallback_owner(device_id)
            if len(owner) > 128:
                raise FleetError("fallback_device_unavailable", 409)
            # The original offer's locator is immutable even if a release tag is later recut.
            # An existing reservation remains usable after its source offer has expired.
            source = conn.execute(
                "SELECT locator_url,locator_sha256,locator_size,expected_sha256,"
                "expected_size FROM asset_references WHERE kind='player-payload' "
                "AND identity=%s AND owner=%s", (sha256, owner),
            ).fetchone()
            if source is None:
                source = conn.execute(
                    "SELECT ref.locator_url,ref.locator_sha256,ref.locator_size,"
                    "ref.expected_sha256,ref.expected_size FROM fleet_boot_offers AS offer "
                    "JOIN asset_references AS ref ON ref.kind='player-payload' "
                    "AND ref.identity=offer.app_sha256 "
                    "AND ref.owner='fleet-offer:' || offer.offer_id::text "
                    "WHERE offer.installation_audience=%s AND offer.device_id=%s "
                    "AND offer.offer_schema=2 AND offer.app_format=%s "
                    "AND offer.app_sha256=%s AND offer.app_size=%s "
                    "AND offer.app_base_abi=%s AND offer.expires_at>%s "
                    "ORDER BY offer.created_at DESC,offer.offer_id DESC LIMIT 1",
                    (self.audience, device_id, PAYLOAD_FORMAT, sha256, accepted["size"],
                     base_abi, now),
                ).fetchone()
                if source is None:
                    raise FleetError("fallback_locator_unavailable", 503)
                self._validate_source(source, sha256, accepted["size"])
                conn.execute(
                    "INSERT INTO asset_references(kind,identity,owner,locator_url,"
                    "locator_sha256,locator_size,expected_sha256,expected_size,added_at) "
                    "VALUES('player-payload',%s,%s,%s,%s,%s,%s,%s,%s)",
                    (sha256, owner, source["locator_url"], source["locator_sha256"],
                     source["locator_size"], source["expected_sha256"],
                     source["expected_size"], now),
                )
            else:
                self._validate_source(source, sha256, accepted["size"])
            # One device retains at most one accepted fallback locator. An unfinished
            # attempt may still need an older digest. It can release that fallback's
            # device root only if it already owns an identical frozen reference. No
            # production attempt-owned reference writer exists yet.
            needed = conn.execute(
                "SELECT 1 FROM fleet_artifact_retention_attempts AS attempt "
                "JOIN asset_references AS old ON old.kind='player-payload' "
                "AND old.owner=%s AND old.identity<>%s "
                "AND (attempt.target_sha256=old.identity "
                "OR attempt.fallback_sha256=old.identity) "
                "WHERE attempt.device_id=%s "
                "AND NOT EXISTS (SELECT 1 FROM asset_references AS rooted "
                "WHERE rooted.kind='player-payload' AND rooted.identity=old.identity "
                "AND rooted.owner='fleet-attempt:' || attempt.attempt_id::text "
                "AND rooted.locator_url=old.locator_url "
                "AND rooted.locator_sha256=old.locator_sha256 "
                "AND rooted.locator_size=old.locator_size "
                "AND rooted.expected_sha256=old.expected_sha256 "
                "AND rooted.expected_size=old.expected_size) "
                "LIMIT 1", (owner, sha256, device_id),
            ).fetchone()
            if needed is not None:
                raise FleetError("fallback_rotation_blocked_by_attempt", 409)
            conn.execute(
                "DELETE FROM asset_references WHERE kind='player-payload' AND owner=%s "
                "AND identity<>%s", (owner, sha256),
            )
        return ReservedFallback(device_id, sha256, accepted["size"], base_abi,
                                accepted["trust_mode"], accepted["evidence_ref"])

    @staticmethod
    def _validate_source(source: dict, sha256: str, size: int) -> None:
        if (source["locator_sha256"] != sha256 or source["locator_size"] != size
                or source["expected_sha256"] != sha256
                or source["expected_size"] != size):
            raise FleetError("fallback_locator_mismatch", 503)

    async def preflight_app(self, *, device_id: str, sha256: str, base_abi: str,
                            bytes_reader: OfferByteReader) -> ReservedFallback:
        """Return only after this pod opens and hashes the reserved exact payload."""
        fallback = await asyncio.to_thread(self.reserve_app, device_id=device_id,
                                           sha256=sha256, base_abi=base_abi)
        await bytes_reader.preflight((fallback.asset,))
        return fallback
