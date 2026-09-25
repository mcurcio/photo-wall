"""`AssetProduction.produce` on a real `CacheStore` and the real Asset records (PostgreSQL)."""

from __future__ import annotations

import asyncio

import pytest
from content_db import Reads, RecordingTransactions, facts_of, put_file, sha

from central.assets.layout import TEMP_PREFIX, CacheLayout
from central.assets.production import AssetProduction
from central.assets.store import CacheStore
from central.infra.asset_records import PgAssetRecords
from central.kernel.assets import (
    AssetKey,
    AssetKind,
    AssetReady,
    AssetReference,
    OriginLocator,
)
from central.kernel.handling import TerminalFailure, TransientFailure
from central.kernel.job_types import FetchOsImage, FetchPackage
from central.kernel.publishing import ASSET_NOT_RECORDED
from contracts.time import ManualClock

TAG = "v1.2.3"
GOOD = b"the produced bytes " * 50
OTHER = b"some other bytes " * 50
facts = facts_of

DEB_SHA = facts(GOOD).sha256
TARBALL = sha("base tarball v1")
OS_JOB = FetchOsImage(tarball_sha256=TARBALL)
DEB_JOB = FetchPackage(sha256=DEB_SHA)
OS_KEY = AssetKey(AssetKind.OS_IMAGE, TARBALL)
DEB_KEY = AssetKey(AssetKind.PLAYER_DEB, DEB_SHA)
URL = "https://example.test/a"


class Writer:
    """A `WriteFn` that writes fixed bytes (or raises) and records every call."""

    def __init__(self, data: bytes | BaseException = GOOD) -> None:
        self.data = data
        self.calls: list[tuple[object, object]] = []

    async def __call__(self, temp, locator) -> None:
        self.calls.append((temp, locator))
        assert not temp.exists()
        if isinstance(self.data, BaseException):
            temp.write_bytes(b"partial")
            raise self.data
        temp.write_bytes(self.data)


class World:
    def __init__(self, registry, tmp_path) -> None:
        self.store = CacheStore(CacheLayout(tmp_path))
        self.records = PgAssetRecords(ManualClock(1000.0))
        self.transactions = RecordingTransactions(registry.db)
        self.reads = Reads(RecordingTransactions(registry.db))
        self.production = AssetProduction(store=self.store, records=self.records,
                                           transactions=self.transactions)

    def reference(self, key, owner=TAG, expected: AssetReady | None = None) -> None:
        """A reference whose locator names the key, as the schema requires."""
        ref = AssetReference(owner, OriginLocator(URL, sha256=key.identity, size=None),
                             expected_size=expected.size if expected else None,
                             expected_sha256=expected.sha256 if expected else None)
        with self.reads.transactions.begin() as tx:
            self.records.reference(tx, key, ref)

    def produced(self, key, value: AssetReady) -> None:
        with self.reads.transactions.begin() as tx:
            self.records.record_produced(tx, key, value)

    def put(self, key, data: bytes) -> None:
        put_file(self.store, key, data)

    def final(self, key) -> bytes | None:
        path = self.store.layout.path(key)
        return path.read_bytes() if path.exists() else None

    def temps(self, kind: AssetKind) -> list[str]:
        directory = self.store.layout.directory(kind)
        if not directory.exists():
            return []
        return [p.name for p in directory.iterdir() if p.name.startswith(TEMP_PREFIX)]

    def produce(self, job, writer):
        return asyncio.run(self.production.produce(job, writer))


@pytest.fixture
def world(registry, tmp_path):
    return World(registry, tmp_path)


def test_unknown_asset_is_transient_asset_not_recorded_and_writes_nothing(world):
    # The publisher's reading of the same condition (PB7): one reason, transient, never sticky.
    writer = Writer()
    with pytest.raises(TransientFailure) as raised:
        world.produce(OS_JOB, writer)
    assert raised.value.reason == ASSET_NOT_RECORDED
    assert raised.value.retry_after is None
    assert writer.calls == []
    assert all(tx.state == "committed" for tx in world.transactions.begun)


def test_absent_file_is_written_measured_installed_and_no_record_written(world):
    world.reference(OS_KEY)
    writer = Writer()
    assert world.produce(OS_JOB, writer) == facts(GOOD)
    assert len(writer.calls) == 1
    assert world.final(OS_KEY) == GOOD
    assert world.temps(AssetKind.OS_IMAGE) == []
    assert world.reads.asset(OS_KEY).produced is None  # the runtime records it, not produce


def test_present_file_equal_to_produced_returns_without_writing(world):
    world.reference(OS_KEY)
    world.produced(OS_KEY, facts(GOOD))
    world.put(OS_KEY, GOOD)
    writer = Writer()
    assert world.produce(OS_JOB, writer) == facts(GOOD)
    assert writer.calls == []


