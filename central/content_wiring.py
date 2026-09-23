"""The one composition helper for OS images and Player `.deb`s (design §10.5), shared by both roots.

`create_app` (the Central process) calls `build_content_services`: the catalog, the read-through
reader, the pod probe and the process's one `OutcomeFeed`. It binds no handlers. Worker boot
(`media/worker.py`) calls `build_job_runtime`: every CATALOG handler behind one `JobRuntime`, the
one worker kind. The worker's publisher has no feed, because a worker never waits on a handle.

Both build their adapters over the same `Database`, so the two roots cannot drift in how a port is
satisfied. Nothing here opens a connection; the returned objects connect when used.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

from central.assets.handlers import FetchOsImageHandler, FetchPackageHandler, PrefetchHandler
from central.assets.layout import CacheLayout
from central.assets.production import AssetProduction
from central.assets.reader import AssetReader, WaiterSlots
from central.assets.store import CacheStore
from central.content_catalog.catalog import ReleaseCatalog
from central.content_catalog.sync import SyncReleasesHandler
from central.db import Database
from central.health.probe import PodProbe
from central.infra.asset_records import PgAssetRecords
from central.infra.catalog_records import PgDeviceRecords, PgReleaseRecords
from central.infra.outcome_feed import OutcomeFeed
from central.infra.outcomes import JobOutcomes
from central.infra.publisher import ProcrastinatePublisher
from central.infra.queue_ops import PurgeFinishedJobsHandler, QueueAdmin, RescueStalledJobsHandler
from central.infra.runtime import JobRuntime
from central.infra.stored_assets import DiskStoredAssets
from central.infra.transactions import PgTransactions
from central.kernel.jobs import QueueName
from central.origins.github import GitHubReleaseOrigin
from contracts.time import Clock

WAITER_SLOTS: Final = 32  # §2: the per-pod waiter cap for OS images and `.deb`s
WORKER_CONCURRENCY: Final[Mapping[QueueName, int]] = MappingProxyType(
    {QueueName.FETCH: 2, QueueName.UPKEEP: 2})


@dataclass(frozen=True, slots=True)
class ContentServices:
    """What the Central process needs to serve and operate content."""

    catalog: ReleaseCatalog
    reader: AssetReader
    probe: PodProbe
    feed: OutcomeFeed | None  # started and stopped by the app's lifespan


@dataclass(frozen=True, slots=True)
class _Core:
    transactions: PgTransactions
    assets: PgAssetRecords
    outcomes: JobOutcomes
    publisher: ProcrastinatePublisher
    catalog: ReleaseCatalog
    store: CacheStore
    feed: OutcomeFeed | None


def _core(db: Database, clock: Clock, *, cache_root: Path, feed_wanted: bool) -> _Core:
    transactions = PgTransactions(db)
    assets = PgAssetRecords(clock)
    outcomes = JobOutcomes()
    feed = (OutcomeFeed(db.dsn, transactions=transactions, outcomes=outcomes)
            if feed_wanted else None)
    publisher = ProcrastinatePublisher(db.dsn, transactions=transactions, outcomes=outcomes,
                                       assets=assets, clock=clock, feed=feed)
    store = CacheStore(CacheLayout(cache_root))
    catalog = ReleaseCatalog(releases=PgReleaseRecords(), devices=PgDeviceRecords(),
                             stored=DiskStoredAssets(records=assets, store=store),
                             transactions=transactions, publisher=publisher, clock=clock)
    return _Core(transactions, assets, outcomes, publisher, catalog, store, feed)


def build_content_services(db: Database, clock: Clock, *, cache_root: Path) -> ContentServices:
    """The Central process's content services; the caller starts and stops `feed`."""
    core = _core(db, clock, cache_root=cache_root, feed_wanted=True)
    reader = AssetReader(store=core.store, records=core.assets, transactions=core.transactions,
                         publisher=core.publisher, slots=WaiterSlots(WAITER_SLOTS), clock=clock)
    return ContentServices(catalog=core.catalog, reader=reader, probe=PodProbe(db.healthy),
                           feed=core.feed)


def build_job_runtime(db: Database, clock: Clock, *, cache_root: Path, env: Mapping[str, str],
                      admin: QueueAdmin | None = None) -> JobRuntime:
    """Every CATALOG handler behind one `JobRuntime`; its boot checks run here.

    `admin` is the queue-ops pool. The worker passes its own so it can `aclose()` it at shutdown
    (lane A errata); left None, one is built over `db.dsn` and closes with the process.
    """
    core = _core(db, clock, cache_root=cache_root, feed_wanted=False)
    origin = GitHubReleaseOrigin.from_env(env)
    production = AssetProduction(store=core.store, records=core.assets,
                                 transactions=core.transactions)
    admin = admin if admin is not None else QueueAdmin(db.dsn)
    handlers = (
        SyncReleasesHandler(origin=origin, releases=PgReleaseRecords(), devices=PgDeviceRecords(),
                            assets=core.assets, transactions=core.transactions,
                            publisher=core.publisher, catalog=core.catalog, clock=clock,
                            include_prereleases=origin.include_prereleases),
        FetchOsImageHandler(production=production, origin=origin, store=core.store),
        FetchPackageHandler(production=production, origin=origin),
        PrefetchHandler(catalog=core.catalog, records=core.assets, store=core.store,
                        transactions=core.transactions, publisher=core.publisher),
        RescueStalledJobsHandler(admin),
        PurgeFinishedJobsHandler(admin, transactions=core.transactions, outcomes=core.outcomes,
                                 clock=clock),
    )
    return JobRuntime(db.dsn, handlers, WORKER_CONCURRENCY, transactions=core.transactions,
                      assets=core.assets, clock=clock)
