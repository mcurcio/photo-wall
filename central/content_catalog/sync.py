"""`SyncReleasesHandler`: poll the release origin and ingest each release's node manifest.

Job types are imported at runtime, never under `TYPE_CHECKING`: `handler_job_type` resolves the
`handle` hints to find the job type this handler serves.

One sync is: read the ETag and list releases, sending the ETag only if it was stored less than
`ETAG_MAX_AGE` ago (an origin failure propagates; the runtime records it); unless unchanged, one
transaction per release, then store the ETag; then a tail transaction (`Prefetch`). Withdrawal of tags gone upstream is not in the MVP.

Jobs may run in any order (docs/central-idempotent-jobs.md rule 2, §6). A release that attaches
a node manifest (auto-ingest design §6.1) gets one transaction of its own. It writes the release
catalog row, the deployment (`NodeReleaseRecords.ingest`, deployment id derived from the
manifest digest, with its base tarball and sealed environments referenced) and the tag's
observation, under the not-older guard on the node manifest asset's `(updated_at, id)`. An equal
version re-applies, so a stale equal-version observation can land after a fresh one: the hourly
full listing repairs it. A refusal of that release (a malformed manifest the origin already
turned into `node_problem`, or an identity / pin / environment conflict the ingest raises) rolls
that transaction back and records the problem on the tag in another: one release's failure is
that release's state, and the tick, the ETag and every other release proceed. A deployment that
cannot be built from a manifest the origin accepted (a bug, or data the code did not foresee)
is refused by the ingest itself as `node_release_invalid`, classified where that data error
starts. Every other exception stops the tick before the ETag is stored: the origin's own
failures (listing, transport) and the database's (lost connection, deadlock, timeout), which say
nothing about the release. Ingest never chooses a Pi's version: a Pi with none boots the newest
ready stable release (`boot_policy.newest_ready_stable`), and a version changes only by the
operator's or Home Assistant's install (central/infra/player_versions.py).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Final

from central.content_catalog.catalog import in_transaction
from central.content_catalog.ports import (
    NodeReleaseRecords,
    NodeReleaseRefused,
    ReleaseRecords,
)
from central.kernel.job_types import Prefetch, SyncReleases
from central.kernel.ports import PublishedRelease, ReleaseOrigin
from central.kernel.publishing import Publisher
from central.kernel.transactions import Transaction, Transactions
from contracts.time import Clock

LOG = logging.getLogger("central.content_catalog.sync")
# How long a stored ETag is trusted (a placeholder, design §6.4): past it the sync lists in full,
# which repairs a stale equal-version observation within ETAG_MAX_AGE plus one sync interval.
ETAG_MAX_AGE: Final = timedelta(hours=1)


class SyncReleasesHandler:
    """Implements `Handler[SyncReleases, None]`."""

    def __init__(self, *, origin: ReleaseOrigin, releases: ReleaseRecords,
                 transactions: Transactions, publisher: Publisher, clock: Clock,
                 node_releases: NodeReleaseRecords) -> None:
        self._origin = origin
        self._node = node_releases
        self._releases = releases
        self._transactions = transactions
        self._publisher = publisher
        self._clock = clock

    async def handle(self, job: SyncReleases) -> None:
        transactions = self._transactions
        stored = await in_transaction(transactions, self._releases.load_etag)
        fresh = (stored is not None and self._clock.utc() - stored.stored_at
                 < ETAG_MAX_AGE.total_seconds())
        listing = await self._origin.list_releases(etag=stored.etag if fresh else None)
        if not listing.unchanged:
            for release in listing.releases:
                await self._record_node(release)
            await in_transaction(transactions, lambda tx: self._releases.store_etag(
                tx, listing.etag, now=self._clock.utc()))
        await in_transaction(transactions, self._tail)

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

    def _tail(self, tx: Transaction) -> None:
        self._publisher.publish(Prefetch(), within=tx)
