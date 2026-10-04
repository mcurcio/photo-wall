"""The one composition helper for OS images and Player `.deb`s (design §10.5), shared by both roots.

`create_app` (the Central process) calls `build_content_services`: the catalog, the read-through
reader, the library thumbnails' resolver and their own reader, the pod probe and the process's one
`OutcomeFeed`. It binds no handlers. Worker boot (`media/worker.py`) calls `build_job_runtime`:
every CATALOG handler behind one `JobRuntime`, the one worker kind, with the library thumbnail
origin INJECTED (Central imports no library client); and `build_library_thumbnails` for its
prefetch. The worker's publisher has no feed, because a worker never waits on a handle.

Both build their adapters over the same `Database`, so the two roots cannot drift in how a port is
satisfied. Nothing here opens a connection; the returned objects connect when used.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Final

from central.assets.handlers import (
    FetchLibraryThumbnailHandler,
    FetchOsImageHandler,
    FetchPackageHandler,
    FetchPlayerPayloadHandler,
    FetchSealedEnvironmentHandler,
    PrefetchHandler,
)
from central.assets.layout import CacheLayout
from central.assets.library import LibraryThumbnails
from central.assets.production import AssetProduction
from central.assets.reader import AssetReader, WaiterSlots
from central.assets.store import CacheStore
from central.content_catalog.catalog import ReleaseCatalog, in_transaction
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
from central.kernel.ports import ThumbnailOrigin
from central.kernel.publishing import Publisher
from central.origins.github import GitHubReleaseOrigin
from contracts.time import Clock

WAITER_SLOTS: Final = 32  # §2: the per-pod waiter cap for OS images and `.deb`s
# Console DDD §40: cold preview tiles hold at most four HTTP waiters for two seconds, in slots of
# their own, so 24 tiles never take the waiter slots of the console's reads or a Player's base
# fetch. These slots bound HTTP waiters, not queue occupancy: on the shared FETCH queue a tile's
# job sits below every boot or package fetch by priority (`FetchLibraryThumbnail`), and a running
# tile holds a FETCH slot for at most its client's metadata budget.
THUMBNAIL_WAITER_SLOTS: Final = 4
THUMBNAIL_WAIT: Final = timedelta(seconds=2)
WORKER_CONCURRENCY: Final[Mapping[QueueName, int]] = MappingProxyType(
    {QueueName.FETCH: 2, QueueName.UPKEEP: 2})


@dataclass(frozen=True, slots=True)
class ContentServices:
    """What the Central process needs to serve and operate content."""

    catalog: ReleaseCatalog
    reader: AssetReader
    probe: PodProbe
    feed: OutcomeFeed | None  # started and stopped by the app's lifespan
    thumbnails: LibraryThumbnails | None = None
    thumbnail_reader: AssetReader | None = None  # its own slots and short wait (§40)
    publisher: Publisher | None = None  # jobs published inside another writer's transaction


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


def _thumbnails(core: _Core, servable: Callable[[str], bool]) -> LibraryThumbnails:
    return LibraryThumbnails(servable=servable, records=core.assets,
                             transactions=core.transactions, publisher=core.publisher)


def _nothing_servable(_asset_id: str) -> bool:
    return False


def build_content_services(db: Database, clock: Clock, *, cache_root: Path,
                           servable_thumbnail: Callable[[str], bool] = _nothing_servable,
                           ) -> ContentServices:
    """The Central process's content services; the caller starts and stops `feed`.

    `servable_thumbnail` reads current data (`MediaRepository.servable_thumbnail`); without it
    no thumbnail is servable, so the route answers 404 and queues nothing.
    """
    core = _core(db, clock, cache_root=cache_root, feed_wanted=True)
    reader = AssetReader(store=core.store, records=core.assets, transactions=core.transactions,
                         publisher=core.publisher, slots=WaiterSlots(WAITER_SLOTS), clock=clock)
    thumbnail_reader = AssetReader(
        store=core.store, records=core.assets, transactions=core.transactions,
        publisher=core.publisher, slots=WaiterSlots(THUMBNAIL_WAITER_SLOTS), clock=clock,
        wait_timeout=THUMBNAIL_WAIT)
    return ContentServices(catalog=core.catalog, reader=reader, probe=PodProbe(db.healthy),
                           feed=core.feed, thumbnails=_thumbnails(core, servable_thumbnail),
                           thumbnail_reader=thumbnail_reader, publisher=core.publisher)


def build_library_thumbnails(db: Database, clock: Clock, *, cache_root: Path) -> LibraryThumbnails:
    """The worker's thumbnail prefetch: a publisher with no feed (it never waits), resolving
    nothing (the worker serves no route)."""
    return _thumbnails(_core(db, clock, cache_root=cache_root, feed_wanted=False),
                       _nothing_servable)


def build_job_runtime(db: Database, clock: Clock, *, cache_root: Path,
                      env: Mapping[str, str], thumbnails: ThumbnailOrigin) -> JobRuntime:
    """Every CATALOG handler behind one `JobRuntime`; its boot checks run here.

    `thumbnails` is the media worker's library origin (R22): the thumbnail handler is Central's,
    its library half is injected, so Central imports no library client.

    The runtime owns the queue-ops pool (`QueueAdmin`, opened on first use) and closes it when
    `run()` ends, so no caller has a pool to remember.
    """
    core = _core(db, clock, cache_root=cache_root, feed_wanted=False)
    origin = GitHubReleaseOrigin.from_env(env)
    production = AssetProduction(store=core.store, records=core.assets,
                                 transactions=core.transactions)
    releases = PgReleaseRecords()

    async def payload_expected_abi(sha256: str) -> str | None:
        return await in_transaction(
            core.transactions,
            lambda tx: releases.payload_abi_for(tx, sha256, now=clock.utc()))
    admin = QueueAdmin(db.dsn)
    handlers = (
        SyncReleasesHandler(origin=origin, releases=PgReleaseRecords(), devices=PgDeviceRecords(),
                            assets=core.assets, transactions=core.transactions,
                            publisher=core.publisher, catalog=core.catalog, clock=clock,
                            include_prereleases=origin.include_prereleases),
        FetchOsImageHandler(production=production, origin=origin, store=core.store),
        FetchPackageHandler(production=production, origin=origin),
        FetchSealedEnvironmentHandler(production=production, origin=origin),
        FetchPlayerPayloadHandler(production=production, origin=origin,
                                  expected_abi=payload_expected_abi),
        FetchLibraryThumbnailHandler(production=production, origin=thumbnails),
        PrefetchHandler(catalog=core.catalog, records=core.assets, store=core.store,
                        transactions=core.transactions, publisher=core.publisher),
        RescueStalledJobsHandler(admin),
        PurgeFinishedJobsHandler(admin, transactions=core.transactions, outcomes=core.outcomes,
                                 clock=clock),
    )
    return JobRuntime(db.dsn, handlers, WORKER_CONCURRENCY, transactions=core.transactions,
                      assets=core.assets, clock=clock, owned=(admin,))
