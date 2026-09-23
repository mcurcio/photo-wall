"""`SyncReleasesHandler`: poll the release origin, record releases and their asset references.

Job types are imported at runtime, never under `TYPE_CHECKING`: `handler_job_type` resolves the
`handle` hints to find the job type this handler serves.

One sync is: read the ETag and list releases (an origin failure propagates; the runtime records
it); unless unchanged, one transaction per release (upsert, reference its `os-image` and
`player-deb` keys, retire a re-cut `.deb` or a dropped image), then store the ETag; then a tail
transaction (the stale-`pending` boot sweep, auto-promote, a fetch for every changed desired key,
and `Prefetch`). Withdrawal of tags gone upstream is not in the MVP.

A `.deb` re-cut upstream after its bytes were produced is FROZEN, as main froze a `mirrored`
tag as `divergent` (`central/app_releases.py` `upsert_discovered`, removed by the MVP): the
tag keeps the sha it was produced with, and the new sha is neither referenced nor fetched. An OS
image re-cut is taken instead (its old bytes are gone upstream): its produced facts are forgotten.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import timedelta

from central.content_catalog.boot_policy import newest
from central.content_catalog.catalog import ReleaseCatalog, in_transaction
from central.content_catalog.ports import DeviceRecords, ReleaseRecords, ReleaseRow
from central.kernel.assets import AssetKey, AssetReference, OriginLocator
from central.kernel.job_types import FetchOsImage, FetchPackage, Prefetch, SyncReleases
from central.kernel.jobs import asset_key, job_keys
from central.kernel.ports import AssetRecords, PublishedRelease, ReleaseOrigin
from central.kernel.publishing import Publisher
from central.kernel.transactions import Transaction, Transactions
from contracts.time import Clock

LOG = logging.getLogger("central.content_catalog.sync")


class SyncReleasesHandler:
    """Implements `Handler[SyncReleases, None]`."""

    def __init__(self, *, origin: ReleaseOrigin, releases: ReleaseRecords, devices: DeviceRecords,
                 assets: AssetRecords, transactions: Transactions, publisher: Publisher,
                 catalog: ReleaseCatalog, clock: Clock,
                 pending_health_timeout: timedelta = timedelta(minutes=15),
                 include_prereleases: bool = False) -> None:
        self._origin = origin
        self._releases = releases
        self._devices = devices
        self._assets = assets
        self._transactions = transactions
        self._publisher = publisher
        self._catalog = catalog
        self._clock = clock
        self._pending_health_timeout = pending_health_timeout
        self._include_prereleases = include_prereleases  # PHOTO_WALL_RELEASE_PRERELEASES

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
        frozen = self._frozen_package(tx, release)
        if frozen is not None:
            release = dataclasses.replace(release, package=frozen, package_problem=None)
        previous = self._releases.upsert(tx, release, now=self._clock.utc())
        if frozen is not None and not (previous is not None and previous.divergent):
            self._releases.mark_divergent(tx, tag)
        changed: set[AssetKey] = set()
        image_key = asset_key(FetchOsImage(tag=tag))
        if release.os_image is not None:
            image = AssetReference(owner=tag, locator=release.os_image,
                                   expected_size=None, expected_sha256=None)
            if self._assets.reference(tx, image_key, image):
                changed.add(image_key)
                self._forget_recut_image(tx, previous, release)
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

    def _forget_recut_image(self, tx: Transaction, previous: ReleaseRow | None,
                            release: PublishedRelease) -> None:
        """An OS image rebuilt under its tag (release.yml re-uploads with `--clobber`) replaces
        the old build, whose bytes are gone upstream: clear the produced facts so the next fetch
        records the new build instead of failing `not_reproducible` for good. Main overwrote the
        stored hash on every fetch. A `.deb` never needs this: its key is its sha."""
        old = previous.os_image if previous is not None else None
        new = release.os_image
        if old is None or new is None or (old.sha256, old.size) == (new.sha256, new.size):
            return
        LOG.warning("release %s: OS image re-cut upstream (%s -> %s); its produced facts are "
                    "cleared so the new build is fetched", release.tag, old.sha256, new.sha256)
        self._assets.forget_produced(tx, asset_key(FetchOsImage(tag=release.tag)))

    def _frozen_package(self, tx: Transaction, release: PublishedRelease) -> OriginLocator | None:
        """The `.deb` the tag must keep, or None when the upstream facts may be taken.

        A tag already divergent stays frozen. Otherwise a changed or dropped `.deb` whose old
        bytes were produced (main: `mirror_state='mirrored'`) freezes the tag now.
        """
        previous = self._releases.get(tx, release.tag)
        if previous is None or previous.package is None:
            return None
        old = previous.package
        if previous.divergent:
            return old
        if release.package is not None and release.package.sha256 == old.sha256:
            return None
        assert old.sha256 is not None  # a stored .deb locator always carries its sha
        asset = self._assets.get(tx, asset_key(FetchPackage(sha256=old.sha256)))
        if asset is None or asset.produced is None:
            return None  # never produced: the re-cut heals, as main refreshed an unmirrored tag
        LOG.warning("release %s: .deb re-cut upstream after it was produced (%s -> %s); keeping "
                    "the produced .deb, the tag is frozen as divergent", release.tag, old.sha256,
                    release.package.sha256 if release.package is not None else None)
        return old

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
        """Main's boot-time `_autopull_deb` rule (`central/app_release_boot.py`, removed by the
        MVP), now on every sync: suppress iff `cached > 0 AND bound > 0`, else promote the
        newest deployable release (prereleases only with PHOTO_WALL_RELEASE_PRERELEASES).

        `cached` was `count(*) FROM app_packages`; it is now "some release's `.deb` was
        produced". A configured fleet is never upgraded behind the operator's back.
        """
        promoted = self._releases.promoted_tag(tx)
        releases = self._releases.all(tx)
        bound = self._releases.bound_player_count(tx)
        if bound > 0 and self._any_package_produced(tx, releases):
            if promoted is None:
                LOG.warning("no release is promoted and auto-promote is suppressed (%d bound "
                            "players, .debs cached): GET /v1/app/manifest answers 503 "
                            "app_unconfigured until an operator promotes a release", bound)
            return
        candidate = newest(row.tag for row in releases if row.package is not None
                           and (self._include_prereleases or not row.is_prerelease))
        if candidate is not None and candidate != promoted:
            self._catalog.promote_in(tx, candidate)

    def _any_package_produced(self, tx: Transaction, releases: tuple[ReleaseRow, ...]) -> bool:
        shas = {row.package.sha256 for row in releases
                if row.package is not None and row.package.sha256 is not None}
        return any((asset := self._assets.get(tx, asset_key(FetchPackage(sha256=sha))))
                   is not None and asset.produced is not None for sha in sorted(shas))
