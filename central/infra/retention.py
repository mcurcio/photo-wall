"""`PgCacheRetention`: the cache cleaner's keep-set, read under the asset-roots lock.

The desired set is the catalog's (`ReleaseCatalog.desired_in`), the same one Prefetch downloads;
the lock is the one every root writer takes (`central/infra/asset_roots.py`).
"""

from __future__ import annotations

from central.content_catalog.catalog import ReleaseCatalog
from central.infra.asset_roots import lock_asset_roots
from central.kernel.assets import AssetKey
from central.kernel.jobs import asset_key
from central.kernel.transactions import Transaction


class PgCacheRetention:
    """Implements `kernel.ports.CacheRetention`."""

    def __init__(self, catalog: ReleaseCatalog) -> None:
        self._catalog = catalog

    def hold_desired(self, tx: Transaction) -> frozenset[AssetKey]:
        lock_asset_roots(tx)
        return frozenset(asset_key(job) for job in self._catalog.desired_in(tx))
