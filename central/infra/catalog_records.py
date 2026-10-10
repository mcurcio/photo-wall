"""Postgres `ReleaseRecords`: the release listing's ETag (`app_release_poll`, + 029's
etag_stored_at) and the wanted node deployments' files. Each method runs in the caller's
transaction; a fake transaction is a `TypeError` (`pg_connection`).
"""

from __future__ import annotations

from central.content_catalog.ports import FleetDesiredAssets, StoredEtag
from central.infra.node_releases import deployment_jobs, wanted_deployments
from central.infra.transactions import pg_connection
from central.kernel.assets import AssetKind
from central.kernel.job_types import AssetJob
from central.kernel.jobs import asset_key
from central.kernel.transactions import Transaction


class PgReleaseRecords:
    """Implements `ReleaseRecords`."""

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
