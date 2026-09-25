"""Postgres `ReleaseRecords` / `DeviceRecords` over the existing tables.

`app_releases` (016 + 018's base_* columns + 029's upstream version), `app_release_policy` (+
023's last_good_tag and 027's promoted_by), `app_release_poll` (+ 029's etag_stored_at),
`devices` (018; 024's and 026's partial indexes serve the tag reads) and `bindings` (001). The SQL is ported from `central/app_releases.py`,
`central/app_release_service.py`, `central/installation_repository.py` and
`central/netboot_base.py`. Each method runs in the caller's transaction; a fake transaction is a
`TypeError` (`pg_connection`).
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import fields
from typing import Any, Final

from central.content_catalog.ports import (
    DeviceRow,
    DeviceUpdate,
    NamedTags,
    Promoter,
    Promotion,
    PromotionWrite,
    ReleaseRow,
    StoredEtag,
)
from central.infra.transactions import pg_connection
from central.kernel.assets import OriginLocator
from central.kernel.ports import PublishedRelease
from central.kernel.transactions import Transaction
from central.kernel.types import release_version

_RELEASE_COLUMNS = (
    "tag, is_prerelease, asset_url, asset_sha256, asset_size, "
    "base_tarball_url, base_tarball_sha256, base_tarball_size, mirror_state"
)
# The transaction-scoped advisory lock one automatic promotion holds from its first read to its
# write (docs/central-idempotent-jobs.md §4). Distinct from every other lock id in `central`
# (734118321 migrations in `central/db.py`, 322 registry, 323 runtime, 324 coordination, 325
# media, 326 media queue schema).
AUTO_PROMOTION_LOCK: Final = 734118327
_DEVICE_COLUMNS = (
    "device_id, serial, attached_tag, known_good_tag, last_served_tag, boot_outcome, "
    "failed_tag, last_served_at, retired_at"
)
# The roles through which an active device names a tag: (role, tag column, extra WHERE).
# `named_tags` and `names_any` are both generated from this one table, so they cannot disagree.
# Each role is a `NamedTags` field name; `named_tags` builds the result by name, and the import
# fails if the table and the fields differ. The served role counts only a serve at or after
# `%(served_since)s` (issue #25: fake-serial rows and long-gone devices must not keep a tag
# desired forever).
_NAMING_ROLES = (
    ("pinned", "attached_tag", ""),
    ("known_good", "known_good_tag", ""),
    ("served", "last_served_tag", " AND last_served_at >= %(served_since)s"),
)
if {role for role, _, _ in _NAMING_ROLES} != {field.name for field in fields(NamedTags)}:
    raise ImportError("_NAMING_ROLES must name exactly the NamedTags fields")


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
        divergent=row["mirror_state"] == "divergent",
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


def _upstream(release: PublishedRelease) -> tuple[float | None, int | None]:
    version = release.upstream_version
    return (None, None) if version is None else (version.changed_at, version.asset_id)


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

    def shipping(self, tx: Transaction, sha256: str) -> tuple[ReleaseRow, ...]:
        rows = pg_connection(tx).execute(
            f"SELECT {_RELEASE_COLUMNS} FROM app_releases WHERE asset_sha256=%s ORDER BY tag",
            (sha256,),
        ).fetchall()
        return tuple(_release(row) for row in rows)

    def claim(self, tx: Transaction, release: PublishedRelease, *,
              now: float) -> ReleaseRow | None:
        # 1. Insert if absent: a concurrent first insert of the tag waits on the unique index
        #    for the winner to commit, then inserts nothing.
        # 2. Otherwise lock the row. Under READ COMMITTED this statement sees the winner's
        #    committed row, so the previous row is never read before the lock is held.
        conn = pg_connection(tx)
        version = release_version(release.tag)
        changed_at, asset_id = _upstream(release)
        mirror_state = "discovered" if release.package is not None else "undeployable"
        if conn.execute(
            "INSERT INTO app_releases(tag,major,minor,patch,prerelease,is_prerelease,"
            "asset_url,asset_sha256,asset_size,base_tarball_url,base_tarball_sha256,"
            "base_tarball_size,mirror_state,upstream_changed_at,upstream_asset_id,"
            "discovered_at,updated_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(tag) DO NOTHING",
            (release.tag, version.major, version.minor, version.patch, version.prerelease,
             release.is_prerelease, *_facts(release.package), *_facts(release.os_image),
             mirror_state, changed_at, asset_id, now, now),
        ).rowcount == 1:
            return None
        row = conn.execute(
            f"SELECT {_RELEASE_COLUMNS} FROM app_releases WHERE tag=%s FOR UPDATE",
            (release.tag,),
        ).fetchone()
        return _release(row)

    def apply(self, tx: Transaction, release: PublishedRelease, *, divergent: bool,
              now: float) -> bool:
        # The guard is the WHERE: a stored NULL version takes any observation; a stored version
        # takes only a set one that is not older, compared as the row value (changed_at, id).
        # The SET's right-hand `mirror_state` is the row's value before this write.
        version = release_version(release.tag)
        changed_at, asset_id = _upstream(release)
        asset_url, asset_sha256, asset_size = _facts(release.package)
        base_url, base_sha256, base_size = _facts(release.os_image)
        return pg_connection(tx).execute(
            "UPDATE app_releases SET major=%(major)s,minor=%(minor)s,patch=%(patch)s,"
            "prerelease=%(prerelease)s,is_prerelease=%(is_prerelease)s,"
            "asset_url=%(asset_url)s,asset_sha256=%(asset_sha256)s,asset_size=%(asset_size)s,"
            "base_tarball_url=%(base_url)s,base_tarball_sha256=%(base_sha256)s,"
            "base_tarball_size=%(base_size)s,"
            "upstream_changed_at=%(changed_at)s,upstream_asset_id=%(asset_id)s,"
            "mirror_state=CASE WHEN %(divergent)s THEN 'divergent' "
            "WHEN mirror_state='divergent' THEN %(released_state)s ELSE mirror_state END,"
            "mirror_error=CASE WHEN %(divergent)s THEN 'asset_changed' "
            "WHEN mirror_state='divergent' THEN NULL ELSE mirror_error END,"
            "updated_at=%(now)s "
            "WHERE tag=%(tag)s AND (upstream_changed_at IS NULL OR "
            "(%(changed_at)s::double precision IS NOT NULL AND "
            "(upstream_changed_at, upstream_asset_id) "
            "<= (%(changed_at)s::double precision, %(asset_id)s::bigint))) "
            "RETURNING tag",
            {"tag": release.tag, "major": version.major, "minor": version.minor,
             "patch": version.patch, "prerelease": version.prerelease,
             "is_prerelease": release.is_prerelease, "asset_url": asset_url,
             "asset_sha256": asset_sha256, "asset_size": asset_size, "base_url": base_url,
             "base_sha256": base_sha256, "base_size": base_size, "changed_at": changed_at,
             "asset_id": asset_id, "divergent": divergent, "now": now,
             "released_state": "discovered" if release.package is not None else "undeployable"},
        ).fetchone() is not None

    def promoted_tag(self, tx: Transaction) -> str | None:
        promotion = self.promotion(tx)
        return None if promotion is None else promotion.tag

    def promotion(self, tx: Transaction) -> Promotion | None:
        row = pg_connection(tx).execute(
            "SELECT promoted_tag, promoted_by FROM app_release_policy WHERE singleton"
        ).fetchone()
        return None if row is None else Promotion(row["promoted_tag"], row["promoted_by"])

    def set_promoted(self, tx: Transaction, tag: str, *, by: Promoter) -> PromotionWrite:
        # Issue #23: "auto never moves an operator promotion" is enforced by the write, not by a
        # caller's earlier read (READ COMMITTED: an operator promote may commit in between).
        # 1. The first promotion inserts; ON CONFLICT is the guard (a concurrent first insert
        #    commits before this statement returns, and this one then does nothing).
        # 2. Otherwise lock the row, read what it holds now (the exact outgoing promotion), and
        #    UPDATE it only for an operator, or over the sync's own promotion.
        conn = pg_connection(tx)
        if conn.execute(
            "INSERT INTO app_release_policy(singleton, promoted_tag, promoted_by) "
            "VALUES(TRUE,%s,%s) ON CONFLICT(singleton) DO NOTHING",
            (tag, by),
        ).rowcount == 1:
            return PromotionWrite(moved=True, outgoing=None)
        row = conn.execute(
            "SELECT promoted_tag, promoted_by FROM app_release_policy WHERE singleton FOR UPDATE"
        ).fetchone()
        moved = conn.execute(
            "UPDATE app_release_policy SET promoted_tag=%(tag)s, promoted_by=%(by)s "
            "WHERE singleton AND (%(by)s = 'operator' OR promoted_by = 'auto')",
            {"tag": tag, "by": by},
        ).rowcount == 1
        return PromotionWrite(moved, Promotion(row["promoted_tag"], row["promoted_by"]))

    def last_good_tag(self, tx: Transaction) -> str | None:
        row = pg_connection(tx).execute(
            "SELECT last_good_tag FROM app_release_policy WHERE singleton"
        ).fetchone()
        return None if row is None else row["last_good_tag"]

    def set_last_good(self, tx: Transaction, tag: str) -> None:
        pg_connection(tx).execute(
            "UPDATE app_release_policy SET last_good_tag=%s WHERE singleton", (tag,)
        )

    def load_etag(self, tx: Transaction) -> StoredEtag | None:
        row = pg_connection(tx).execute(
            "SELECT etag, etag_stored_at FROM app_release_poll WHERE singleton"
        ).fetchone()
        if row is None or row["etag_stored_at"] is None:
            return None  # never stored by this code (029 cleared any older ETag)
        return StoredEtag(row["etag"], row["etag_stored_at"])

    def store_etag(self, tx: Transaction, etag: str | None, *, now: float) -> None:
        pg_connection(tx).execute(
            "INSERT INTO app_release_poll(singleton,etag,etag_stored_at) VALUES(TRUE,%s,%s) "
            "ON CONFLICT(singleton) DO UPDATE SET etag=EXCLUDED.etag,"
            "etag_stored_at=EXCLUDED.etag_stored_at",
            (etag, now),
        )

    def lock_auto_promotion(self, tx: Transaction) -> None:
        pg_connection(tx).execute("SELECT pg_advisory_xact_lock(%s)", (AUTO_PROMOTION_LOCK,))

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

    def known_good_tags(self, tx: Transaction) -> frozenset[str]:
        rows = pg_connection(tx).execute(
            "SELECT DISTINCT known_good_tag AS tag FROM devices "
            "WHERE retired_at IS NULL AND known_good_tag IS NOT NULL"
        ).fetchall()
        return frozenset(row["tag"] for row in rows)

    def named_tags(self, tx: Transaction, *, served_since: float) -> NamedTags:
        rows = pg_connection(tx).execute(
            " UNION ".join(
                f"SELECT DISTINCT '{role}' AS role, {column} AS tag FROM devices "
                f"WHERE retired_at IS NULL{extra} AND {column} IS NOT NULL"
                for role, column, extra in _NAMING_ROLES),
            {"served_since": served_since},
        ).fetchall()
        by_role: dict[str, set[str]] = {role: set() for role, _, _ in _NAMING_ROLES}
        for row in rows:
            by_role[row["role"]].add(row["tag"])
        return NamedTags(**{role: frozenset(tags) for role, tags in by_role.items()})

    def names_any(self, tx: Transaction, tags: Collection[str], *, served_since: float) -> bool:
        tags = sorted(tags)
        if not tags:
            return False
        row = pg_connection(tx).execute(
            "SELECT " + " OR ".join(
                f"EXISTS(SELECT 1 FROM devices WHERE retired_at IS NULL{extra} "
                f"AND {column} = ANY(%(tags)s))"
                for _, column, extra in _NAMING_ROLES) + " AS named",
            {"tags": tags, "served_since": served_since},
        ).fetchone()
        return bool(row["named"])

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
