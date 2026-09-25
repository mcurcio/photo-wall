"""The one producer every asset fetch handler shares (design §10.4).

A verified file already on disk returns its facts without writing, which is what makes a crash
between the rename and the runtime's outcome write harmless. Otherwise the handler's `write` fills
a unique temp file from one reference's locator, trying every reference newest first; the result
is checked against the recorded produced facts and the references' expected facts, and only then
renamed into place. `produce` writes no record: the runtime writes `produced` and the outcome.

Any reference may supply the bytes: an asset's key is the sha256 of its upstream file, and every
download is checked against its locator's sha (`ReleaseOrigin.download`), so each reference names
the same bytes. A late run can only write the same bytes under the same name.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeAlias

from central.artifact_io import HardenedOpenError
from central.assets.store import CacheStore
from central.kernel.assets import Asset, AssetKey, AssetReady, OriginLocator
from central.kernel.handling import (
    OriginRejected,
    OriginUnavailable,
    TerminalFailure,
    TransientFailure,
)
from central.kernel.job_types import AssetJob
from central.kernel.jobs import asset_key
from central.kernel.ports import AssetRecords
from central.kernel.publishing import ASSET_NOT_RECORDED
from central.kernel.transactions import Transactions

# Fill the temp path from one locator. On any failure it leaves nothing at the temp path (as
# `ReleaseOrigin.download` does), so the next reference can reuse it.
WriteFn: TypeAlias = Callable[[Path, OriginLocator], Awaitable[None]]


def _meets_references(asset: Asset, facts: AssetReady) -> bool:
    """Every reference's stated expectation (size and/or digest, where set) holds for `facts`."""
    return all(
        (ref.expected_size is None or ref.expected_size == facts.size)
        and (ref.expected_sha256 is None or ref.expected_sha256 == facts.sha256)
        for ref in asset.references
    )


async def _write_from_any(temp: Path, asset: Asset, write: WriteFn) -> None:
    """`write` from each reference, newest first, until one succeeds.

    A rejection tries the next reference. An `OriginUnavailable` is remembered and raised when no
    reference succeeds; when every reference is rejected, `all_references_rejected`.
    """
    unavailable: OriginUnavailable | None = None
    for ref in asset.references:
        try:
            await write(temp, ref.locator)
            return
        except OriginRejected:
            continue
        except OriginUnavailable as error:
            unavailable = error
    if unavailable is not None:
        raise unavailable
    raise TerminalFailure("all_references_rejected")


class AssetProduction:
    """Shared by every fetch handler; each handler supplies only how to `write` its kind's file
    from one locator. The reference loop is here, so every kind falls through the same way."""

    def __init__(self, *, store: CacheStore, records: AssetRecords,
                 transactions: Transactions) -> None:
        self._store = store
        self._records = records
        self._transactions = transactions

    async def produce(self, job: AssetJob, write: WriteFn) -> AssetReady:
        key = asset_key(job)
        asset = await asyncio.to_thread(self._get, key)
        if asset is None:
            # Transient, as a waiter sees the same condition (PB7): the record is catalog state
            # that a later reference re-creates; a terminal outcome would stick (PB3).
            raise TransientFailure(ASSET_NOT_RECORDED)

        final = self._store.layout.path(key)
        present = await asyncio.to_thread(self._measure_present, final)
        if present is not None:
            if asset.produced is not None and present == asset.produced:
                return asset.produced
            newest = asset.references[0]
            if (asset.produced is None and newest.expected_sha256 is not None
                    and newest.expected_size is not None
                    and _meets_references(asset, present)):
                return present
            # Unverifiable (an os-image with no produced facts) or wrong: re-produce.
            await asyncio.to_thread(self._store.discard, final)

        temp = await asyncio.to_thread(self._store.temp_path, key)
        installed = False
        try:
            await _write_from_any(temp, asset, write)
            facts = await asyncio.to_thread(self._store.measure, temp)
            if asset.produced is not None and facts != asset.produced:
                # The key fixes the bytes, so this is a bug, never a re-cut (a re-cut is a new key).
                raise TerminalFailure("not_reproducible")
            if not _meets_references(asset, facts):
                raise TerminalFailure("digest_mismatch")
            await asyncio.to_thread(self._store.install, temp, key)
            installed = True
            return facts
        finally:
            if not installed:
                self._store.discard(temp)  # also on cancellation; unlink is cheap

    def _get(self, key: AssetKey) -> Asset | None:
        with self._transactions.begin() as tx:
            return self._records.get(tx, key)

    def _measure_present(self, final: Path) -> AssetReady | None:
        """The facts of the file at `final`, or None when there is no valid regular file.

        A symlink, fifo, directory or empty file counts as absent; `install` replaces it.
        """
        try:
            return self._store.measure(final)
        except (HardenedOpenError, ValueError):
            return None