def test_present_deb_equal_to_newest_expectation_returns_measured_facts(world):
    world.reference(DEB_KEY, expected=facts(GOOD))
    world.put(DEB_KEY, GOOD)
    writer = Writer()
    assert world.produce(DEB_JOB, writer) == facts(GOOD)
    assert writer.calls == []


def test_present_os_image_without_produced_facts_always_reproduces(world):
    world.reference(OS_KEY)
    world.put(OS_KEY, OTHER)
    writer = Writer()
    assert world.produce(OS_JOB, writer) == facts(GOOD)
    assert len(writer.calls) == 1
    assert world.final(OS_KEY) == GOOD


def test_present_file_differing_from_produced_is_discarded_and_reproduced(world):
    world.reference(OS_KEY)
    world.produced(OS_KEY, facts(GOOD))
    world.put(OS_KEY, OTHER)  # corrupted on disk
    writer = Writer()
    assert world.produce(OS_JOB, writer) == facts(GOOD)
    assert world.final(OS_KEY) == GOOD


def test_present_deb_differing_from_expectation_is_discarded_before_writing(world):
    world.reference(DEB_KEY, expected=facts(GOOD))
    world.put(DEB_KEY, OTHER)
    seen: list[bool] = []

    async def write(temp, locator):
        seen.append(world.store.layout.path(DEB_KEY).exists())
        temp.write_bytes(GOOD)

    assert asyncio.run(world.production.produce(DEB_JOB, write)) == facts(GOOD)
    assert seen == [False]


def test_empty_or_symlinked_final_counts_as_absent(world, tmp_path):
    world.reference(OS_KEY)
    world.produced(OS_KEY, facts(GOOD))
    world.put(OS_KEY, b"")
    assert world.produce(OS_JOB, Writer()) == facts(GOOD)
    path = world.store.layout.path(OS_KEY)
    path.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(GOOD)
    path.symlink_to(outside)
    writer = Writer()
    assert world.produce(OS_JOB, writer) == facts(GOOD)
    assert len(writer.calls) == 1
    assert not path.is_symlink()


def test_reproduction_differing_from_produced_is_not_reproducible(world):
    world.reference(OS_KEY)
    world.produced(OS_KEY, facts(GOOD))
    with pytest.raises(TerminalFailure) as raised:
        world.produce(OS_JOB, Writer(OTHER))
    assert raised.value.reason == "not_reproducible"
    assert world.final(OS_KEY) is None
    assert world.temps(AssetKind.OS_IMAGE) == []


def test_production_differing_from_a_reference_expectation_is_a_digest_mismatch(world):
    world.reference(DEB_KEY, expected=facts(GOOD))
    with pytest.raises(TerminalFailure) as raised:
        world.produce(DEB_JOB, Writer(OTHER))
    assert raised.value.reason == "digest_mismatch"
    assert world.final(DEB_KEY) is None
    assert world.temps(AssetKind.PLAYER_DEB) == []


def test_every_reference_expectation_must_hold(world):
    world.reference(DEB_KEY, owner="v1.0.0", expected=facts(GOOD))
    world.reference(DEB_KEY, owner="v1.1.0",
                    expected=AssetReady(size=len(GOOD) + 1, sha256=facts(GOOD).sha256))
    with pytest.raises(TerminalFailure) as raised:
        world.produce(DEB_JOB, Writer(GOOD))
    assert raised.value.reason == "digest_mismatch"


def test_a_failing_write_leaves_no_temp_and_propagates(world):
    world.reference(OS_KEY)
    with pytest.raises(OSError, match="disk full"):
        world.produce(OS_JOB, Writer(OSError("disk full")))
    assert world.temps(AssetKind.OS_IMAGE) == []
    assert world.final(OS_KEY) is None


def test_cancellation_mid_write_discards_the_temp(world):
    world.reference(OS_KEY)
    started = asyncio.Event()

    async def write(temp, locator):
        temp.write_bytes(b"partial")
        started.set()
        await asyncio.sleep(3600)

    async def run():
        task = asyncio.create_task(world.production.produce(OS_JOB, write))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert world.temps(AssetKind.OS_IMAGE) == []
    assert world.final(OS_KEY) is None


def test_a_recut_is_a_new_key_and_is_produced(world):
    # A re-cut names another tarball, so it is another asset: the old key's facts are never
    # cleared, and the new build is recorded under its own key (no `not_reproducible`).
    world.reference(OS_KEY)
    world.produced(OS_KEY, facts(GOOD))
    recut = AssetKey(AssetKind.OS_IMAGE, sha("base tarball v1, re-cut"))
    world.reference(recut)
    assert world.produce(FetchOsImage(tarball_sha256=recut.identity), Writer(OTHER)) == facts(
        OTHER)
    assert world.final(recut) == OTHER
    assert world.reads.asset(OS_KEY).produced == facts(GOOD)
