"""`SyncReleasesHandler` against `FakeReleaseOrigin` and the real repositories.

PostgreSQL (the `registry` fixture; CI runs it). The origin is the only fake besides the
`RecordingPublisher`, which records what the sync publishes. The node half of a sync (ingest,
refusals, the first-run selection) is `test_node_release_ingest.py`'s.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from content_db import Reads, RecordingTransactions, store_etag
from fakes.origin import FakeReleaseOrigin
from fakes.publisher import PublishedCall, RecordingPublisher

from central.assets.layout import CacheLayout
from central.assets.store import CacheStore
from central.content_catalog.catalog import ReleaseCatalog
from central.content_catalog.ports import NodeReleaseRecords
from central.content_catalog.sync import ETAG_MAX_AGE, SyncReleasesHandler
from central.infra.asset_records import PgAssetRecords
from central.infra.catalog_records import PgReleaseRecords
from central.infra.node_releases import PgNodeReleaseRecords
from central.infra.stored_assets import DiskStoredAssets
from central.kernel.handling import OriginUnavailable, handler_job_type
from central.kernel.job_types import Prefetch, SyncReleases
from central.kernel.ports import (
    NODE_RELEASE_INVALID,
    PublishedRelease,
    ReleaseListing,
    ReleaseOrigin,
)
from contracts.time import ManualClock

T1, T2 = "v0.0.1", "v0.0.3"
NOW = 10_000.0


def published(tag: str, *, pre: bool = False, node_problem: str | None = None) -> PublishedRelease:
    """`tag` as listed; with `node_problem`, a release whose attached node manifest was refused."""
    return PublishedRelease(tag, pre, node_problem=node_problem)


def listing(*releases: PublishedRelease, etag: str | None = "e1") -> ReleaseListing:
    return ReleaseListing(tuple(releases), etag, unchanged=False)


class RecordingOrigin(FakeReleaseOrigin):
    """Records every ETag it is sent. With `honour_etag`, it answers as GitHub does: unchanged
    (304) when sent the ETag of the listing it holds."""

    def __init__(self, listing_or_error, *, honour_etag: bool = False) -> None:
        super().__init__(listing_or_error, {})
        self.etags: list[str | None] = []
        self.honour_etag = honour_etag

    async def list_releases(self, *, etag: str | None) -> ReleaseListing:
        self.etags.append(etag)
        held = self.listing
        if (self.honour_etag and etag is not None and isinstance(held, ReleaseListing)
                and held.etag == etag):
            return ReleaseListing((), etag, unchanged=True)
        return await super().list_releases(etag=etag)


@dataclass
class World:
    handler: SyncReleasesHandler
    origin: ReleaseOrigin  # a `RecordingOrigin` unless the test passed its own
    assets: PgAssetRecords
    transactions: RecordingTransactions
    publisher: RecordingPublisher
    store: CacheStore
    reads: Reads
    db: object
    catalog: ReleaseCatalog  # the operator route's refresh runs through the same one
    clock: ManualClock
    stored: DiskStoredAssets


def make_world(registry, tmp_path):
    """The `world` fixture's builder, for another test module's own fixture."""
    def build(origin_listing, *, etag=None, origin: ReleaseOrigin | None = None,
              honour_etag: bool = False,
              node_releases: NodeReleaseRecords | None = None) -> World:
        clock = ManualClock(NOW)
        if etag is not None:
            store_etag(RecordingTransactions(registry.db), etag, stored_at=NOW)  # fresh: sent
        assets = PgAssetRecords(clock)
        store = CacheStore(CacheLayout(tmp_path))
        transactions = RecordingTransactions(registry.db)
        publisher = RecordingPublisher(clock)
        stored = DiskStoredAssets(records=assets, store=store)
        catalog = ReleaseCatalog(releases=PgReleaseRecords(), transactions=transactions,
                                 publisher=publisher, clock=clock)
        origin = origin or RecordingOrigin(origin_listing, honour_etag=honour_etag)
        handler = SyncReleasesHandler(origin=origin, releases=PgReleaseRecords(),
                                      transactions=transactions, publisher=publisher,
                                      clock=clock,
                                      node_releases=node_releases or PgNodeReleaseRecords(clock),
                                      readiness=stored)
        return World(handler, origin, assets, transactions, publisher, store,
                     Reads(RecordingTransactions(registry.db)), registry.db, catalog, clock,
                     stored)

    return build


@pytest.fixture
def world(registry, tmp_path):
    return make_world(registry, tmp_path)


def sync(w: World) -> None:
    asyncio.run(w.handler.handle(SyncReleases()))


def test_handler_serves_sync_releases(world):
    assert handler_job_type(world(listing()).handler) is SyncReleases


def test_a_release_without_a_node_manifest_writes_nothing_but_the_etag(world):
    # No V1 record: a release that attaches no node manifest is observed and left alone.
    w = world(listing(published(T1), published(T2, pre=True)), etag="e0")
    sync(w)
    assert w.origin.etags == ["e0"]
    assert w.reads.etag() == "e1"
    # Load etag, store etag, tail: each its own committed transaction.
    assert [tx.state for tx in w.transactions.begun] == ["committed"] * 3
    assert w.publisher.calls == [PublishedCall(Prefetch(), False, w.transactions.begun[-1])]
    with w.db.transaction() as conn:
        assert conn.execute("SELECT kind FROM assets").fetchall() == []
        assert conn.execute("SELECT tag FROM node_release_observations").fetchall() == []


def test_unchanged_listing_runs_only_the_tail(world):
    w = world(ReleaseListing((), "e1", unchanged=True), etag="e1")
    sync(w)
    assert w.reads.etag() == "e1"
    assert len(w.transactions.begun) == 2  # load etag + tail
    assert w.publisher.calls == [PublishedCall(Prefetch(), False, w.transactions.begun[-1])]


def test_origin_failure_propagates_and_writes_nothing(world):
    w = world(OriginUnavailable("github_down"))
    with pytest.raises(OriginUnavailable):
        sync(w)
    assert len(w.transactions.begun) == 1  # only the etag read
    assert w.reads.etag() is None and w.publisher.calls == []


def test_a_failing_release_transaction_keeps_the_etag_so_the_next_sync_retries(world):
    class Boom(PgNodeReleaseRecords):
        def record_problem(self, tx, release, problem, *, now):
            raise RuntimeError("db down")

    w = world(listing(published(T1, node_problem=NODE_RELEASE_INVALID)), etag="e0",
              node_releases=Boom(ManualClock(NOW)))
    with pytest.raises(RuntimeError):
        sync(w)
    assert w.reads.etag() == "e0"
    assert w.transactions.begun[-1].state == "rolled_back"


def test_a_stored_etag_is_sent_only_within_its_hour(world):
    # Past ETAG_MAX_AGE the sync lists in full, which repairs a stale equal-version observation.
    w = world(listing(etag="e2"), etag="e1", honour_etag=True)
    sync(w)
    w.clock.advance(ETAG_MAX_AGE.total_seconds() + 1)
    sync(w)
    assert w.origin.etags == ["e1", None]
    assert w.reads.etag() == "e2"
