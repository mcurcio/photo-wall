"""`DiskStoredAssets`: the catalog's `StoredAssets` over the Asset record and the cache disk.

The one predicate is `central.assets.readiness.CacheReadiness`; this adapter wires it to the
`job_outcomes` repository, so the catalog, Prefetch and the release read share it.
"""

from __future__ import annotations

from central.assets.readiness import CacheReadiness
from central.assets.store import CacheStore
from central.infra.outcomes import JobOutcomes
from central.kernel.ports import AssetRecords


class DiskStoredAssets(CacheReadiness):
    """Implements `StoredAssets` and `AssetReadiness` over PostgreSQL outcomes."""

    def __init__(self, *, records: AssetRecords, store: CacheStore) -> None:
        super().__init__(records=records, store=store, outcomes=JobOutcomes())
