"""Library thumbnails on the one asset layer (console DDD §38 shape A, §40, G10).

A thumbnail may be referenced, fetched or served only while it is SERVABLE: a member of a live
preview, which `servable` reads from current data. `resolve` is the thumbnail route's catalog:
a servable id is referenced and resolved to its one fetch job, which the route reads through
its own dedicated `AssetReader`; any other id resolves to None, and nothing is written or
published for it. `prefetch` references and publishes a just-completed preview's members.

Every reference carries the same reserved locator: the library's address is held only by the
media worker (R22), whose injected `ThumbnailOrigin` finds the item from the preview's stored
member, so no handler reads this locator. A thumbnail is keyed by its original's identity, not
its own bytes (`AssetKind.keyed_by_content` is False), so the library may regenerate it: after a
purged cache, a re-production's new bytes replace its produced facts instead of failing for good.
Its record must not outlive the previews that select it: the media worker's maintenance deletes
the records (and files) of thumbnails no live preview selects
(`MediaRepository.maintain_source_previews`), and the next preview starts a fresh record.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Final

from central.kernel.assets import AssetReference, OriginLocator
from central.kernel.job_types import FetchLibraryThumbnail
from central.kernel.jobs import asset_key
from central.kernel.ports import AssetRecords, Candidates
from central.kernel.publishing import Publisher
from central.kernel.transactions import Transactions

# `.invalid` (RFC 2606) never resolves: the reference says "wanted", never where from.
THUMBNAIL_REFERENCE: Final = AssetReference(
    owner="library-preview",
    locator=OriginLocator("http://library.invalid/", sha256=None, size=None),
    expected_size=None, expected_sha256=None)


class LibraryThumbnails:
    def __init__(self, *, servable: Callable[[str], bool], records: AssetRecords,
                 transactions: Transactions, publisher: Publisher) -> None:
        self._servable = servable  # blocking: reads current data in its own transaction
        self._records = records
        self._transactions = transactions
        self._publisher = publisher

    async def resolve(self, asset_id: str) -> Candidates | None:
        """The servable id's one fetch job, referenced; None (nothing written) for any other."""
        try:
            job = FetchLibraryThumbnail(asset_id=asset_id)
        except ValueError:  # not an asset id at all
            return None
        if not await asyncio.to_thread(self._reference_if_servable, job):
            return None
        return Candidates(jobs=(job,), pinned=True)

    def _reference_if_servable(self, job: FetchLibraryThumbnail) -> bool:
        if not self._servable(job.asset_id):
            return False
        with self._transactions.begin() as tx:
            self._records.reference(tx, asset_key(job), THUMBNAIL_REFERENCE)
        return True

    async def prefetch(self, asset_ids: Sequence[str]) -> None:
        """Reference and publish each member of a preview the caller has just completed.

        One transaction; a request may retry a terminal failure (a new preview is a new ask).
        """
        jobs = [FetchLibraryThumbnail(asset_id=asset_id) for asset_id in asset_ids]

        def publish() -> None:
            with self._transactions.begin() as tx:
                for job in jobs:
                    self._records.reference(tx, asset_key(job), THUMBNAIL_REFERENCE)
                    self._publisher.publish(job, within=tx, retry_terminal=True)

        if jobs:
            await asyncio.to_thread(publish)
