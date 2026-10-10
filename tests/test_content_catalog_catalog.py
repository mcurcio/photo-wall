"""`ReleaseCatalog` against the real repositories and the `RecordingPublisher`.

PostgreSQL (the `registry` fixture; CI runs it). The desired set of node deployments is read in
`test_node_release_catalog.py` and `test_node_release_ingest.py`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from content_db import Reads, RecordingTransactions, store_etag
from fakes.publisher import PublishedCall, RecordingPublisher

from central.content_catalog.catalog import ReleaseCatalog, device_id_for_serial, sanitize_serial
from central.content_catalog.ports import StoredEtag
from central.infra.catalog_records import PgReleaseRecords
from central.kernel.job_types import Prefetch, SyncReleases
from central.kernel.ports import ContentCatalog
from contracts.equipment import equipment_device_id
from contracts.time import ManualClock

SERIAL = "10000000abcd0010"
DEVICE_ID = equipment_device_id("pi", SERIAL.encode())


@dataclass
class World:
    catalog: ReleaseCatalog
    transactions: RecordingTransactions
    publisher: RecordingPublisher
    clock: ManualClock
    reads: Reads


@pytest.fixture
def world(registry):
    def build(*, etag=None) -> World:
        clock = ManualClock(1000.0)
        if etag is not None:
            store_etag(RecordingTransactions(registry.db), etag, stored_at=1000.0)
        transactions = RecordingTransactions(registry.db)
        publisher = RecordingPublisher(clock)
        catalog = ReleaseCatalog(releases=PgReleaseRecords(), transactions=transactions,
                                 publisher=publisher, clock=clock)
        return World(catalog, transactions, publisher, clock,
                     Reads(RecordingTransactions(registry.db)))

    return build


def run(coro):
    return asyncio.run(coro)


def test_release_catalog_is_a_content_catalog(world):
    catalog: ContentCatalog = world().catalog  # static conformance
    assert callable(catalog.desired_assets) and callable(catalog.desired_tiers)


def test_sanitize_serial_is_the_ported_safe_charset():
    assert sanitize_serial(SERIAL) == SERIAL
    assert sanitize_serial("a:b_c.d-e") == "a:b_c.d-e"
    assert sanitize_serial("bad serial") is None
    assert sanitize_serial(None) is None
    assert sanitize_serial("x" * 129) is None


def test_the_device_id_is_derived_from_a_safe_serial_only():
    assert device_id_for_serial(SERIAL) == DEVICE_ID
    assert device_id_for_serial("ABC:DEF") is None  # safe charset, refused by the derivation


def test_desired_assets_of_an_empty_catalog_is_empty(world):
    assert run(world().catalog.desired_assets()) == frozenset()


def test_refresh_clears_the_etag_and_publishes_a_sync_retrying_a_terminal_outcome(world):
    # One transaction clears the ETag and publishes the sync, so the sync lists in full
    # (design §6.4: the operator's repair of a stale equal-version observation).
    w = world(etag='W/"e1"')
    run(w.catalog.refresh())
    (tx,) = w.transactions.begun
    assert tx.state == "committed"
    assert w.reads.stored_etag() == StoredEtag(None, w.clock.utc())
    assert w.publisher.calls == [
        PublishedCall(SyncReleases(), True, tx),
        PublishedCall(Prefetch(), False, None),  # publish_now owns its own transaction
    ]
