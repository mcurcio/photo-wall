"""Job handlers of the assets domain: fetch an OS image, a Player `.deb`, a library thumbnail;
prefetch.

Job types are imported at runtime, never under `TYPE_CHECKING`: `handler_job_type` reads each
`handle` method's annotations with `typing.get_type_hints`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final

from central.assets.os_image import extract_squashfs
from central.assets.production import AssetProduction
from central.assets.store import CacheStore
from central.kernel.assets import AssetReady, OriginLocator
from central.kernel.handling import OriginRejected
from central.kernel.job_types import (
    AssetJob,
    FetchLibraryThumbnail,
    FetchOsImage,
    FetchPackage,
    FetchPlayerPayload,
    FetchSealedEnvironment,
    Prefetch,
)
from central.kernel.jobs import job_keys
from central.kernel.ports import (
    AssetReadiness,
    ContentCatalog,
    DesiredTiers,
    ReleaseOrigin,
    ThumbnailOrigin,
)
from central.kernel.publishing import Publisher, started
from central.kernel.transactions import Transactions
from contracts.node_boot import MAX_ENVIRONMENT_BYTES
from contracts.player_payload import MAX_ARCHIVE_BYTES, PayloadError, verify_archive
from contracts.release import MAX_ROOTFS_BYTES

MAX_TARBALL_BYTES: Final = MAX_ROOTFS_BYTES
MAX_PACKAGE_BYTES: Final = 1024**3


class FetchOsImageHandler:
    """Download one reference's base tarball, then extract the squashfs off the loop.

    `AssetProduction` tries every reference, newest first; each names the same tarball (the key).
    """

    def __init__(self, *, production: AssetProduction, origin: ReleaseOrigin,
                 store: CacheStore) -> None:
        self._production = production
        self._origin = origin
        self._store = store

    async def handle(self, job: FetchOsImage) -> AssetReady:
        return await self._production.produce(job, self._write)

    async def _write(self, temp: Path, locator: OriginLocator) -> None:
        # Beside `temp`, so it carries the temp prefix and is never mistaken for an asset; the
        # download creates it `O_EXCL` and removes it on any failure.
        tarball = temp.with_name(f"{temp.name}.tar.gz")
        try:
            await self._origin.download(locator, tarball,
                                        max_bytes=locator.size or MAX_TARBALL_BYTES)
            # gzip + sha256 over up to 1 GiB: never on the event loop (it would starve the
            # worker's heartbeat; the legacy fetch ran it inline).
            await asyncio.to_thread(extract_squashfs, tarball, temp)
        finally:
            self._store.discard(tarball)


class FetchPackageHandler:
    """Download the `.deb` from one reference; one file for every tag that ships it.

    `AssetProduction` tries every reference, newest first.
    """

    MAX_DOWNLOAD_BYTES = MAX_PACKAGE_BYTES

    def __init__(self, *, production: AssetProduction, origin: ReleaseOrigin) -> None:
        self._production = production
        self._origin = origin

    async def handle(self, job: FetchPackage) -> AssetReady:
        return await self._production.produce(job, self._write)

    async def _write(self, temp: Path, locator: OriginLocator) -> None:
        await self._origin.download(locator, temp, max_bytes=min(locator.size or self.MAX_DOWNLOAD_BYTES, self.MAX_DOWNLOAD_BYTES))


class FetchSealedEnvironmentHandler(FetchPackageHandler):
    """Cache exact closure bytes; base verifies the sealed root before any launch."""

    MAX_DOWNLOAD_BYTES = MAX_ENVIRONMENT_BYTES

    async def handle(self, job: FetchSealedEnvironment) -> AssetReady:
        return await self._production.produce(job, self._write)


class FetchPlayerPayloadHandler:
    """Cache verified data-only Player archive bytes under their own asset kind."""

    def __init__(self, *, production: AssetProduction, origin: ReleaseOrigin,
                 expected_abi: Callable[[str], Awaitable[str | None]]) -> None:
        self._production = production
        self._origin = origin
        self._expected_abi = expected_abi

    async def handle(self, job: FetchPlayerPayload) -> AssetReady:
        return await self._production.produce(job, self._write)

    async def _write(self, temp: Path, locator: OriginLocator) -> None:
        await self._origin.download(locator, temp,
                                    max_bytes=locator.size or MAX_ARCHIVE_BYTES)
        try:
            manifest = await asyncio.to_thread(verify_archive, temp)
            expected = await self._expected_abi(locator.sha256)
            if expected is None or manifest["base_abi"] != expected:
                raise PayloadError("base_abi_claim_mismatch")
        except PayloadError:
            temp.unlink(missing_ok=True)
            raise OriginRejected("player_payload_invalid") from None


class FetchLibraryThumbnailHandler:
    """Write one library thumbnail through the media worker's injected origin (R22).

    `AssetProduction` supplies the record check, the present-file shortcut, the produced-facts
    check and the install; the reference's locator is reserved and never read
    (`central.assets.library`): the origin finds the item from the preview's stored member.
    """

    def __init__(self, *, production: AssetProduction, origin: ThumbnailOrigin) -> None:
        self._production = production
        self._origin = origin

    async def handle(self, job: FetchLibraryThumbnail) -> AssetReady:
        async def write(temp: Path, _locator: OriginLocator) -> None:
            await self._origin.thumbnail(job.asset_id, temp)

        return await self._production.produce(job, write)


class PrefetchHandler:
    """Publish the fetch job of every `wanted` desired asset that is recorded but not on disk,
    then START at most ONE missing `background` asset (files only the window of newest stable
    node releases names; newest release first), all at each job type's own priority. One
    background download per tick keeps the window from flooding the shared FETCH queue ahead of
    previews and wanted files; the next tick starts the next one.

    The background walk stops at the first publish that `started` a job: a file whose latest
    outcome is terminal (PB3) or still in backoff (PB2) inserts nothing, so it never holds the
    tick's one slot and the rest of the window is still pre-downloaded.

    "Missing" is the one readiness predicate (`AssetReadiness.missing`). It never waits for those
    jobs and never sets `retry_terminal`: a terminal outcome stays terminal until a request or an
    operator asks again.
    """

    def __init__(self, *, catalog: ContentCatalog, readiness: AssetReadiness,
                 transactions: Transactions, publisher: Publisher) -> None:
        self._catalog = catalog
        self._readiness = readiness
        self._transactions = transactions
        self._publisher = publisher

    async def handle(self, job: Prefetch) -> None:
        tiers = await self._catalog.desired_tiers()
        wanted, background = await asyncio.to_thread(self._missing, tiers)
        for fetch in wanted:
            await self._publisher.publish_now(fetch)
        for fetch in background:
            if started(await self._publisher.publish_now(fetch)):
                break

    def _missing(self, tiers: DesiredTiers) -> tuple[list[AssetJob], list[AssetJob]]:
        with self._transactions.begin() as tx:
            wanted = [fetch for fetch in sorted(tiers.wanted, key=lambda job: job_keys(job).lock)
                      if self._readiness.missing(tx, fetch)]
            background = [fetch for fetch in tiers.background
                          if self._readiness.missing(tx, fetch)]
        return wanted, background
