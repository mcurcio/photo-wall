"""Central release authority. A trial is consumed before its ticket is returned.

Release records are immutable, selection is per physical device, and health is
bound to the currently enrolled session. The bootstrap stores nothing locally.
Transactions that also touch enrollment lock the Player before the device row.
"""

from __future__ import annotations

import base64
import math
import re
import secrets

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from central.db import Database
from central.installation_ports import InstallationSessions
from central.installation_repository import PostgresInstallationRepository
from contracts.release import MAX_MANIFEST_BYTES, BootRequest, BootTicket, Release
from contracts.time import Clock


class ReleaseError(ValueError):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


class ReleaseAuthority:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        public_key: Ed25519PublicKey,
        boot_abi: str,
        configuration_sha256: str,
        *,
        health_seconds: float = 30,
        health_max_age: float = 2,
        installation: InstallationSessions | None = None,
    ):
        if (
            not isinstance(public_key, Ed25519PublicKey)
            or not math.isfinite(health_seconds)
            or not math.isfinite(health_max_age)
            or health_seconds <= 0
            or health_max_age <= 0
        ):
            raise ValueError("invalid_release_policy")
        self.db, self.clock, self.public_key = db, clock, public_key
        self.boot_abi, self.configuration_sha256 = boot_abi, configuration_sha256
        self.health_seconds, self.health_max_age = health_seconds, health_max_age
        self.installation = installation or PostgresInstallationRepository(clock)

    def _verify(self, manifest: bytes, signature: bytes) -> Release:
        if (
            not isinstance(manifest, bytes)
            or not 0 < len(manifest) <= MAX_MANIFEST_BYTES
            or not isinstance(signature, bytes)
            or len(signature) != 64
        ):
            raise ReleaseError("invalid_release", 422)
        try:
            self.public_key.verify(signature, manifest)
            release = Release.decode(manifest)
            release.require_compatible(self.boot_abi, self.configuration_sha256)
        except (InvalidSignature, ValueError, TypeError):
            raise ReleaseError("invalid_release", 422) from None
        return release

    def register(self, manifest: bytes, signature: bytes) -> Release:
        """Register authenticated canonical metadata; gateway verifies exact bytes."""
        release = self._verify(manifest, signature)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO appliance_releases VALUES(%s,%s,%s,%s) "
                "ON CONFLICT(release_id) DO NOTHING",
                (release.release_id, manifest, signature, self.clock.utc()),
            )
            row = conn.execute(
                "SELECT manifest,signature FROM appliance_releases WHERE release_id=%s",
                (release.release_id,),
            ).fetchone()
            if (bytes(row["manifest"]), bytes(row["signature"])) != (manifest, signature):
                raise ReleaseError("release_immutable")
        return release

    def _release(self, conn, release_id):
        row = conn.execute(
            "SELECT * FROM appliance_releases WHERE release_id=%s", (release_id,)
        ).fetchone()
        if row is None:
            raise ReleaseError("release_not_found", 404)
        release = self._verify(bytes(row["manifest"]), bytes(row["signature"]))
        if release.release_id != release_id:
            raise ReleaseError("release_identity_mismatch")
        return row

    def release_for_rootfs(self, rootfs_sha256: str) -> Release:
        """Return verified metadata only for an immutable registered root image."""
        if not isinstance(rootfs_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", rootfs_sha256):
            raise ReleaseError("release_not_found", 404)
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT release_id FROM appliance_releases WHERE "
                "convert_from(manifest,'UTF8')::jsonb->>'rootfs_sha256'=%s "
                "ORDER BY release_id LIMIT 1",
                (rootfs_sha256,),
            ).fetchone()
            if row is None:
                raise ReleaseError("release_not_found", 404)
            verified = self._release(conn, row["release_id"])
            return Release.decode(bytes(verified["manifest"]))

    def set_default(self, release_id: str) -> None:
        """The operator-qualified accepted release for newly observed equipment."""
        with self.db.transaction() as conn:
            self._release(conn, release_id)
            conn.execute(
                "INSERT INTO appliance_release_policy VALUES(TRUE,%s) ON CONFLICT(singleton) "
                "DO UPDATE SET accepted_release_id=EXCLUDED.accepted_release_id",
                (release_id,),
            )

    def initialize_default(self, release_id: str) -> None:
        """Seed configured startup once without replacing the operator's choice."""
        with self.db.transaction() as conn:
            self._release(conn, release_id)
            conn.execute(
                "INSERT INTO appliance_release_policy VALUES(TRUE,%s) "
                "ON CONFLICT(singleton) DO NOTHING",
                (release_id,),
            )

    def stage(self, device_id: str, release_id: str) -> bool:
        with self.db.transaction() as conn:
            device = self._device(conn, device_id)
            self._release(conn, release_id)
            if device["accepted_release_id"] == release_id:
                return False
            if conn.execute(
                "SELECT 1 FROM appliance_release_trials WHERE device_id=%s AND release_id=%s",
                (device_id, release_id),
            ).fetchone():
                raise ReleaseError("trial_already_consumed")
            if (
                device["candidate_release_id"] not in (None, release_id)
                and not conn.execute(
                    "SELECT 1 FROM appliance_release_trials WHERE device_id=%s AND release_id=%s",
                    (device_id, device["candidate_release_id"]),
                ).fetchone()
            ):
                raise ReleaseError("candidate_already_staged")
            conn.execute(
                "UPDATE appliance_devices SET candidate_release_id=%s WHERE device_id=%s",
                (release_id, device_id),
            )
            return True

    @staticmethod
    def _device(conn, device_id):
        row = conn.execute(
            "SELECT * FROM appliance_devices WHERE device_id=%s FOR UPDATE", (device_id,)
        ).fetchone()
        if row is None:
            raise ReleaseError("device_not_found", 404)
        return row

    def _ticket(self, conn, attempt) -> BootTicket:
        release = self._release(conn, attempt["release_id"])
        return BootTicket(
            ticket_id=attempt["ticket_id"],
            device_id=attempt["device_id"],
            boot_id=attempt["boot_id"],
            request_id=attempt["request_id"],
            release_id=attempt["release_id"],
            trial=attempt["trial"],
            manifest=bytes(release["manifest"]).decode(),
            signature=base64.b64encode(bytes(release["signature"])).decode(),
        )

    def select_boot(self, request: BootRequest) -> BootTicket:
        with self.db.transaction() as conn:
            default = conn.execute(
                "SELECT accepted_release_id FROM appliance_release_policy WHERE singleton"
            ).fetchone()
            if default is None:
                raise ReleaseError("release_unconfigured", 503)
            conn.execute(
                "INSERT INTO appliance_devices(device_id,accepted_release_id) VALUES(%s,%s) "
                "ON CONFLICT(device_id) DO NOTHING",
                (request.device_id, default["accepted_release_id"]),
            )
            device = self._device(conn, request.device_id)
            old = conn.execute(
                "SELECT * FROM appliance_boot_attempts WHERE device_id=%s AND "
                "(request_id=%s OR boot_id=%s)",
                (request.device_id, request.request_id, request.boot_id),
            ).fetchall()
            if old:
                if (
                    len(old) != 1
                    or old[0]["boot_id"] != request.boot_id
                    or old[0]["request_id"] != request.request_id
                ):
                    raise ReleaseError("boot_request_conflict")
                if old[0]["ticket_id"] != device["current_ticket_id"]:
                    raise ReleaseError("stale_boot_request")
                return self._ticket(conn, old[0])
            release_id, trial = device["accepted_release_id"], False
            candidate = device["candidate_release_id"]
            if (
                candidate
                and not conn.execute(
                    "SELECT 1 FROM appliance_release_trials WHERE device_id=%s AND release_id=%s",
                    (request.device_id, candidate),
                ).fetchone()
            ):
                release_id, trial = candidate, True
            self._release(conn, release_id)
            ticket_id = secrets.token_hex(24)
            conn.execute(
                "UPDATE appliance_boot_attempts SET status=CASE WHEN trial AND status='booting' "
                "THEN 'failed' ELSE 'superseded' END WHERE ticket_id=%s",
                (device["current_ticket_id"],),
            )
            attempt = conn.execute(
                "INSERT INTO appliance_boot_attempts(ticket_id,device_id,boot_id,request_id,"
                "release_id,trial,issued_at,status) VALUES(%s,%s,%s,%s,%s,%s,%s,'booting') RETURNING *",
                (
                    ticket_id,
                    request.device_id,
                    request.boot_id,
                    request.request_id,
                    release_id,
                    trial,
                    self.clock.utc(),
                ),
            ).fetchone()
            if trial:
                conn.execute(
                    "INSERT INTO appliance_release_trials VALUES(%s,%s,%s)",
                    (request.device_id, release_id, ticket_id),
                )
            conn.execute(
                "UPDATE appliance_devices SET current_ticket_id=%s,player_id=NULL,authority_epoch=NULL "
                "WHERE device_id=%s",
                (ticket_id, request.device_id),
            )
            return self._ticket(conn, attempt)

    def bind_session_in(
        self,
        conn,
        ticket_id: str,
        device_id: str,
        boot_id: str,
        player_id: str,
        authority_epoch: int,
    ) -> None:
        """Enrollment calls within its transaction, after locking the Player row."""
        device = self._device(conn, device_id)
        attempt = conn.execute(
            "SELECT * FROM appliance_boot_attempts WHERE ticket_id=%s", (ticket_id,)
        ).fetchone()
        if (
            attempt is None
            or attempt["device_id"] != device_id
            or attempt["boot_id"] != boot_id
            or ticket_id != device["current_ticket_id"]
            or attempt["status"] not in ("booting", "healthy")
        ):
            raise ReleaseError("stale_boot_ticket", 403)
        if (device["player_id"], device["authority_epoch"]) != (player_id, authority_epoch):
            conn.execute(
                "UPDATE appliance_boot_attempts SET healthy_since=NULL,health_received_at=NULL,"
                "health_observed_at=NULL WHERE ticket_id=%s",
                (ticket_id,),
            )
        conn.execute(
            "UPDATE appliance_devices SET player_id=%s,authority_epoch=%s WHERE device_id=%s",
            (player_id, authority_epoch, device_id),
        )

    def health(
        self,
        ticket_id: str,
        player_id: str,
        authority_epoch: int,
        *,
        healthy: bool,
        observed_at: float,
    ) -> dict:
        now = self.clock.utc()
        if (
            type(healthy) is not bool
            or type(observed_at) not in (int, float)
            or not math.isfinite(observed_at)
            or not math.isfinite(now)
        ):
            raise ReleaseError("invalid_boot_health", 422)
        with self.db.transaction() as conn:
            if not self.installation.session_is_current_in(
                conn, player_id, authority_epoch, lock=True
            ):
                raise ReleaseError("stale_session", 403)
            attempt = conn.execute(
                "SELECT * FROM appliance_boot_attempts WHERE ticket_id=%s", (ticket_id,)
            ).fetchone()
            if attempt is None:
                raise ReleaseError("stale_boot_ticket", 403)
            device = self._device(conn, attempt["device_id"])
            if (
                device["current_ticket_id"] != ticket_id
                or device["player_id"] != player_id
                or device["authority_epoch"] != authority_epoch
                or attempt["status"] not in ("booting", "healthy")
            ):
                raise ReleaseError("stale_boot_ticket", 403)
            # Re-read after the device lock serializes simultaneous reports.
            attempt = conn.execute(
                "SELECT * FROM appliance_boot_attempts WHERE ticket_id=%s", (ticket_id,)
            ).fetchone()
            if not 0 <= now - observed_at <= self.health_max_age:
                conn.execute(
                    "UPDATE appliance_boot_attempts SET healthy_since=NULL WHERE ticket_id=%s",
                    (ticket_id,),
                )
                return {"accepted": False, "reason": "stale_health"}
            if (
                attempt["health_observed_at"] is not None
                and observed_at <= attempt["health_observed_at"]
            ):
                return {
                    "accepted": attempt["status"] == "healthy",
                    "reason": "duplicate_health",
                    "release_id": attempt["release_id"],
                }
            continuous = (
                healthy
                and attempt["healthy_since"] is not None
                and attempt["health_received_at"] is not None
                and 0 <= now - attempt["health_received_at"] <= self.health_max_age
                and 0 <= observed_at - attempt["health_observed_at"] <= self.health_max_age
            )
            since = (attempt["healthy_since"] if continuous else now) if healthy else None
            accepted = bool(healthy and since is not None and now - since >= self.health_seconds)
            conn.execute(
                "UPDATE appliance_boot_attempts SET healthy_since=%s,health_received_at=%s,"
                "health_observed_at=%s,status=CASE WHEN %s THEN 'healthy' ELSE status END WHERE ticket_id=%s",
                (since, now, observed_at, accepted, ticket_id),
            )
            if accepted and attempt["trial"] and attempt["status"] != "healthy":
                if device["candidate_release_id"] != attempt["release_id"]:
                    raise ReleaseError("candidate_changed")
                conn.execute(
                    "UPDATE appliance_devices SET accepted_release_id=%s,candidate_release_id=NULL "
                    "WHERE device_id=%s",
                    (attempt["release_id"], attempt["device_id"]),
                )
            return {"accepted": accepted, "release_id": attempt["release_id"]}

    def inventory(self) -> list[dict]:
        with self.db.transaction() as conn:
            return conn.execute(
                "SELECT device_id,accepted_release_id,candidate_release_id,player_id,authority_epoch "
                "FROM appliance_devices ORDER BY device_id"
            ).fetchall()
