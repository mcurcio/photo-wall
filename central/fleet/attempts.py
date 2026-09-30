"""Immutable same-boot app attempts and exact artifact locator roots.

This owner only queues a selected attempt. A separately authenticated T1/T2
principal is required even for that inert stage. There is no command, stop,
drain, acceptance or automatic fallback promotion path here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID, uuid4

from central.db import Database
from central.fleet.locks import lock_fleet_assets_in
from central.fleet.models import FleetError, OfferAsset
from central.fleet.policy import effective_app
from central.fleet.principal import VerifiedOsPrincipal, require_current_principal_in
from central.fleet.service import FleetService
from contracts.player_payload import FORMAT as PAYLOAD_FORMAT
from contracts.player_payload import MAX_ARCHIVE_BYTES
from contracts.time import Clock

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ABI = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SOURCE_MANIFEST = "manifest.v2.json"


@dataclass(frozen=True, slots=True)
class AttemptSnapshot:
    attempt_id: UUID
    device_id: str
    device_generation: int
    command_session_id: UUID
    offer_id: UUID
    kernel_boot_id: UUID
    installation_audience: str
    desired_revision: int
    policy_source: str
    base_sha256: str
    base_abi: str
    target_tag: str
    target_sha256: str
    target_size: int
    fallback_sha256: str
    fallback_size: int
    fallback_evidence_ref: str
    phase: str
    root_released_at: float | None

    @classmethod
    def from_row(cls, row: dict) -> AttemptSnapshot:
        return cls(
            attempt_id=row["attempt_id"], device_id=row["device_id"],
            device_generation=row["device_generation"], offer_id=row["offer_id"],
            command_session_id=row["command_session_id"],
            kernel_boot_id=row["kernel_boot_id"],
            installation_audience=row["installation_audience"],
            desired_revision=row["desired_revision"], policy_source=row["policy_source"],
            base_sha256=row["base_sha256"], base_abi=row["base_abi"],
            target_tag=row["target_tag"], target_sha256=row["target_sha256"],
            target_size=row["target_size"], fallback_sha256=row["fallback_sha256"],
            fallback_size=row["fallback_size"],
            fallback_evidence_ref=row["fallback_evidence_ref"],
            phase=row["phase"], root_released_at=row["root_released_at"],
        )

    @property
    def assets(self) -> tuple[OfferAsset, OfferAsset]:
        """Exact expected bytes for an explicit, separate pod-local preflight."""
        return (
            OfferAsset("app", self.target_tag, self.target_sha256,
                       self.target_sha256, self.target_size, PAYLOAD_FORMAT),
            OfferAsset("app", "accepted-fallback", self.fallback_sha256,
                       self.fallback_sha256, self.fallback_size, PAYLOAD_FORMAT),
        )


def _valid_digest(value: str) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _exact_locator(row: dict | None, *, sha256: str, size: int) -> bool:
    if row is None:
        return False
    url = row["locator_url"]
    return (isinstance(url, str) and url.startswith(("https://", "http://"))
            and len(url) <= 2048 and row["locator_sha256"] == sha256
            and row["locator_size"] == size and row["expected_sha256"] == sha256
            and row["expected_size"] == size)


class AttemptService:
    """Queue one frozen target/fallback pair; never infer command authority."""

    def __init__(self, db: Database, clock: Clock) -> None:
        self.db, self.clock = db, clock

    @staticmethod
    def _root_owner(attempt_id: UUID) -> str:
        return f"fleet-attempt:{attempt_id}"

    @staticmethod
    def _same_retry(row: dict, principal: VerifiedOsPrincipal, *,
                    target_sha256: str, fallback_sha256: str) -> bool:
        return (row["attempt_schema"] == 1 and row["device_generation"] ==
                principal.device_generation and row["kernel_boot_id"] ==
                principal.kernel_boot_id and row["installation_audience"] ==
                principal.installation_audience and row["command_session_id"] ==
                principal.command_session_id and row["target_sha256"] == target_sha256
                and row["fallback_sha256"] == fallback_sha256)

    @staticmethod
    def _rooted_in(conn, row: dict) -> bool:
        owner = AttemptService._root_owner(row["attempt_id"])
        refs = conn.execute(
            "SELECT identity,locator_url,locator_sha256,locator_size,"
            "expected_sha256,expected_size FROM asset_references "
            "WHERE kind='player-payload' AND owner=%s", (owner,),
        ).fetchall()
        by_digest = {ref["identity"]: ref for ref in refs}
        return (len(refs) == 2 and
                _exact_locator(by_digest.get(row["target_sha256"]),
                               sha256=row["target_sha256"], size=row["target_size"]) and
                _exact_locator(by_digest.get(row["fallback_sha256"]),
                               sha256=row["fallback_sha256"], size=row["fallback_size"]))

    def create_queued(self, principal: VerifiedOsPrincipal, *, desired_revision: int,
                      expected_target_sha256: str,
                      fallback_sha256: str) -> AttemptSnapshot:
        """Freeze policy, base ABI and two exact locators in one transaction.

        A duplicate key returns the same immutable attempt while its roots still
        exist, even if a release tag has since been recut. Different expectations
        under the same revision conflict. This does not fetch bytes or stop a Player.
        """
        with self.db.transaction() as conn:
            return self.create_queued_in(
                conn, principal, desired_revision=desired_revision,
                expected_target_sha256=expected_target_sha256,
                fallback_sha256=fallback_sha256,
            )

    def create_queued_in(self, conn, principal: VerifiedOsPrincipal, *,
                         desired_revision: int, expected_target_sha256: str,
                         fallback_sha256: str) -> AttemptSnapshot:
        """Freeze the attempt and both roots in the caller-owned transaction.

        The current T1/T2 principal guard acquires fleet, device, lifecycle
        and session locks. A caller that also mutates equipment must acquire
        Coordination and Runtime before entering this method.
        """
        if (type(desired_revision) is not int or desired_revision < 1
                or not _valid_digest(expected_target_sha256)
                or not _valid_digest(fallback_sha256)
                or expected_target_sha256 == fallback_sha256):
            raise FleetError("attempt_request_invalid", 422)
        admission = require_current_principal_in(conn, principal, clock=self.clock)
        existing = conn.execute(
            "SELECT * FROM fleet_app_attempts WHERE device_id=%s AND offer_id=%s "
            "AND desired_revision=%s FOR UPDATE",
            (principal.device_id, principal.offer_id, desired_revision),
        ).fetchone()
        fleet, override = FleetService._app_policy(conn, principal.device_id)
        selected = effective_app(fleet, override)
        target = selected["target"]
        if existing is not None:
            if not self._same_retry(existing, principal,
                                    target_sha256=expected_target_sha256,
                                    fallback_sha256=fallback_sha256):
                raise FleetError("attempt_retry_conflict")
            if existing["revoked_at"] is not None or existing["root_released_at"] is not None:
                raise FleetError("attempt_not_active")
            if (selected["source"] != existing["policy_source"]
                    or selected["revision"] != existing["desired_revision"]
                    or target is None or target["tag"] != existing["target_tag"]
                    or target["sha256"] != existing["target_sha256"]
                    or target["size"] != existing["target_size"]
                    or target["format"] != existing["target_format"]):
                raise FleetError("attempt_policy_changed")
            if not self._rooted_in(conn, existing):
                raise FleetError("attempt_root_unavailable", 503)
            admission.ensure_current(self.clock)
            return AttemptSnapshot.from_row(existing)

        offer = conn.execute("SELECT * FROM fleet_boot_offers WHERE offer_id=%s FOR SHARE",
                             (principal.offer_id,)).fetchone()
        if (offer is None or offer["offer_schema"] != 2
                or offer["installation_audience"] != principal.installation_audience
                or offer["device_id"] != principal.device_id
                or offer["kernel_boot_id"] != principal.kernel_boot_id):
            raise FleetError("attempt_offer_unavailable", 409)
        base = conn.execute(
            "SELECT base_abi,base_abi_squashfs_sha256,base_abi_source_manifest "
            "FROM app_releases WHERE tag=%s FOR SHARE", (offer["base_tag"],),
        ).fetchone()
        if (base is None or base["base_abi_squashfs_sha256"] != offer["base_sha256"]
                or not isinstance(base["base_abi"], str)
                or _ABI.fullmatch(base["base_abi"]) is None
                or base["base_abi_source_manifest"] != _SOURCE_MANIFEST):
            raise FleetError("attempt_base_abi_unavailable", 409)
        if (selected["source"] not in ("explicit", "override")
                or selected["revision"] != desired_revision or target is None
                or target["format"] != PAYLOAD_FORMAT
                or target["sha256"] != expected_target_sha256
                or not 0 < target["size"] <= MAX_ARCHIVE_BYTES):
            raise FleetError("attempt_policy_changed")
        release = conn.execute(
            "SELECT payload_url,payload_sha256,payload_size,payload_format,"
            "payload_base_abi,payload_source_manifest,mirror_state "
            "FROM app_releases WHERE tag=%s FOR SHARE", (target["tag"],),
        ).fetchone()
        if (release is None or release["payload_sha256"] != target["sha256"]
                or release["payload_size"] != target["size"]
                or release["payload_format"] != PAYLOAD_FORMAT
                or release["payload_base_abi"] != base["base_abi"]
                or release["payload_source_manifest"] != _SOURCE_MANIFEST
                or release["mirror_state"] in
                ("divergent", "withdrawn", "undeployable")
                or not _exact_locator({
                    "locator_url": release["payload_url"],
                    "locator_sha256": release["payload_sha256"],
                    "locator_size": release["payload_size"],
                    "expected_sha256": release["payload_sha256"],
                    "expected_size": release["payload_size"],
                }, sha256=target["sha256"], size=target["size"])):
            raise FleetError("attempt_target_unavailable", 503)
        accepted = conn.execute(
            "SELECT content_key,sha256,size,base_abi,trust_mode,evidence_ref,"
            "fallback_owner FROM fleet_generation_acceptances "
            "WHERE device_id=%s AND device_generation=%s AND kind='app' "
            "AND sha256=%s FOR SHARE",
            (principal.device_id, principal.device_generation, fallback_sha256),
        ).fetchone()
        fallback = conn.execute(
            "SELECT locator_url,locator_sha256,locator_size,expected_sha256,"
            "expected_size FROM asset_references WHERE kind='player-payload' "
            "AND identity=%s AND owner=%s FOR SHARE",
            (fallback_sha256, accepted["fallback_owner"] if accepted else ""),
        ).fetchone()
        if (accepted is None or accepted["content_key"] != fallback_sha256
                or accepted["base_abi"] != base["base_abi"]
                or accepted["trust_mode"] not in ("t1", "t2")
                or not isinstance(accepted["evidence_ref"], str)
                or not 1 <= len(accepted["evidence_ref"]) <= 256
                or not 0 < accepted["size"] <= MAX_ARCHIVE_BYTES
                or fallback is None or not _exact_locator(
                    fallback, sha256=fallback_sha256, size=accepted["size"])):
            raise FleetError("attempt_fallback_unavailable", 503)

        now = admission.ensure_current(self.clock)
        attempt_id = uuid4()
        row = conn.execute(
            "INSERT INTO fleet_app_attempts(attempt_id,device_id,offer_id,"
            "desired_revision,target_sha256,fallback_sha256,phase,created_at,updated_at,"
            "device_generation,command_session_id,attempt_schema,"
            "installation_audience,kernel_boot_id,"
            "policy_source,base_sha256,base_abi,base_abi_source_manifest,target_tag,"
            "target_size,target_format,target_base_abi,target_source_manifest,"
            "fallback_size,fallback_base_abi,fallback_trust_mode,fallback_evidence_ref) "
            "VALUES(%s,%s,%s,%s,%s,%s,'queued',%s,%s,%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "%s,%s,%s,%s,%s) RETURNING *",
            (attempt_id, principal.device_id, principal.offer_id, desired_revision,
             target["sha256"], fallback_sha256, now, now, principal.device_generation,
             principal.command_session_id,
             principal.installation_audience, principal.kernel_boot_id,
             selected["source"], offer["base_sha256"], base["base_abi"],
             _SOURCE_MANIFEST, target["tag"], target["size"], PAYLOAD_FORMAT,
             base["base_abi"], _SOURCE_MANIFEST, accepted["size"],
             base["base_abi"], accepted["trust_mode"], accepted["evidence_ref"]),
        ).fetchone()
        owner = self._root_owner(attempt_id)
        for sha256, size, url in (
            (target["sha256"], target["size"], release["payload_url"]),
            (fallback_sha256, accepted["size"], fallback["locator_url"]),
        ):
            conn.execute("INSERT INTO assets(kind,identity,created_at) "
                         "VALUES('player-payload',%s,%s) "
                         "ON CONFLICT(kind,identity) DO NOTHING", (sha256, now))
            conn.execute(
                "INSERT INTO asset_references(kind,identity,owner,locator_url,"
                "locator_sha256,locator_size,expected_sha256,expected_size,added_at) "
                "VALUES('player-payload',%s,%s,%s,%s,%s,%s,%s,%s)",
                (sha256, owner, url, sha256, size, sha256, size, now),
            )
        admission.ensure_current(self.clock)
        return AttemptSnapshot.from_row(row)

    def release_queued(self, attempt_id: UUID) -> None:
        """Release a revoked, never-commanded queued attempt's exact cache roots.

        This internal lifecycle operation is safe to retry after retirement. It
        does not revoke an attempt and cannot release an active queued attempt.
        """
        if not isinstance(attempt_id, UUID):
            raise FleetError("attempt_id_invalid", 422)
        with self.db.transaction() as conn:
            lock_fleet_assets_in(conn)
            row = conn.execute("SELECT device_id FROM fleet_app_attempts WHERE attempt_id=%s",
                               (attempt_id,)).fetchone()
            if row is None:
                raise FleetError("attempt_not_found", 404)
            conn.execute("SELECT 1 FROM devices WHERE device_id=%s FOR UPDATE",
                         (row["device_id"],)).fetchone()
            conn.execute("SELECT 1 FROM fleet_device_lifecycle WHERE device_id=%s FOR UPDATE",
                         (row["device_id"],)).fetchone()
            release_queued_in(conn, attempt_id, now=self.clock.utc())


def release_queued_in(conn, attempt_id: UUID, *, now: float) -> None:
    """Caller holds fleet offer, device and lifecycle locks in that order."""
    row = conn.execute("SELECT * FROM fleet_app_attempts WHERE attempt_id=%s FOR UPDATE",
                       (attempt_id,)).fetchone()
    if row is None:
        raise FleetError("attempt_not_found", 404)
    if row["attempt_schema"] != 1 or row["phase"] != "queued" or row["command_id"] is not None \
            or row["drain_id"] is not None or row["revoked_at"] is None:
        raise FleetError("attempt_release_requires_revocation")
    owner = AttemptService._root_owner(attempt_id)
    if row["root_released_at"] is None:
        if not AttemptService._rooted_in(conn, row):
            raise FleetError("attempt_root_unavailable", 503)
        conn.execute("UPDATE fleet_app_attempts SET root_released_at=%s,updated_at=%s "
                     "WHERE attempt_id=%s", (now, now, attempt_id))
    conn.execute("DELETE FROM asset_references WHERE kind='player-payload' AND owner=%s "
                 "AND identity IN (%s,%s)",
                 (owner, row["target_sha256"], row["fallback_sha256"]))


def release_queued_for_device_in(conn, device_id: str, *, now: float) -> None:
    """Complete queued-root retirement under Registry's existing lock chain."""
    rows = conn.execute(
        "SELECT attempt_id FROM fleet_app_attempts WHERE device_id=%s AND attempt_schema=1 "
        "AND phase='queued' AND command_id IS NULL AND drain_id IS NULL "
        "AND root_released_at IS NULL ORDER BY attempt_id FOR UPDATE",
        (device_id,),
    ).fetchall()
    for row in rows:
        release_queued_in(conn, row["attempt_id"], now=now)
