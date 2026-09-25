"""`SyncReleasesHandler`: poll the release origin, record releases and their asset references.

Job types are imported at runtime, never under `TYPE_CHECKING`: `handler_job_type` resolves the
`handle` hints to find the job type this handler serves.

One sync is: read the ETag and list releases, sending the ETag only if it was stored less than
`ETAG_MAX_AGE` ago (an origin failure propagates; the runtime records it); unless unchanged, one
transaction per release, then store the ETag; then a tail transaction (the stale-`pending` boot
sweep, auto-promote, a fetch for every changed desired key, and `Prefetch`). Withdrawal of tags
gone upstream is not in the MVP.

Jobs may run in any order (docs/central-idempotent-jobs.md rule 2, §6): each release transaction
applies one observation only if its upstream version (the manifest asset's `(updated_at, id)`)
is not older than the row's. It claims the tag (insert if absent, else lock the row), derives the
frozen flag from the locked previous row, and offers the observation to the guarded write. A
refused, older observation touches nothing: no row, no reference. An applied one references its
`os-image` and `player-deb` keys and retires the tag's reference to a re-cut or dropped `.deb` or
OS image. An equal version re-applies (a prerelease flag can change without a new manifest), so a
stale equal-version observation can land after a fresh one: the hourly full listing repairs it.

A `.deb` re-cut upstream after its bytes were produced is FROZEN, as main froze a `mirrored`
tag as `divergent` (`central/app_releases.py` `upsert_discovered`, removed by the MVP): the
tag keeps the sha it was produced with, and the new sha is neither referenced nor fetched. The
flag is derived on every write, never carried: upstream returning to the produced `.deb`
unfreezes the tag. An OS image re-cut is taken instead (its old bytes are gone upstream). It is
keyed by its base tarball's sha256, so a re-cut is a new key: the tag references the new key and
retires its reference to the old one, whose row and produced facts stay (facts are never
cleared).

Automatic promotion holds a transaction-scoped advisory lock from its first read to its write, so
the last writer read every row committed before it.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import timedelta
from typing import Final

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
# How long a stored ETag is trusted (a placeholder, design §6.4): past it the sync lists in full,
# which repairs a stale equal-version observation within ETAG_MAX_AGE plus one sync interval.
ETAG_MAX_AGE: Final = timedelta(hours=1)


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
        stored = await in_transaction(transactions, self._releases.load_etag)
        fresh = (stored is not None and self._clock.utc() - stored.stored_at
                 < ETAG_MAX_AGE.total_seconds())
        listing = await self._origin.list_releases(etag=stored.etag if fresh else None)
        changed: set[AssetKey] = set()
        if not listing.unchanged:
            for release in listing.releases:
                changed |= await in_transaction(
                    transactions, lambda tx, r=release: self._record(tx, r))
            await in_transaction(transactions, lambda tx: self._releases.store_etag(
                tx, listing.etag, now=self._clock.utc()))
        await in_transaction(transactions, lambda tx: self._tail(tx, changed))

    def _record(self, tx: Transaction, release: PublishedRelease) -> set[AssetKey]:
        """Apply one observation and its references; return the keys whose reference changed.

        An observation older than the row's is refused whole: nothing changes."""
        tag = release.tag
        now = self._clock.utc()
        previous = self._releases.claim(tx, release, now=now)
        if previous is not None:
            frozen = self._frozen_package(tx, release, previous)
            if frozen is not None:
                release = dataclasses.replace(release, package=frozen, package_problem=None)
            if not self._releases.apply(tx, release, divergent=frozen is not None, now=now):
                LOG.info("release %s: an older observation was refused", tag)
                return set()
        changed: set[AssetKey] = set()
        image = release.os_image
        if image is not None:
            assert image.sha256 is not None  # PublishedRelease guarantees a complete locator
            image_key = asset_key(FetchOsImage(tarball_sha256=image.sha256))
            reference = AssetReference(owner=tag, locator=image,
                                       expected_size=None, expected_sha256=None)
            if self._assets.reference(tx, image_key, reference):
                changed.add(image_key)
        old_image = previous.os_image if previous is not None else None
        if old_image is not None and old_image.sha256 is not None and (
                image is None or image.sha256 != old_image.sha256):
            if image is not None:
                LOG.warning("release %s: OS image re-cut upstream (%s -> %s); the new build is "
                            "referenced under its own key", tag, old_image.sha256, image.sha256)
            # re-cut or dropped: the old key keeps its row and facts
            self._assets.retire(
                tx, asset_key(FetchOsImage(tarball_sha256=old_image.sha256)), tag)
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

    def _frozen_package(self, tx: Transaction, release: PublishedRelease,
                        previous: ReleaseRow) -> OriginLocator | None:
        """The `.deb` the tag must keep, or None when the upstream facts may be taken.

        Derived from the locked previous row, never carried: a changed or dropped `.deb` whose
        old bytes were produced (main: `mirror_state='mirrored'`) freezes the tag, and upstream
        naming the produced `.deb` again unfreezes it. "Produced" is read under
        `lock_produced`, so a `FetchPackage` recording its facts either committed first and
        freezes the tag, or waits for this transaction and lands after the re-cut was taken.
        Lock order: the release row (`claim`), then the old `.deb`'s asset row, then the rows
        `reference` inserts.
        """
        if previous.package is None:
            return None
        old = previous.package
        if release.package is not None and release.package.sha256 == old.sha256:
            return None
        assert old.sha256 is not None  # a stored .deb locator always carries its sha
        if self._assets.lock_produced(tx, asset_key(FetchPackage(sha256=old.sha256))) is None:
            return None  # never produced: the re-cut heals, as main refreshed an unmirrored tag
        if not previous.divergent:  # freezing now; while frozen, every sync derives it again
            LOG.warning(
                "release %s: .deb re-cut upstream after it was produced (%s -> %s); keeping the "
                "produced .deb, the tag is frozen as divergent", release.tag, old.sha256,
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
        """Auto-promote the newest release while nothing is promoted or the promotion is the
        sync's own ("auto"); an operator promotion is never moved (issue #23, owner ruling).

        Main's boot-time `_autopull_deb` rule (`central/app_release_boot.py`, removed by the
        MVP), now on every sync: suppress iff `cached > 0 AND bound > 0`, else promote the
        newest deployable release (prereleases only with PHOTO_WALL_RELEASE_PRERELEASES).
        `cached` was `count(*) FROM app_packages`; it is now "some release's `.deb` was
        produced". A configured fleet is never upgraded behind the operator's back.

        The advisory lock comes FIRST: the rows read below include every release committed
        before it, and no other sync's promotion can land between this read and this write.
        """
        self._releases.lock_auto_promotion(tx)
        promotion = self._releases.promotion(tx)
        if promotion is not None and promotion.by == "operator":
            return
        releases = self._releases.all(tx)
        bound = self._releases.bound_player_count(tx)
        if bound > 0 and self._any_package_produced(tx, releases):
            if promotion is None:
                LOG.warning("no release is promoted and auto-promote is suppressed (%d bound "
                            "players, .debs cached): GET /v1/app/manifest answers 503 "
                            "app_unconfigured until an operator promotes a release", bound)
            return
        candidate = newest(row.tag for row in releases if row.package is not None
                           and (self._include_prereleases or not row.is_prerelease))
        # The advisory lock serializes syncs, not the operator route: the write re-checks under
        # the row lock and refuses to move an operator promotion committed since.
        if (candidate is not None and (promotion is None or candidate != promotion.tag)
                and not self._catalog.promote_in(tx, candidate, by="auto")):
            LOG.info("auto-promote of %s yielded to an operator promotion made meanwhile",
                     candidate)

    def _any_package_produced(self, tx: Transaction, releases: tuple[ReleaseRow, ...]) -> bool:
        shas = {row.package.sha256 for row in releases
                if row.package is not None and row.package.sha256 is not None}
        return any((asset := self._assets.get(tx, asset_key(FetchPackage(sha256=sha))))
                   is not None and asset.produced is not None for sha in sorted(shas))
