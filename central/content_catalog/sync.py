"""`SyncReleasesHandler`: poll the release origin, record releases and their asset references.

Job types are imported at runtime, never under `TYPE_CHECKING`: `handler_job_type` resolves the
`handle` hints to find the job type this handler serves.

One sync is: read the ETag and list releases, sending the ETag only if it was stored less than
`ETAG_MAX_AGE` ago (an origin failure propagates; the runtime records it); unless unchanged, one
transaction per release, then store the ETag; then a tail transaction (the first-run
auto-select, a fetch for every changed desired key, and `Prefetch`). Withdrawal of tags gone
upstream is not in the MVP.

Jobs may run in any order (docs/central-idempotent-jobs.md rule 2, §6): each release transaction
applies one observation only if its upstream version (the `manifest.json` asset's
`(updated_at, id)`) is not older than the row's. It claims the tag (insert if absent, else lock
the row) and offers the observation to the guarded write. A refused, older observation touches
nothing: no row, no reference. An applied one references the release's `os-image` key and
retires the tag's reference to a re-cut or dropped OS image. An equal version re-applies (a
prerelease flag can change without a new manifest), so a stale equal-version observation can land
after a fresh one: the hourly full listing repairs it. An OS image is keyed by its base tarball's
sha256, so a re-cut is a new key: the tag references the new key and retires its reference to the
old one, whose row and produced facts stay (facts are never cleared).

The sync reads no Player `.deb` from a release: `app_releases`' package columns are written by
no sync (`/v1/app/manifest` serves only a package already recorded there).

**Node releases** (auto-ingest design §6.1): a release that attaches a node manifest gets a
second transaction of its own after the `manifest.json` one. It writes the release catalog row,
the deployment (`NodeReleaseRecords.ingest`, deployment id derived from the manifest digest) and
the tag's observation, under the same not-older guard. A refusal of that release (a malformed
manifest the origin already turned into `node_problem`, or an identity / pin / environment
conflict the ingest raises) rolls that transaction back and records the problem on the tag in
another: one release's failure is that release's state, and the tick, the ETag and every other
release proceed. A deployment that cannot be built from a manifest the origin accepted (a bug,
or data the code did not foresee) is refused by the ingest itself as `node_release_invalid`,
classified where that data error starts. Every other exception stops the tick before the ETag
is stored: the origin's own failures (listing, transport) and the database's (lost connection,
deadlock, timeout), which say nothing about the release. Ingest never selects. The tail's first-run auto-select is the only automatic
selection: with no selection ever made, it selects the newest stable release that has not
failed, once that release is ready (`first_run_candidate`, which the release read shows too),
through the one select writer with expected revision 0, so it can only create the selection,
never move it.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Final

from central.content_catalog.boot_policy import first_run_choice
from central.content_catalog.catalog import ReleaseCatalog, in_transaction
from central.content_catalog.ports import (
    NodeReleaseRecords,
    NodeReleaseRefused,
    ReleaseRecords,
    StableDeployment,
)
from central.kernel.assets import AssetKey, AssetReference
from central.kernel.job_types import FetchOsImage, Prefetch, SyncReleases
from central.kernel.jobs import asset_key, job_keys
from central.kernel.ports import (
    AssetReadiness,
    AssetRecords,
    PublishedRelease,
    Readiness,
    ReleaseOrigin,
)
from central.kernel.publishing import Publisher
from central.kernel.transactions import Transaction, Transactions
from contracts.time import Clock

LOG = logging.getLogger("central.content_catalog.sync")
# How long a stored ETag is trusted (a placeholder, design §6.4): past it the sync lists in full,
# which repairs a stale equal-version observation within ETAG_MAX_AGE plus one sync interval.
ETAG_MAX_AGE: Final = timedelta(hours=1)


def first_run_candidate(tx: Transaction, node: NodeReleaseRecords, readiness: AssetReadiness
                        ) -> tuple[StableDeployment, Readiness] | None:
    """The release a wall that never had a selection gets (R4) and its readiness: the newest
    stable release with a deployment that has not failed (`first_run_choice`); None once any
    selection exists or with no candidate. The ONE definition: the sync tail selects it once it
    is ready, and the release read shows it."""
    if node.selection_exists(tx):
        return None
    return first_run_choice((row, readiness.readiness(tx, row.jobs, wanted=True))
                            for row in node.stable_deployments(tx))


class SyncReleasesHandler:
    """Implements `Handler[SyncReleases, None]`."""

    def __init__(self, *, origin: ReleaseOrigin, releases: ReleaseRecords, assets: AssetRecords,
                 transactions: Transactions, publisher: Publisher, catalog: ReleaseCatalog,
                 clock: Clock, node_releases: NodeReleaseRecords,
                 readiness: AssetReadiness) -> None:
        self._origin = origin
        self._node = node_releases
        self._readiness = readiness
        self._releases = releases
        self._assets = assets
        self._transactions = transactions
        self._publisher = publisher
        self._catalog = catalog
        self._clock = clock

    async def handle(self, job: SyncReleases) -> None:
        transactions = self._transactions
        stored = await in_transaction(transactions, self._releases.load_etag)
        fresh = (stored is not None and self._clock.utc() - stored.stored_at
                 < ETAG_MAX_AGE.total_seconds())
        listing = await self._origin.list_releases(etag=stored.etag if fresh else None)
        changed: set[AssetKey] = set()
        if not listing.unchanged:
            for release in listing.releases:
                if release.legacy:
                    changed |= await in_transaction(
                        transactions, lambda tx, r=release: self._record(tx, r))
                await self._record_node(release)
            await in_transaction(transactions, lambda tx: self._releases.store_etag(
                tx, listing.etag, now=self._clock.utc()))
        await in_transaction(transactions, lambda tx: self._tail(tx, changed))

    async def _record_node(self, release: PublishedRelease) -> None:
        """The release's node half: ingest it, or record why it is refused (module docstring)."""
        problem = release.node_problem
        if release.node_publication is None and problem is None:
            return  # no node manifest attached
        now = self._clock.utc()
        if release.node_publication is not None:
            try:
                if not await in_transaction(self._transactions, lambda tx: self._node.ingest(
                        tx, release, now=now)):
                    LOG.info("node release %s: an older observation was refused", release.tag)
                return
            except NodeReleaseRefused as refused:  # the release's own data; nothing else
                if refused.__cause__ is not None:
                    LOG.warning("node release %s: its deployment could not be built",
                                release.tag, exc_info=refused.__cause__)
                problem = refused.reason
        assert problem is not None
        LOG.warning("node release %s refused: %s", release.tag, problem)
        if not await in_transaction(self._transactions, lambda tx: self._node.record_problem(
                tx, release, problem, now=now)):
            LOG.info("node release %s: an older refusal was not recorded", release.tag)

    def _record(self, tx: Transaction, release: PublishedRelease) -> set[AssetKey]:
        """Apply one observation and its references; return the keys whose reference changed.

        An observation older than the row's is refused whole: nothing changes."""
        tag = release.tag
        now = self._clock.utc()
        previous = self._releases.claim(tx, release, now=now)
        if previous is not None and not self._releases.apply(tx, release, now=now):
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
        return changed

    def _tail(self, tx: Transaction, changed: set[AssetKey]) -> None:
        self._auto_select(tx)
        if changed:
            fetches = [job for job in self._catalog.desired_in(tx) if asset_key(job) in changed]
            for fetch in sorted(fetches, key=lambda job: job_keys(job).lock):
                self._publisher.publish(fetch, within=tx, retry_terminal=True)
        self._publisher.publish(Prefetch(), within=tx)

    def _auto_select(self, tx: Transaction) -> None:
        """First run only (R4): with no node boot selection ever made, select the newest stable
        release that has not failed once it is ready. Never moves a selection (R6)."""
        choice = first_run_candidate(tx, self._node, self._readiness)
        if choice is None:
            return
        row, readiness = choice
        if readiness.state != "ready":
            LOG.info("no node boot selection yet: %s is %s", row.tag, readiness.state)
            return
        if self._node.select_first(tx, row.deployment_id, now=self._clock.utc()):
            LOG.warning("no node boot selection existed: selected %s (%s), the newest ready "
                        "stable release; later upgrades are the operator's", row.tag,
                        row.deployment_id)
        else:
            LOG.info("first-run selection of %s yielded to a selection made meanwhile", row.tag)
