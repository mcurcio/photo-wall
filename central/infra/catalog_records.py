"""Postgres `ReleaseRecords` / `DeviceRecords` over the existing tables, unchanged.

`app_releases` (016 + 018's base_* columns), `app_release_policy`, `app_release_poll`, `devices`
(018) and `bindings` (001). The SQL is ported from `central/app_releases.py`,
`central/app_release_service.py`, `central/installation_repository.py` and
`central/netboot_base.py`. Each method runs in the caller's transaction; a fake transaction is a
`TypeError` (`pg_connection`).
"""

from __future__ import annotations

from typing import Any

from central.content_catalog.ports import DeviceRow, DeviceUpdate, ReleaseRow
from central.infra.transactions import pg_connection
from central.kernel.assets import OriginLocator
from central.kernel.ports import PublishedRelease
from central.kernel.transactions import Transaction
from central.kernel.types import release_version

_RELEASE_COLUMNS = (
    "tag, is_prerelease, asset_url, asset_sha256, asset_size, "
    "base_tarball_url, base_tarball_sha256, base_tarball_size"
)
_DEVICE_COLUMNS = (
    "device_id, serial, attached_tag, known_good_tag, last_served_tag, boot_outcome, "
    "failed_tag, last_served_at, retired_at"
)


def _locator(url: str | None, sha256: str | None, size: int | None) -> OriginLocator | None:
    if url is None or sha256 is None or size is None:
        return None
    return OriginLocator(url, sha256, size)


def _release(row: dict[str, Any]) -> ReleaseRow:
    return ReleaseRow(
        tag=row["tag"],
        is_prerelease=row["is_prerelease"],
        package=_locator(row["asset_url"], row["asset_sha256"], row["asset_size"]),
        os_image=_locator(row["base_tarball_url"], row["base_tarball_sha256"],
                          row["base_tarball_size"]),
    )


def _device(row: dict[str, Any]) -> DeviceRow:
    return DeviceRow(
        device_id=row["device_id"],
        serial=row["serial"],
        attached_tag=row["attached_tag"],
        known_good_tag=row["known_good_tag"],
        last_served_tag=row["last_served_tag"],
        boot_outcome=row["boot_outcome"],
        failed_tag=row["failed_tag"],
        last_served_at=row["last_served_at"],
        retired=row["retired_at"] is not None,
    )


def _facts(locator: OriginLocator | None) -> tuple[str | None, str | None, int | None]:
    if locator is None:
        return None, None, None
    return locator.url, locator.sha256, locator.size


class PgReleaseRecords:
    """Implements `ReleaseRecords`."""

    def get(self, tx: Transaction, tag: str) -> ReleaseRow | None:
        row = pg_connection(tx).execute(
            f"SELECT {_RELEASE_COLUMNS} FROM app_releases WHERE tag=%s", (tag,)
        ).fetchone()
        return None if row is None else _release(row)

    def all(self, tx: Transaction) -> tuple[ReleaseRow, ...]:
        rows = pg_connection(tx).execute(
            f"SELECT {_RELEASE_COLUMNS} FROM app_releases ORDER BY tag"
        ).fetchall()
        return tuple(_release(row) for row in rows)

    def upsert(self, tx: Transaction, release: PublishedRelease, *,
               now: float) -> ReleaseRow | None:
        conn = pg_connection(tx)
        previous = conn.execute(
            f"SELECT {_RELEASE_COLUMNS} FROM app_releases WHERE tag=%s FOR UPDATE",
            (release.tag,),
        ).fetchone()
        version = release_version(release.tag)
        asset = _facts(release.package)  # url, sha256, size
        base = _facts(release.os_image)
        # mirror_state is a legacy NOT NULL column that nothing reads; written on insert only.
        mirror_state = "discovered" if release.package is not None else "undeployable"
        conn.execute(
            "INSERT INTO app_releases(tag,major,minor,patch,prerelease,is_prerelease,"
            "asset_url,asset_sha256,asset_size,base_tarball_url,base_tarball_sha256,"
            "base_tarball_size,mirror_state,discovered_at,updated_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(tag) DO UPDATE SET major=EXCLUDED.major,minor=EXCLUDED.minor,"
            "patch=EXCLUDED.patch,prerelease=EXCLUDED.prerelease,"
            "is_prerelease=EXCLUDED.is_prerelease,asset_url=EXCLUDED.asset_url,"
            "asset_sha256=EXCLUDED.asset_sha256,asset_size=EXCLUDED.asset_size,"
            "base_tarball_url=EXCLUDED.base_tarball_url,"
            "base_tarball_sha256=EXCLUDED.base_tarball_sha256,"
            "base_tarball_size=EXCLUDED.base_tarball_size,updated_at=EXCLUDED.updated_at",
            (release.tag, version.major, version.minor, version.patch, version.prerelease,
             release.is_prerelease, *asset, *base, mirror_state, now, now),
        )
        return None if previous is None else _release(previous)

    def promoted_tag(self, tx: Transaction) -> str | None:
        row = pg_connection(tx).execute(
            "SELECT promoted_tag FROM app_release_policy WHERE singleton"
        ).fetchone()
        return None if row is None else row["promoted_tag"]

    def set_promoted(self, tx: Transaction, tag: str) -> None:
        conn = pg_connection(tx)
        conn.execute("SELECT promoted_tag FROM app_release_policy WHERE singleton FOR UPDATE")
        conn.execute(
            "INSERT INTO app_release_policy VALUES(TRUE,%s) ON CONFLICT(singleton) "
            "DO UPDATE SET promoted_tag=EXCLUDED.promoted_tag",
            (tag,),
        )

    def load_etag(self, tx: Transaction) -> str | None:
        row = pg_connection(tx).execute(
            "SELECT etag FROM app_release_poll WHERE singleton"
        ).fetchone()
        return None if row is None else row["etag"]

    def store_etag(self, tx: Transaction, etag: str | None) -> None:
        pg_connection(tx).execute(
            "INSERT INTO app_release_poll(singleton,etag) VALUES(TRUE,%s) "
            "ON CONFLICT(singleton) DO UPDATE SET etag=EXCLUDED.etag",
            (etag,),
        )

    def bound_player_count(self, tx: Transaction) -> int:
        row = pg_connection(tx).execute(
            "SELECT count(DISTINCT player_id) AS n FROM bindings"
        ).fetchone()
        return int(row["n"])


