"""Job handlers of the assets domain: fetch an OS image, fetch a Player `.deb`, prefetch.

Job types are imported at runtime, never under `TYPE_CHECKING`: `handler_job_type` reads each
`handle` method's annotations with `typing.get_type_hints`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Final

from central.assets.os_image import extract_squashfs
from central.assets.production import AssetProduction
from central.assets.store import CacheStore
from central.kernel.assets import Asset, AssetReady
from central.kernel.handling import OriginRejected, OriginUnavailable, TerminalFailure
from central.kernel.job_types import AssetJob, FetchOsImage, FetchPackage, Prefetch
from central.kernel.jobs import asset_key, job_keys
from central.kernel.ports import AssetRecords, ContentCatalog, ReleaseOrigin
from central.kernel.publishing import Publisher
from central.kernel.transactions import Transactions
from contracts.release import MAX_ROOTFS_BYTES

MAX_TARBALL_BYTES: Final = MAX_ROOTFS_BYTES
MAX_PACKAGE_BYTES: Final = 1024**3


class FetchOsImageHandler:
    """Download the newest reference's base tarball, then extract the squashfs off the loop."""

    def __init__(self, *, production: AssetProduction, origin: ReleaseOrigin,
                 store: CacheStore) -> None:
        self._production = production
        self._origin = origin
        self._store = store

    async def handle(self, job: FetchOsImage) -> AssetReady:
        return await self._production.produce(job, self._write)

    async def _write(self, temp: Path, asset: Asset) -> None:
        locator = asset.references[0].locator
        tarball = await asyncio.to_thread(self._store.temp_path, asset.key)
        try:
            await self._origin.download(locator, tarball,
                                        max_bytes=locator.size or MAX_TARBALL_BYTES)
            # gzip + sha256 over up to 1 GiB: never on the event loop (it would starve the
            # worker's heartbeat; the legacy fetch ran it inline).
            await asyncio.to_thread(extract_squashfs, tarball, temp)
        finally:
            self._store.discard(tarball)


class FetchPackageHandler:
    """Download the `.deb` from its references, newest first; one file for every tag that ships it."""

    def __init__(self, *, production: AssetProduction, origin: ReleaseOrigin) -> None:
        self._production = production
        self._origin = origin

    async def handle(self, job: FetchPackage) -> AssetReady:
        return await self._production.produce(job, self._write)

    async def _write(self, temp: Path, asset: Asset) -> None:
        unavailable: OriginUnavailable | None = None
        for ref in asset.references:
            locator = ref.locator
            try:
                # `download` removes `temp` on any failure, so the next reference can reuse it.
                await self._origin.download(locator, temp,
                                            max_bytes=locator.size or MAX_PACKAGE_BYTES)
                return
            except OriginRejected:
                continue
            except OriginUnavailable as error:
                unavailable = error
        if unavailable is not None:
            raise unavailable
        raise TerminalFailure("all_references_rejected")


class PrefetchHandler:
    """Publish the fetch job of every desired asset that is recorded but not on disk.

    It never waits for those jobs and never sets `retry_terminal`: a terminal outcome stays
    terminal until a request or an operator asks again.
    """

    def __init__(self, *, catalog: ContentCatalog, records: AssetRecords, store: CacheStore,
                 transactions: Transactions, publisher: Publisher) -> None:
        self._catalog = catalog
        self._records = records
        self._store = store
        self._transactions = transactions
        self._publisher = publisher

    async def handle(self, job: Prefetch) -> None:
        desired = await self._catalog.desired_assets()
        missing = await asyncio.to_thread(self._missing, desired)
        for fetch in missing:
            await self._publisher.publish_now(fetch)

    def _missing(self, desired: frozenset[AssetJob]) -> list[AssetJob]:
        missing: list[AssetJob] = []
        with self._transactions.begin() as tx:
            for fetch in sorted(desired, key=lambda candidate: job_keys(candidate).lock):
                key = asset_key(fetch)
                asset = self._records.get(tx, key)
                if asset is None:
                    continue
                if asset.produced is None or not self._store.present(key, asset.produced):
                    missing.append(fetch)
        return missing
