"""Postgres `ReleaseRecords` over the existing tables.

`app_releases` (016 + 018's base_* columns + 029's upstream version), `app_release_policy` (+
023's last_good_tag and 027's promoted_by) and `app_release_poll` (+ 029's etag_stored_at). The
SQL is ported from `central/app_releases.py` and `central/app_release_service.py`. Each method
runs in the caller's transaction; a fake transaction is a `TypeError` (`pg_connection`).
"""

from __future__ import annotations

from typing import Any

from central.content_catalog.ports import (
    FleetDesiredAssets,
    Promotion,
    ReleaseRow,
    StoredEtag,
)
from central.infra.node_releases import deployment_jobs, wanted_deployments
from central.infra.transactions import pg_connection
from central.infra.upstream_guard import not_older
from central.kernel.assets import AssetKind, OriginLocator
from central.kernel.job_types import AssetJob
from central.kernel.jobs import asset_key
from central.kernel.ports import PublishedRelease
from central.kernel.transactions import Transaction
from central.kernel.types import release_version

_RELEASE_COLUMNS = (
    "tag, is_prerelease, asset_url, asset_sha256, asset_size, "
    "base_tarball_url, base_tarball_sha256, base_tarball_size"
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

    def fleet_desired_assets(self, tx: Transaction) -> FleetDesiredAssets:
        """The wanted node deployments' files (`wanted_deployments`): the selected and the
        previous one are fetched first; the window's (the newest stable releases) in the
        background, newest release first."""
        conn = pg_connection(tx)
        wanted = wanted_deployments(conn)
        jobs = deployment_jobs(conn, wanted.deployments)
        node: set[AssetJob] = set()
        for deployment in (wanted.selected, wanted.previous):
            if deployment is not None:
                node.update(jobs.get(deployment, ()))
        window: list[AssetJob] = []
        for _, deployment in wanted.window:
            for job in sorted(jobs.get(deployment, ()), key=lambda job: str(asset_key(job))):
                if job not in window:
                    window.append(job)
        keys = [asset_key(job) for job in node]
        return FleetDesiredAssets(
            frozenset(key.identity for key in keys if key.kind == AssetKind.OS_IMAGE),
            frozenset(key.identity for key in keys if key.kind == AssetKind.SEALED_ENVIRONMENT),
            tuple(window))

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
        if conn.execute(
            "INSERT INTO app_releases(tag,major,minor,patch,prerelease,is_prerelease,"
            "base_tarball_url,base_tarball_sha256,base_tarball_size,upstream_changed_at,"
            "upstream_asset_id,discovered_at,updated_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(tag) DO NOTHING",
            (release.tag, version.major, version.minor, version.patch, version.prerelease,
             release.is_prerelease, *_facts(release.os_image), changed_at, asset_id, now, now),
        ).rowcount == 1:
            return None
        row = conn.execute(
            f"SELECT {_RELEASE_COLUMNS} FROM app_releases WHERE tag=%s FOR UPDATE",
            (release.tag,),
        ).fetchone()
        return _release(row)

    def apply(self, tx: Transaction, release: PublishedRelease, *, now: float) -> bool:
        # The guard is the WHERE (`not_older`, shared with the node observations).
        version = release_version(release.tag)
        changed_at, asset_id = _upstream(release)
        base_url, base_sha256, base_size = _facts(release.os_image)
        return pg_connection(tx).execute(
            "UPDATE app_releases SET major=%(major)s,minor=%(minor)s,patch=%(patch)s,"
            "prerelease=%(prerelease)s,is_prerelease=%(is_prerelease)s,"
            "base_tarball_url=%(base_url)s,base_tarball_sha256=%(base_sha256)s,"
            "base_tarball_size=%(base_size)s,"
            "upstream_changed_at=%(changed_at)s,upstream_asset_id=%(asset_id)s,"
            "updated_at=%(now)s "
            f"WHERE tag=%(tag)s AND {not_older('app_releases')} "
            "RETURNING tag",
            {"tag": release.tag, "major": version.major, "minor": version.minor,
             "patch": version.patch, "prerelease": version.prerelease,
             "is_prerelease": release.is_prerelease, "base_url": base_url,
             "base_sha256": base_sha256, "base_size": base_size, "changed_at": changed_at,
             "asset_id": asset_id, "now": now},
        ).fetchone() is not None

    def promoted_tag(self, tx: Transaction) -> str | None:
        promotion = self.promotion(tx)
        return None if promotion is None else promotion.tag

    def promotion(self, tx: Transaction) -> Promotion | None:
        row = pg_connection(tx).execute(
            "SELECT promoted_tag, promoted_by FROM app_release_policy WHERE singleton"
        ).fetchone()
        return None if row is None else Promotion(row["promoted_tag"], row["promoted_by"])

    def set_promoted(self, tx: Transaction, tag: str) -> Promotion | None:
        # Lock the policy row (a first promotion inserts it), read what it holds now (the exact
        # outgoing promotion), then write the operator's.
        conn = pg_connection(tx)
        if conn.execute(
            "INSERT INTO app_release_policy(singleton, promoted_tag, promoted_by) "
            "VALUES(TRUE,%s,'operator') ON CONFLICT(singleton) DO NOTHING",
            (tag,),
        ).rowcount == 1:
            return None
        row = conn.execute(
            "SELECT promoted_tag, promoted_by FROM app_release_policy WHERE singleton FOR UPDATE"
        ).fetchone()
        conn.execute(
            "UPDATE app_release_policy SET promoted_tag=%s, promoted_by='operator' "
            "WHERE singleton", (tag,))
        return Promotion(row["promoted_tag"], row["promoted_by"])

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