class PgDeviceRecords:
    """Implements `DeviceRecords`."""

    def lock(self, tx: Transaction, device_id: str, serial: str, *, now: float) -> DeviceRow:
        conn = pg_connection(tx)
        conn.execute(
            "INSERT INTO devices(device_id, serial, first_seen, last_seen) VALUES(%s,%s,%s,%s) "
            "ON CONFLICT(device_id) DO UPDATE SET serial=EXCLUDED.serial, "
            "last_seen=EXCLUDED.last_seen",
            (device_id, serial, now, now),
        )
        row = conn.execute(
            f"SELECT {_DEVICE_COLUMNS} FROM devices WHERE device_id=%s FOR UPDATE", (device_id,)
        ).fetchone()
        return _device(row)

    def active(self, tx: Transaction) -> tuple[DeviceRow, ...]:
        rows = pg_connection(tx).execute(
            f"SELECT {_DEVICE_COLUMNS} FROM devices WHERE retired_at IS NULL ORDER BY device_id"
        ).fetchall()
        return tuple(_device(row) for row in rows)

    def get(self, tx: Transaction, device_id: str) -> DeviceRow | None:
        row = pg_connection(tx).execute(
            f"SELECT {_DEVICE_COLUMNS} FROM devices WHERE device_id=%s", (device_id,)
        ).fetchone()
        return None if row is None else _device(row)

    def apply(self, tx: Transaction, device_id: str, update: DeviceUpdate) -> None:
        pg_connection(tx).execute(
            "UPDATE devices SET failed_tag=%s, "
            "boot_outcome=CASE WHEN %s THEN 'failed' ELSE boot_outcome END "
            "WHERE device_id=%s",
            (update.failed_tag, update.mark_boot_failed, device_id),
        )

    def record_served(self, tx: Transaction, device_id: str, tag: str, *, now: float) -> None:
        pg_connection(tx).execute(
            "UPDATE devices SET last_served_tag=%s, boot_outcome='pending', last_served_at=%s "
            "WHERE device_id=%s",
            (tag, now, device_id),
        )

    def set_pin(self, tx: Transaction, device_id: str, tag: str | None) -> bool:
        return pg_connection(tx).execute(
            "UPDATE devices SET attached_tag=%s WHERE device_id=%s", (tag, device_id)
        ).rowcount == 1

    def sweep_failed_boots(self, tx: Transaction, *, served_before: float) -> int:
        # The exact SQL of netboot_base.sweep_failed_boots: E2 never overwrites a live fence;
        # E2b fences the tag the device ACTUALLY attempted (its pin, else what it was served).
        return pg_connection(tx).execute(
            "UPDATE devices SET boot_outcome='failed', "
            "failed_tag = CASE WHEN failed_tag IS NULL "
            "THEN COALESCE(attached_tag, last_served_tag) ELSE failed_tag END "
            "WHERE boot_outcome='pending' AND retired_at IS NULL "
            "AND last_served_at IS NOT NULL AND last_served_at < %s",
            (served_before,),
        ).rowcount
