"""`DiskStoredAssets`: the catalog's `StoredAssets` over the Asset record and the cache disk.

The record says what the file IS (produced facts); the disk alone says whether it is there
(`CacheStore.present`, `lstat` only). The same test `PrefetchHandler` applies to find missing
desired assets.
"""

from __future__ import annotations

from central.assets.store import CacheStore
from central.kernel.job_types import AssetJob
from central.kernel.jobs import asset_key
from central.kernel.ports import AssetRecords
from central.kernel.transactions import Transaction


class DiskStoredAssets:
    """Implements `StoredAssets`."""

    def __init__(self, *, records: AssetRecords, store: CacheStore) -> None:
        self._records = records
        self._store = store

    def present(self, tx: Transaction, job: AssetJob) -> bool:
        key = asset_key(job)
        asset = self._records.get(tx, key)
        return (asset is not None and asset.produced is not None
                and self._store.present(key, asset.produced))
