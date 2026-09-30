"""Central fleet state. T0 intake is observational and cannot issue commands.

The existing release catalog remains the source of discovered bytes. This module freezes exact
facts in an offer; it never treats a mutable release tag or an HTTP 200 as delivery/acceptance.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID, uuid4

from central.content_catalog.catalog import device_id_for_serial, sanitize_serial
from central.db import Database
from central.fleet.models import (
    Artifact,
    BaselineWrite,
    CheckIn,
    CheckInV2,
    FleetError,
    OfferRequest,
    PolicyWrite,
)
from central.fleet.policy import (
    app_control_status,
    app_observation_status,
    boot_claim_status,
    effective_app,
    fallback_classification,
)
from contracts.player_payload import FORMAT as PAYLOAD_FORMAT
from contracts.player_payload import MAX_ARCHIVE_BYTES
from contracts.release import MAX_ROOTFS_BYTES
from contracts.time import Clock

AUDIENCE = "photo-wall-central-t0"  # a label, deliberately not an authenticated audience
_OFFER_LOCK = 734118328
_OBS_LOCK_CLASS = 734118329
_DAY_SECONDS = 86400
_OBS_TTL_SECONDS = 30 * _DAY_SECONDS
_OFFER_DEVICE_DAILY = 128
_OFFER_GLOBAL_DAILY = 32768  # 128 Players x 128 offers, plus equivalent spoof headroom
_ASSET_DEVICE_DAILY = 512
_ASSET_GLOBAL_DAILY = 131072
_OBS_DEVICE_DAILY = 10000  # 10-second heartbeat with room for phase changes
_OBS_GLOBAL_DAILY = 2000000  # exceeds 128 x per-device budget with headroom
_NEW_CLAIMS_GLOBAL_DAILY = 1024  # bounds distinct fake serial rows, above 128-Player ceiling
OFFER_TTL_SECONDS = 30 * _DAY_SECONDS


class CommandAuthority(Protocol):
    """Future T1/T2 port. No T0 check-in or policy route calls this port."""

    def authorize(self, *, device_id: str, boot_id: UUID, attempt_id: UUID,
                  policy_revision: int, drain_id: UUID) -> UUID: ...


class DisabledCommandAuthority:
    def authorize(self, *, device_id: str, boot_id: UUID, attempt_id: UUID,
                  policy_revision: int, drain_id: UUID) -> UUID:
        raise FleetError("command_trust_unapproved", 409)


@dataclass(frozen=True, slots=True)
class OfferAsset:
    kind: str
    tag: str
    content_key: str
    sha256: str
    size: int
    format: str | None = None


def _artifact(row: dict[str, Any] | None, *, prefix: str = "target") -> dict | None:
    if (row is None or row[f"{prefix}_tag"] is None
            or row[f"{prefix}_sha256"] is None or row[f"{prefix}_size"] is None):
        return None
    return {"tag": row[f"{prefix}_tag"], "sha256": row[f"{prefix}_sha256"],
            "size": row[f"{prefix}_size"]}


def _offer_document(row: dict[str, Any]) -> dict[str, Any]:
    initial = _artifact(row, prefix="app")
    if initial is not None and row["offer_schema"] == 2:
        initial = {**initial, "format": row["app_format"],
                   "base_abi": row["app_base_abi"]}
    return {
        "schema": row["offer_schema"], "offer_id": str(row["offer_id"]),
        "base": {"tag": row["base_tag"], "sha256": row["base_sha256"],
                 "size": row["base_size"]},
        "initial_app": initial,
        "initial_app_status": row["app_status"],
        "compatibility_basis": row["compatibility_basis"],
        "base_policy_source": row["base_policy_source"],
        "base_policy_revision": row["base_policy_revision"],
        "app_policy_source": row["app_policy_source"],
        "app_policy_revision": row["app_policy_revision"],
        "expires_at": row["expires_at"],
    }


class FleetService:
    def __init__(self, db: Database, clock: Clock, *, audience: str = AUDIENCE) -> None:
        self.db, self.clock, self.audience = db, clock, audience

    @staticmethod
    def _revision(conn) -> int:
        return conn.execute("SELECT nextval('fleet_policy_revision_seq') AS n").fetchone()["n"]

    @staticmethod
    def _retain_offer_asset(conn, *, offer_id: UUID, kind: str, identity: str,
                            url: str, source_sha256: str, source_size: int | None,
                            expected_sha256: str | None, expected_size: int | None,
                            now: float) -> None:
        """Keep a frozen locator and produced facts readable after a release recut.

        The shared cache is still the byte source; an offer-owned asset reference
        prevents catalog sync from making the old exact digest invisible to the reader.
        """
        asset_kind = {"base": "os-image", "app": "player-payload"}[kind]
        conn.execute("INSERT INTO assets(kind,identity,created_at) VALUES(%s,%s,%s) "
                     "ON CONFLICT(kind,identity) DO NOTHING", (asset_kind, identity, now))
        conn.execute("INSERT INTO asset_references(kind,identity,owner,locator_url,"
                     "locator_sha256,locator_size,expected_sha256,expected_size,added_at) "
                     "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                     (asset_kind, identity, f"fleet-offer:{offer_id}", url,
                      source_sha256, source_size, expected_sha256, expected_size, now))

    @staticmethod
    def _retire_expired_offer_assets(conn, now: float) -> None:
        expired = conn.execute(
            "SELECT offer_id,kind,content_key FROM fleet_offer_artifact_roots "
            "WHERE retain_until<=%s ORDER BY retain_until,offer_id LIMIT 100 FOR UPDATE",
            (now,),
        ).fetchall()
        for root in expired:
            asset_kind = "os-image" if root["kind"] == "base" else "player-payload"
            conn.execute("DELETE FROM asset_references WHERE kind=%s AND identity=%s "
                         "AND owner=%s", (asset_kind, root["content_key"],
                                          f"fleet-offer:{root['offer_id']}"))
            conn.execute("DELETE FROM fleet_offer_artifact_roots "
                         "WHERE offer_id=%s AND kind=%s",
                         (root["offer_id"], root["kind"]))

    @staticmethod
    def _claim_quota(conn, *, device_id: str, kind: str, now: float) -> None:
        day = int(now // _DAY_SECONDS)
        limits = {
            "offer": (_OFFER_GLOBAL_DAILY, _OFFER_DEVICE_DAILY),
            "asset": (_ASSET_GLOBAL_DAILY, _ASSET_DEVICE_DAILY),
            "observation": (_OBS_GLOBAL_DAILY, _OBS_DEVICE_DAILY),
        }[kind]
        for scope, limit in zip(("global", device_id), limits, strict=True):
            row = conn.execute(
                "INSERT INTO fleet_t0_daily_quotas(scope,kind,day,used) "
                "VALUES(%s,%s,%s,1) ON CONFLICT(scope,kind,day) DO UPDATE SET "
                "used=fleet_t0_daily_quotas.used+1 WHERE fleet_t0_daily_quotas.used<%s "
                "RETURNING used", (scope, kind, day, limit),
            ).fetchone()
            if row is None:
                raise FleetError("t0_rate_limited", 429)
        # A bounded day index keeps fake-serial quota rows from accumulating forever.
        conn.execute("DELETE FROM fleet_t0_daily_quotas WHERE day<%s", (day - 30,))

    @staticmethod
    def _claim_new_device(conn, *, device_id: str, serial: str, now: float) -> None:
        if conn.execute("SELECT 1 FROM devices WHERE device_id=%s", (device_id,)).fetchone():
            return
        day = int(now // _DAY_SECONDS)
        row = conn.execute(
            "INSERT INTO fleet_t0_daily_quotas(scope,kind,day,used) "
            "VALUES('global','new_device',%s,1) ON CONFLICT(scope,kind,day) DO UPDATE SET "
            "used=fleet_t0_daily_quotas.used+1 "
            "WHERE fleet_t0_daily_quotas.used<%s RETURNING used",
            (day, _NEW_CLAIMS_GLOBAL_DAILY),
        ).fetchone()
        if row is None:
            raise FleetError("t0_claim_limit", 429)
        conn.execute("INSERT INTO devices(device_id,serial,first_seen,last_seen) "
                     "VALUES(%s,%s,%s,%s) ON CONFLICT(device_id) DO NOTHING",
                     (device_id, serial, now, now))

    @staticmethod
    def _validate_app(conn, target: Artifact) -> None:
        row = conn.execute(
            "SELECT payload_sha256,payload_size,payload_format,payload_base_abi,"
            "payload_source_manifest,mirror_state FROM app_releases WHERE tag=%s FOR SHARE",
            (target.tag,),
        ).fetchone()
        if row is None:
            raise FleetError("release_not_found", 404)
        if (row["payload_sha256"] != target.sha256 or row["payload_size"] != target.size
                or not 0 < target.size <= MAX_ARCHIVE_BYTES
                or row["payload_format"] != PAYLOAD_FORMAT
                or row["payload_source_manifest"] != "manifest.v2.json"
                or not row["payload_base_abi"]
                or row["mirror_state"] in ("divergent", "withdrawn", "undeployable")):
            raise FleetError("release_artifact_changed")

    def set_app_policy(self, request: PolicyWrite) -> dict:
        with self.db.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_OFFER_LOCK,))
            row = conn.execute("SELECT revision FROM fleet_app_policy WHERE singleton FOR UPDATE").fetchone()
            current = row["revision"] if row else 0
            if current != request.expected_revision:
                raise FleetError("policy_revision_conflict")
            if request.target is not None:
                self._validate_app(conn, request.target)
            revision = self._revision(conn)
            target = request.target
            conn.execute(
                "INSERT INTO fleet_app_policy(singleton,revision,target_tag,target_sha256,"
                "target_size,target_format,changed_at) VALUES(TRUE,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(singleton) "
                "DO UPDATE SET revision=EXCLUDED.revision,target_tag=EXCLUDED.target_tag,"
                "target_sha256=EXCLUDED.target_sha256,target_size=EXCLUDED.target_size,"
                "target_format=EXCLUDED.target_format,"
                "changed_at=EXCLUDED.changed_at",
                (revision, target.tag if target else None, target.sha256 if target else None,
                 target.size if target else None, PAYLOAD_FORMAT, self.clock.utc()),
            )
            return {"source": "explicit", "revision": revision,
                    "target": target.model_dump() if target else None, "status": "selected"}

    def set_override(self, device_id: str, *, expected_revision: int,
                     target: Artifact | None) -> dict:
        with self.db.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_OFFER_LOCK,))
            device = conn.execute("SELECT retired_at FROM devices WHERE device_id=%s FOR UPDATE",
                                  (device_id,)).fetchone()
            if device is None or device["retired_at"] is not None:
                raise FleetError("device_not_found", 404)
            row = conn.execute("SELECT revision FROM fleet_device_app_overrides "
                               "WHERE device_id=%s FOR UPDATE", (device_id,)).fetchone()
            current = row["revision"] if row else 0
            if current != expected_revision:
                raise FleetError("policy_revision_conflict")
            if target is not None:
                self._validate_app(conn, target)
            revision = self._revision(conn)
            conn.execute(
                "INSERT INTO fleet_device_app_overrides(device_id,revision,target_tag,"
                "target_sha256,target_size,target_format,changed_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(device_id) DO UPDATE SET revision=EXCLUDED.revision,"
                "target_tag=EXCLUDED.target_tag,target_sha256=EXCLUDED.target_sha256,"
                "target_size=EXCLUDED.target_size,target_format=EXCLUDED.target_format,"
                "changed_at=EXCLUDED.changed_at",
                (device_id, revision, target.tag if target else None,
                 target.sha256 if target else None, target.size if target else None,
                 PAYLOAD_FORMAT, self.clock.utc()),
            )
            capable_claim = conn.execute("SELECT 1 FROM fleet_boot_offers "
                                         "WHERE device_id=%s AND offer_schema=2 LIMIT 1",
                                         (device_id,)).fetchone()
            return {"source": "override", "revision": revision,
                    "target": target.model_dump() if target else None,
                    "status": ("queued_for_next_offer" if capable_claim else "queued_unenforceable")
                    if target else "selected"}

    def set_base_baseline(self, request: BaselineWrite) -> dict:
        """An operator selection, not an automatic frontier or acceptance event."""
        with self.db.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_OFFER_LOCK,))
            old = conn.execute("SELECT revision FROM fleet_base_policy WHERE singleton FOR UPDATE").fetchone()
            if (old["revision"] if old else 0) != request.expected_revision:
                raise FleetError("policy_revision_conflict")
            row = conn.execute("SELECT c.state,c.squashfs_sha256,c.size,r.base_abi,"
                               "r.base_abi_squashfs_sha256 "
                               "FROM app_releases r JOIN base_cache c ON c.tag=r.tag "
                               "WHERE r.tag=%s FOR SHARE", (request.tag,)).fetchone()
            if (row is None or row["state"] != "cached" or not row["squashfs_sha256"]
                    or not row["size"] or row["size"] > MAX_ROOTFS_BYTES
                    or not row["base_abi"] or
                    row["base_abi_squashfs_sha256"] != row["squashfs_sha256"]):
                raise FleetError("base_unavailable")
            revision = self._revision(conn)
            conn.execute("INSERT INTO fleet_base_policy(singleton,revision,tag,source,changed_at) "
                         "VALUES(TRUE,%s,%s,'operator',%s) ON CONFLICT(singleton) DO UPDATE SET "
                         "revision=EXCLUDED.revision,tag=EXCLUDED.tag,source=EXCLUDED.source,"
                         "changed_at=EXCLUDED.changed_at",
                         (revision, request.tag, self.clock.utc()))
            return {"revision": revision, "tag": request.tag, "source": "operator",
                    "status": "operator_baseline"}

    @staticmethod
    def _app_policy(conn, device_id: str) -> tuple[dict, dict | None]:
        def typed_artifact(row: dict | None, artifact_format: str) -> dict | None:
            artifact = _artifact(row)
            return {**artifact, "format": artifact_format} if artifact else None

        row = conn.execute("SELECT * FROM fleet_app_policy WHERE singleton").fetchone()
        if row is not None:
            fleet = {"source": "explicit", "revision": row["revision"],
                     "target": typed_artifact(row, row["target_format"])}
        else:
            legacy = conn.execute("SELECT r.tag AS target_tag,"
                                  "r.payload_sha256 AS target_sha256,"
                                  "r.payload_size AS target_size FROM app_release_policy p "
                                  "JOIN app_releases r ON r.tag=p.promoted_tag "
                                  "WHERE p.singleton").fetchone()
            fleet = {"source": "legacy_promotion", "revision": 0,
                     "target": typed_artifact(legacy, PAYLOAD_FORMAT)}
        override = conn.execute("SELECT * FROM fleet_device_app_overrides "
                                "WHERE device_id=%s", (device_id,)).fetchone()
        override_doc = (None if override is None else
                        {"revision": override["revision"],
                         "target": typed_artifact(override, override["target_format"])})
        return fleet, override_doc

    def create_offer(self, request: OfferRequest) -> dict:
        serial = request.validated_serial()
        device_id = device_id_for_serial(serial)
        assert device_id is not None
        now = self.clock.utc()
        # This quota commits even when a duplicate offer returns early or a later
        # policy/byte preflight fails. Idempotency must not make hashing free.
        with self.db.transaction() as quota_conn:
            self._claim_quota(quota_conn, device_id=device_id, kind="offer", now=now)
            quota_conn.execute("SELECT pg_advisory_xact_lock(%s)", (_OFFER_LOCK,))
            self._retire_expired_offer_assets(quota_conn, now)
        with self.db.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_OFFER_LOCK,))
            existing = conn.execute(
                "SELECT * FROM fleet_boot_offers WHERE installation_audience=%s "
                "AND device_id=%s AND (kernel_boot_id=%s OR boot_nonce=%s) FOR UPDATE",
                (self.audience, device_id, request.kernel_boot_id, request.boot_nonce),
            ).fetchall()
            if existing:
                row = existing[0]
                if (len(existing) != 1 or row["kernel_boot_id"] != request.kernel_boot_id
                        or row["boot_nonce"] != request.boot_nonce or row["serial"] != serial):
                    raise FleetError("boot_offer_conflict")
                if row["expires_at"] <= now:
                    raise FleetError("boot_offer_expired", 410)
                return _offer_document(row)
            self._claim_new_device(conn, device_id=device_id, serial=serial, now=now)
            device = conn.execute("SELECT attached_tag,retired_at FROM devices "
                                  "WHERE device_id=%s FOR UPDATE", (device_id,)).fetchone()
            if device["retired_at"] is not None:
                raise FleetError("device_retired", 403)
            baseline = conn.execute("SELECT revision,tag FROM fleet_base_policy WHERE singleton").fetchone()
            base_tag = device["attached_tag"] or (baseline["tag"] if baseline else None)
            if base_tag is None:
                raise FleetError("recovery_required", 503)
            base = conn.execute("SELECT r.tag,r.base_tarball_url,r.base_tarball_sha256,"
                                "r.base_tarball_size,r.base_abi,r.base_abi_squashfs_sha256,"
                                "c.squashfs_sha256,c.size,c.state "
                                "FROM app_releases r JOIN base_cache c ON c.tag=r.tag "
                                "WHERE r.tag=%s", (base_tag,)).fetchone()
            if (base is None or base["state"] != "cached" or not base["base_tarball_sha256"]
                    or not base["base_tarball_url"] or not base["base_tarball_size"]
                    or not base["squashfs_sha256"] or not base["size"]
                    or base["size"] > MAX_ROOTFS_BYTES):
                raise FleetError("base_unavailable", 503)
            if (not base["base_abi"] or
                    base["base_abi_squashfs_sha256"] != base["squashfs_sha256"]):
                # An old base ignores the handoff and could fetch a mutable .deb.
                raise FleetError("base_incapable", 503)
            fleet, override = self._app_policy(conn, device_id)
            desired = effective_app(fleet, override)
            target = desired["target"]
            app, app_release = None, None
            app_status, compatibility_basis, app_abi_key = "unconfigured", "none", None
            if target is not None:
                app_release = conn.execute(
                    "SELECT tag,payload_url,payload_sha256,payload_size,payload_format,"
                    "payload_base_abi,payload_source_manifest,mirror_state "
                    "FROM app_releases WHERE tag=%s", (target["tag"],)).fetchone()
                if (target["format"] != PAYLOAD_FORMAT or app_release is None
                        or app_release["payload_sha256"] != target["sha256"]
                        or app_release["payload_size"] != target["size"]
                        or not 0 < app_release["payload_size"] <= MAX_ARCHIVE_BYTES
                        or not app_release["payload_url"]
                        or app_release["payload_format"] != PAYLOAD_FORMAT
                        or app_release["payload_source_manifest"] != "manifest.v2.json"
                        or app_release["mirror_state"] in
                        ("divergent", "withdrawn", "undeployable")):
                    app_status = "unavailable"
                elif base["base_abi"] == app_release["payload_base_abi"]:
                    app, app_status = target, "selected"
                    compatibility_basis, app_abi_key = (
                        "abi_match", app_release["payload_base_abi"])
                else:
                    app_status = "compatibility_unverified"
            offer_id = uuid4()
            row = conn.execute(
                "INSERT INTO fleet_boot_offers(offer_id,installation_audience,device_id,serial,"
                "kernel_boot_id,boot_nonce,base_policy_source,base_policy_revision,"
                "app_policy_source,app_policy_revision,base_tag,"
                "base_content_key,base_sha256,base_size,app_tag,app_sha256,app_size,app_status,"
                "compatibility_basis,app_abi_key,offer_schema,app_format,app_base_abi,"
                "created_at,expires_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "RETURNING *",
                (offer_id, self.audience, device_id, serial, request.kernel_boot_id,
                 request.boot_nonce, "pin" if device["attached_tag"] else "operator_baseline",
                 0 if device["attached_tag"] else baseline["revision"],
                 desired["source"], desired["revision"], base_tag,
                 base["base_tarball_sha256"],
                 base["squashfs_sha256"], base["size"], app["tag"] if app else None,
                 app["sha256"] if app else None, app["size"] if app else None, app_status,
                 compatibility_basis, app_abi_key, 2,
                 PAYLOAD_FORMAT if app else None, app_abi_key if app else None,
                 now, now + OFFER_TTL_SECONDS),
            ).fetchone()
            conn.execute("INSERT INTO fleet_offer_artifact_roots(offer_id,kind,content_key,"
                         "sha256,size,retain_until) VALUES(%s,'base',%s,%s,%s,%s)",
                         (offer_id, base["base_tarball_sha256"], base["squashfs_sha256"],
                          base["size"], row["expires_at"]))
            self._retain_offer_asset(
                conn, offer_id=offer_id, kind="base", identity=base["base_tarball_sha256"],
                url=base["base_tarball_url"], source_sha256=base["base_tarball_sha256"],
                source_size=base["base_tarball_size"], expected_sha256=None,
                expected_size=None, now=now)
            if app is not None:
                conn.execute("INSERT INTO fleet_offer_artifact_roots(offer_id,kind,content_key,"
                             "sha256,size,retain_until) VALUES(%s,'app',%s,%s,%s,%s)",
                             (offer_id, app["sha256"], app["sha256"], app["size"],
                              row["expires_at"]))
                assert app_release is not None
                self._retain_offer_asset(
                    conn, offer_id=offer_id, kind="app", identity=app["sha256"],
                    url=app_release["payload_url"], source_sha256=app["sha256"],
                    source_size=app["size"], expected_sha256=app["sha256"],
                    expected_size=app["size"], now=now)
            return _offer_document(row)

    def offer_asset(self, offer_id: UUID, kind: str) -> OfferAsset:
        if kind not in ("base", "app"):
            raise FleetError("boot_offer_asset_not_found", 404)
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM fleet_boot_offers WHERE offer_id=%s "
                               "AND installation_audience=%s", (offer_id, self.audience)).fetchone()
        if row is None:
            raise FleetError("boot_offer_not_found", 404)
        if row["expires_at"] <= self.clock.utc():
            raise FleetError("boot_offer_expired", 410)
        with self.db.transaction() as quota_conn:
            self._claim_quota(quota_conn, device_id=row["device_id"], kind="asset",
                              now=self.clock.utc())
        if kind == "base":
            return OfferAsset(kind, row["base_tag"], row["base_content_key"],
                              row["base_sha256"], row["base_size"])
        if row["app_sha256"] is None:
            raise FleetError("app_unconfigured" if row["app_status"] == "unconfigured"
                             else "app_unavailable", 503)
        return OfferAsset(kind, row["app_tag"], row["app_sha256"], row["app_sha256"],
                          row["app_size"], row["app_format"])

    def evict_if_unretained(self, *, kind: str, content_key: str,
                            evict: Callable[[], None]) -> bool:
        """Future cache-GC seam: hold the offer lock through the actual unlink.

        All future acceptance/attempt root writers must share this lock. Open response FDs
        lease their inodes independently. No periodic cache cleaner ships today.
        """
        if kind not in ("base", "app") or len(content_key) != 64 or any(
            char not in "0123456789abcdef" for char in content_key
        ):
            raise ValueError("invalid_asset_identity")
        now = self.clock.utc()
        with self.db.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_OFFER_LOCK,))
            root = conn.execute(
                "SELECT 1 FROM fleet_offer_artifact_roots WHERE kind=%s AND content_key=%s "
                "AND retain_until>%s LIMIT 1", (kind, content_key, now),
            ).fetchone()
            if root:
                return False
            accepted = conn.execute(
                "SELECT 1 FROM fleet_accepted_artifacts WHERE kind=%s AND content_key=%s "
                "LIMIT 1", (kind, content_key),
            ).fetchone()
            if accepted:
                return False
            if kind == "app":
                attempt = conn.execute(
                    "SELECT 1 FROM fleet_app_attempts WHERE "
                    "(target_sha256=%s OR fallback_sha256=%s) AND phase NOT IN "
                    "('operational','observed_failed','expired_unknown','recovery_required') "
                    "LIMIT 1", (content_key, content_key),
                ).fetchone()
                if attempt:
                    return False
            evict()
            return True

    def record_check_in(self, request: CheckIn | CheckInV2) -> dict:
        serial = sanitize_serial(request.serial)
        if serial is None:
            raise FleetError("invalid_serial", 422)
        device_id = device_id_for_serial(serial)
        assert device_id is not None
        # T0 is unauthenticated. Charge every syntactically valid check-in before
        # any idempotency/offer lookup or per-boot lock, so cheap duplicate and
        # mismatched-offer traffic cannot bypass the daily DB admission cap.
        now = self.clock.utc()
        with self.db.transaction() as quota_conn:
            self._claim_quota(quota_conn, device_id=device_id, kind="observation", now=now)
        with self.db.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s,hashtext(%s))",
                         (_OBS_LOCK_CLASS, device_id + str(request.kernel_boot_id)))
            offer = None
            if request.offer_id is not None:
                offer = conn.execute("SELECT device_id,kernel_boot_id,boot_nonce,base_sha256 "
                                     "FROM fleet_boot_offers WHERE offer_id=%s AND "
                                     "installation_audience=%s", (request.offer_id, self.audience)).fetchone()
                if (offer is None or offer["device_id"] != device_id
                        or offer["kernel_boot_id"] != request.kernel_boot_id
                        or (request.boot_nonce is not None and offer["boot_nonce"] != request.boot_nonce)
                        or (request.base_digest is not None and
                            offer["base_sha256"] != request.base_digest)):
                    raise FleetError("boot_offer_mismatch")
            last = conn.execute("SELECT max(observation_sequence) AS n FROM fleet_os_observations "
                                "WHERE device_id=%s AND kernel_boot_id=%s",
                                (device_id, request.kernel_boot_id)).fetchone()["n"]
            if last is not None and request.observation_sequence <= last:
                return {"accepted": False, "reason": "stale_or_duplicate",
                        "next_sequence": last + 1 if last < 2147483647 else None}
            self._claim_new_device(conn, device_id=device_id, serial=serial, now=now)
            evidence = request.app_evidence if isinstance(request, CheckInV2) else None
            running = evidence.running if evidence is not None else None
            conn.execute("INSERT INTO fleet_os_observations(device_id,kernel_boot_id,"
                         "agent_incarnation,observation_sequence,offer_id,base_digest,phase,"
                         "fault_code,attempted_app_sha256,sampled_boottime_ms,received_at,"
                         "observation_schema,app_installed_sha256,app_running_sha256,"
                         "app_running_pid,app_running_start_ticks,app_running_invocation_id,"
                         "app_installed_reason,app_running_reason) "
                         "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         (device_id, request.kernel_boot_id, request.agent_incarnation,
                          request.observation_sequence, request.offer_id, request.base_digest,
                          request.phase, request.fault_code, request.attempted_app_sha256,
                          request.sampled_boottime_ms, now, request.schema_version,
                          evidence.installed_sha256 if evidence is not None else None,
                          running.sha256 if running is not None else None,
                          running.pid if running is not None else None,
                          running.start_ticks if running is not None else None,
                          running.invocation_id if running is not None else None,
                          evidence.installed_reason if evidence is not None else None,
                          evidence.running_reason if evidence is not None else None))
            # Bounded per-boot history; the latest sequence and its fault always survive.
            conn.execute("DELETE FROM fleet_os_observations WHERE device_id=%s "
                         "AND kernel_boot_id=%s AND observation_sequence < %s",
                         (device_id, request.kernel_boot_id,
                          max(0, request.observation_sequence - 63)))
            # Opportunistic global TTL in bounded batches; boot offers remain immutable.
            conn.execute("DELETE FROM fleet_os_observations WHERE ctid IN "
                         "(SELECT ctid FROM fleet_os_observations WHERE received_at<%s "
                         "ORDER BY received_at LIMIT 1000)", (now - _OBS_TTL_SECONDS,))
            return {"accepted": True}

    def status(self) -> dict:
        read_at = self.clock.utc()
        with self.db.transaction() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            fleet, _ = self._app_policy(conn, "")
            base_policy = conn.execute("SELECT revision,tag,source FROM fleet_base_policy "
                                       "WHERE singleton").fetchone()
            releases = conn.execute("SELECT r.tag,r.payload_sha256,r.payload_size,"
                                    "r.payload_format,r.payload_base_abi,r.payload_source_manifest,"
                                    "r.base_tarball_sha256,"
                                    "r.base_tarball_size,r.mirror_state,c.state AS base_cache_state,"
                                    "c.squashfs_sha256,c.size AS squashfs_size "
                                    "FROM app_releases r LEFT JOIN base_cache c ON c.tag=r.tag "
                                    "ORDER BY r.major DESC,r.minor DESC,r.patch DESC,r.tag DESC "
                                    "LIMIT 500").fetchall()
            devices = conn.execute("SELECT device_id,serial,known_good_tag,boot_outcome,"
                                   "last_served_tag,attached_tag FROM devices "
                                   "WHERE retired_at IS NULL ORDER BY device_id LIMIT 5000").fetchall()
            overrides = {r["device_id"]: r for r in conn.execute(
                "SELECT * FROM fleet_device_app_overrides").fetchall()}
            observations: dict[str, list[dict]] = {}
            for observation in conn.execute(
                "SELECT DISTINCT ON(device_id,kernel_boot_id) * FROM fleet_os_observations "
                "WHERE received_at>=%s ORDER BY device_id,kernel_boot_id,"
                "observation_sequence DESC,received_at DESC",
                (read_at - _OBS_TTL_SECONDS,),
            ).fetchall():
                observations.setdefault(observation["device_id"], []).append(observation)
            offers = {r["device_id"]: r for r in conn.execute(
                "SELECT DISTINCT ON(device_id) * FROM fleet_boot_offers "
                "ORDER BY device_id,offer_sequence DESC").fetchall()}
            players = {r["device_id"]: r for r in conn.execute(
                "SELECT DISTINCT ON(device_id) device_id,id,authority_epoch,last_seen,health "
                "FROM players WHERE retired_at IS NULL ORDER BY device_id,last_seen DESC").fetchall()}
            control = {r["player_id"]: r for r in conn.execute(
                "SELECT player_id,authority_epoch,status,schema_version,applied_at,"
                "applied_delivery_id,applied_digest,last_delivery_id,last_result_sequence,"
                "last_result_digest,last_result,last_result_at "
                "FROM player_control_sessions").fetchall()}
            accepted = {(r["device_id"], r["kind"]): r for r in conn.execute(
                "SELECT DISTINCT ON(device_id,kind) * FROM fleet_accepted_artifacts "
                "ORDER BY device_id,kind,accepted_at DESC").fetchall()}
            result = []
            for device in devices:
                device_id = device["device_id"]
                override = overrides.get(device_id)
                override_doc = (None if override is None else
                                {"revision": override["revision"], "target":
                                 ({**_artifact(override), "format": override["target_format"]}
                                  if _artifact(override) else None)})
                desired = effective_app(fleet, override_doc)
                offer = offers.get(device_id)
                obs = observations.get(device_id, [])
                player = players.get(device_id)
                session = control.get(player["id"]) if player else None
                base = boot_claim_status(observations=obs, offer=offer, legacy=device,
                                         read_at=read_at)
                installed, running = app_observation_status(
                    observations=obs, offer=offer, read_at=read_at)
                accepted_app = accepted.get((device_id, "app"))
                app_fact = app_control_status(player=player, session=session, read_at=read_at)
                result.append({
                    "device_id": device_id, "serial": device["serial"],
                    "capability": (f"offer_v{offer['offer_schema']}_claimed" if offer
                                   else "legacy_or_unknown"),
                    "desired": {**desired, "artifact": desired["target"]},
                    "override": override_doc if override_doc and
                    override_doc["target"] is not None else None,
                    "override_revision": override_doc["revision"] if override_doc else 0,
                    "offered": None if offer is None else {
                        "offer_id": str(offer["offer_id"]), "schema": offer["offer_schema"],
                        "boot_id": str(offer["kernel_boot_id"]),
                        "base_digest": offer["base_sha256"], "app_digest": offer["app_sha256"],
                        "app_status": offer["app_status"], "app_format": offer["app_format"],
                        "base_policy_source": offer["base_policy_source"],
                        "base_policy_revision": offer["base_policy_revision"],
                        "app_policy_source": offer["app_policy_source"],
                        "app_policy_revision": offer["app_policy_revision"],
                        "compatibility_basis": offer["compatibility_basis"],
                        "at": offer["created_at"], "expires_at": offer["expires_at"]},
                    "installed": installed, "running": running,
                    "accepted_fallback": None if accepted_app is None else {
                        "sha256": accepted_app["sha256"], "size": accepted_app["size"],
                        "at": accepted_app["accepted_at"], "assurance": "accepted_record"},
                    "fallback": fallback_classification(
                        accepted_digest=accepted_app["sha256"] if accepted_app else None,
                        obtainable=False, compatible=False, legacy_tag=device["known_good_tag"]),
                    "base": base,
                    "app": app_fact,
                    "output": {"state": "not_currently_qualified", "source": "none",
                               "age_seconds": None},
                    "update_now": {"available": False, "reason": "command_trust_unapproved"},
                })
            release_docs = [{
                "tag": r["tag"],
                "app": {"sha256": r["payload_sha256"], "size": r["payload_size"],
                        "format": r["payload_format"], "base_abi": r["payload_base_abi"]}
                if r["payload_sha256"] and r["payload_size"] and
                r["payload_format"] == PAYLOAD_FORMAT and
                r["payload_source_manifest"] == "manifest.v2.json" else None,
                "base": {"sha256": r["squashfs_sha256"], "size": r["squashfs_size"]}
                if r["squashfs_sha256"] and r["squashfs_size"] else None,
                "mirror_state": r["mirror_state"],
                "deployable": bool(r["payload_sha256"] and r["payload_size"] and
                                   r["payload_format"] == PAYLOAD_FORMAT and
                                   r["payload_source_manifest"] == "manifest.v2.json" and
                                   r["mirror_state"] not in
                                   ("divergent", "withdrawn", "undeployable")),
                # Catalog state is not a local file-open proof on every serving pod.
                "available_now": None,
                "base_cache_state": r["base_cache_state"],
            } for r in releases]
            return {"read_at": read_at, "fleet_policy": fleet,
                    "base_baseline": {"revision": base_policy["revision"],
                                      "tag": base_policy["tag"], "source": base_policy["source"]}
                    if base_policy else {"revision": 0, "tag": None,
                                         "source": "unconfigured"},
                    "releases": release_docs, "devices": result,
                    "assurance": "t0_observational", "commands_available": False}
