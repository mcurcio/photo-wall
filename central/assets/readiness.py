"""`CacheReadiness`: the one answer to "is this in the cache now" (auto-ingest design §5B).

The Asset record says what a file IS (produced facts); the disk alone says whether it is there
(`CacheStore.present`, `lstat` only); an outcome only explains an absence. Prefetch (what to
fetch), the release read (what to show) and first-run selection (when to select) all ask this
one object, so they cannot disagree. Nothing here is stored: a cleaned or purged file is simply
absent on the next read.
"""

from __future__ import annotations

from collections.abc import Collection

from central.assets.store import CacheStore
from central.kernel.assets import Asset
from central.kernel.job_types import AssetJob
from central.kernel.jobs import asset_key, job_keys
from central.kernel.ports import (
    CACHE_DISK_FULL,
    AssetRecords,
    LatestOutcomes,
    Readiness,
)
from central.kernel.transactions import Transaction


def _final_size(asset: Asset | None) -> int:
    """The size the asset's file will have: its produced facts, else the newest reference's
    stated expectation, else its download size; 0 when nothing states one."""
    if asset is None:
        return 0
    if asset.produced is not None:
        return asset.produced.size
    for reference in asset.references:
        if reference.expected_size is not None:
            return reference.expected_size
    return next((ref.locator.size for ref in asset.references if ref.locator.size), 0)


class CacheReadiness:
    """Implements `kernel.ports.AssetReadiness` (and so `StoredAssets`)."""

    def __init__(self, *, records: AssetRecords, store: CacheStore,
                 outcomes: LatestOutcomes) -> None:
        self._records = records
        self._store = store
        self._outcomes = outcomes

    def _present(self, asset: Asset | None) -> bool:
        return (asset is not None and asset.produced is not None
                and self._store.present(asset.key, asset.produced))

    def present(self, tx: Transaction, job: AssetJob) -> bool:
        return self._present(self._records.get(tx, asset_key(job)))

    def missing(self, tx: Transaction, job: AssetJob) -> bool:
        asset = self._records.get(tx, asset_key(job))
        return asset is not None and not self._present(asset)

    def readiness(self, tx: Transaction, jobs: Collection[AssetJob], *,
                  wanted: bool) -> Readiness:
        missing_bytes, absent = 0, False
        failures: list[str] = []
        for job in sorted(jobs, key=lambda candidate: job_keys(candidate).lock):
            asset = self._records.get(tx, asset_key(job))
            if self._present(asset):
                continue  # data first: facts plus file decide before any outcome
            absent = True
            missing_bytes += _final_size(asset)
            outcome = self._outcomes.get(tx, job_keys(job).lock)
            if outcome is not None and (outcome.status == "terminal" or (
                    outcome.status == "transient" and outcome.reason == CACHE_DISK_FULL)):
                failures.append(outcome.reason or "fetch_failed")
        if failures:
            return Readiness("failed", failures[0], missing_bytes)
        if not absent:
            return Readiness("ready", None, 0)
        return Readiness("downloading" if wanted else "not_downloaded", None, missing_bytes)
