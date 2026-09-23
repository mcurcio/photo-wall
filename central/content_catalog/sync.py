"""`SyncReleasesHandler`: poll the release origin, record releases and their asset references.

Job types are imported at runtime, never under `TYPE_CHECKING`: `handler_job_type` resolves the
`handle` hints to find the job type this handler serves.

One sync is: read the ETag and list releases (an origin failure propagates; the runtime records
it); unless unchanged, one transaction per release (upsert, reference its `os-image` and
`player-deb` keys, retire a re-cut `.deb` or a dropped image), then store the ETag; then a tail
transaction (the stale-`pending` boot sweep, auto-promote, a fetch for every changed desired key,
and `Prefetch`). Withdrawal of tags gone upstream is not in the MVP.
"""

from __future__ import annotations

from datetime import timedelta

from central.content_catalog.boot_policy import newest
from central.content_catalog.catalog import ReleaseCatalog, in_transaction
from central.content_catalog.ports import DeviceRecords, ReleaseRecords
from central.kernel.assets import AssetKey, AssetReference
from central.kernel.job_types import FetchOsImage, FetchPackage, Prefetch, SyncReleases
from central.kernel.jobs import asset_key, job_keys
from central.kernel.ports import AssetRecords, PublishedRelease, ReleaseOrigin
from central.kernel.publishing import Publisher
from central.kernel.transactions import Transaction, Transactions
from contracts.time import Clock


class SyncReleasesHandler:
    """Implements `Handler[SyncReleases, None]`."""

    def __init__(self, *, origin: ReleaseOrigin, releases: ReleaseRecords, devices: DeviceRecords,
                 assets: AssetRecords, transactions: Transactions, publisher: Publisher,
                 catalog: ReleaseCatalog, clock: Clock,
                 pending_health_timeout: timedelta = timedelta(minutes=15)) -> None:
        self._origin = origin
        self._releases = releases
        self._devices = devices
        self._assets = assets
        self._transactions = transactions
        self._publisher = publisher
        self._catalog = catalog
        self._clock = clock
        self._pending_health_timeout = pending_health_timeout

    async def handle(self, job: SyncReleases) -> None:
        transactions = self._transactions
        etag = await in_transaction(transactions, self._releases.load_etag)
        listing = await self._origin.list_releases(etag=etag)
        changed: set[AssetKey] = set()
        if not listing.unchanged:
            for release in listing.releases:
                changed |= await in_transaction(
                    transactions, lambda tx, r=release: self._record(tx, r))
            await in_transaction(
                transactions, lambda tx: self._releases.store_etag(tx, listing.etag))
        await in_transaction(transactions, lambda tx: self._tail(tx, changed))

    def _record(self, tx: Transaction, release: PublishedRelease) -> set[AssetKey]:
        """Upsert one release and its references; return the keys whose reference changed."""
        tag = release.tag
        previous = self._releases.upsert(tx, release, now=self._clock.utc())
        changed: set[AssetKey] = set()
        image_key = asset_key(FetchOsImage(tag=tag))
        if release.os_image is not None:
            image = AssetReference(owner=tag, locator=release.os_image,
                                   expected_size=None, expected_sha256=None)
            if self._assets.reference(tx, image_key, image):
                changed.add(image_key)
        elif previous is not None and previous.os_image is not None:
            self._assets.retire(tx, image_key, tag)  # the release dropped its image
        package = release.package
        if package is not None:
            assert package.sha256 is not None  # PublishedRelease guarantees a complete locator
            deb_key = asset_key(FetchPackage(sha256=package.sha256))
            deb = AssetReference(owner=tag, locator=package, expected_size=package.size,
                                 expected_sha256=package.sha256)
            if self._assets.reference(tx, deb_key, deb):
                changed.add(deb_key)
        old = previous.package if previous is not None else None
        if old is not None and old.sha256 is not None and (
                package is None or package.sha256 != old.sha256):
            self._assets.retire(tx, asset_key(FetchPackage(sha256=old.sha256)), tag)  # re-cut
        return changed

    def _tail(self, tx: Transaction, changed: set[AssetKey]) -> None:
        served_before = self._clock.utc() - self._pending_health_timeout.total_seconds()
        self._devices.sweep_failed_boots(tx, served_before=served_before)
        self._auto_promote(tx)
        if changed:
            fetches = [job for job in self._catalog.desired_in(tx) if asset_key(job) in changed]
            for fetch in sorted(fetches, key=lambda job: job_keys(job).lock):
                self._publisher.publish(fetch, within=tx, retry_terminal=True)
        self._publisher.publish(Prefetch(), within=tx)

    def _auto_promote(self, tx: Transaction) -> None:
        """Replaces boot-time `_autopull_deb`: promote the newest full deployable release when
        nothing is promoted or no player is bound ("cached == 0" is now "nothing promoted")."""
        promoted = self._releases.promoted_tag(tx)
        if promoted is not None and self._releases.bound_player_count(tx) != 0:
            return
        candidate = newest(row.tag for row in self._releases.all(tx)
                           if not row.is_prerelease and row.package is not None)
        if candidate is not None and candidate != promoted:
            self._releases.set_promoted(tx, candidate)
