"""Postgres `ReleaseRecords`: the release listing's ETag (the `singletons` document
'release_sync', decision 0034's migration 118) and the wanted node deployments' files. Each method
runs in the caller's transaction; a fake transaction is a `TypeError` (`pg_connection`).
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict

from central.content_catalog.ports import FleetDesiredAssets, StoredEtag
from central.db import DatabaseTransactionClock
from central.infra.node_releases import deployment_jobs, wanted_deployments
from central.infra.singletons import Singleton
from central.infra.transactions import pg_connection
from central.kernel.assets import AssetKind
from central.kernel.job_types import AssetJob
from central.kernel.jobs import asset_key
from central.kernel.transactions import Transaction


class _ReleaseSyncDocument(BaseModel):
    """The listing's last ETag (None: list in full) and when it was stored (the sync's `now`)."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    etag: str | None
    etag_stored_at: float | None


RELEASE_SYNC: Final[Singleton[_ReleaseSyncDocument]] = Singleton("release_sync", _ReleaseSyncDocument)
_TIMES: Final = DatabaseTransactionClock()


class PgReleaseRecords:
    """Implements `ReleaseRecords`."""

    def fleet_desired_assets(self, tx: Transaction) -> FleetDesiredAssets:
        """The wanted node deployments' files (`wanted_deployments`): every Pi's version and
        previous one are fetched first; the window's (the newest stable releases) in the
        background, newest release first."""
        conn = pg_connection(tx)
        wanted = wanted_deployments(conn)
        jobs = deployment_jobs(conn, wanted.deployments)
        node: set[AssetJob] = set()
        for deployment in wanted.versions | wanted.previous:
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
        stored = RELEASE_SYNC.read_in(pg_connection(tx))
        if stored is None or stored.value.etag_stored_at is None:
            return None  # never stored by this code (029 cleared any older ETag)
        return StoredEtag(stored.value.etag, stored.value.etag_stored_at)

    def store_etag(self, tx: Transaction, etag: str | None, *, now: float) -> None:
        RELEASE_SYNC.write_in(pg_connection(tx), _ReleaseSyncDocument(etag=etag, etag_stored_at=now),
                              by="central", times=_TIMES)
