"""`MaintainCacheHandler`: remove cached release files nothing wants (auto-ingest design §6.3).

The keep-set is the ONE desired set (`CacheRetention.hold_desired`, which is `desired_in` read
under the asset-roots lock): what Prefetch downloads, the cleaner keeps. The sweep is eager, not
budget-gated: every release file whose key is not desired and that is past its grace is removed,
whatever the free space. The cache stays ephemeral: rows, references and produced facts stay, so
a key that becomes wanted again re-fetches by Prefetch or read-through.

* **Scope.** Only `os-images/` (OS images) and `apps/` (sealed environments, Player `.deb`s and
  payloads). Never `previews/` (library thumbnails) and never `media/` (`central/media_store.py`).
* **Never a desired file.** The lock is held from the desired read through every unlink, and
  every writer of a desired-set input (`central/infra/asset_roots.py` lists them) takes the same
  lock, so no root can be committed in between.
* **Never a file being served.** A file whose mtime or `last_served_at` is within `SERVE_GRACE`
  is spared: a fetch that landed after the desired read has a fresh mtime, and the reader touches
  `last_served_at` at every open (at most every 5 minutes per process), so a stream younger than
  55 minutes is never unlinked. An open descriptor keeps its inode on POSIX anyway.
  Precondition: no single stream lasts longer than 55 minutes.
* **Temp files** (`TEMP_PREFIX`) are removed only when idle past `TEMP_GRACE`: a live download
  advances its mtime with every write and fails after 30 s of silence.
* **Bounded.** At most `MAX_UNLINKS_PER_RUN` per run, which bounds how long the lock is held.

Cost: mtime (the cache filesystem's clock) and `last_served_at` (Central's clock) are compared
with this worker's clock. On the one host the cache is mounted from, they are one clock; the
grace is an hour, far above any skew a synchronised host shows.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from datetime import timedelta
from pathlib import Path
from typing import Final

from central.assets.layout import TEMP_PREFIX
from central.assets.store import CacheStore
from central.kernel.assets import AssetKey, AssetKind
from central.kernel.job_types import MaintainCache
from central.kernel.ports import AssetRecords, CacheRetention
from central.kernel.transactions import Transactions
from contracts.time import Clock

LOG = logging.getLogger("central.assets.maintenance")

SERVE_GRACE: Final = timedelta(hours=1)
TEMP_GRACE: Final = timedelta(hours=1)
MAX_UNLINKS_PER_RUN: Final = 50
# The release kinds, grouped by the directory they share. `previews/` and `media/` are absent:
# the cleaner never touches them.
RELEASE_KINDS: Final[tuple[tuple[AssetKind, ...], ...]] = (
    (AssetKind.OS_IMAGE,),
    (AssetKind.SEALED_ENVIRONMENT, AssetKind.PLAYER_DEB, AssetKind.PLAYER_PAYLOAD),
)


class MaintainCacheHandler:
    """Implements `Handler[MaintainCache, None]`."""

    def __init__(self, *, retention: CacheRetention, records: AssetRecords, store: CacheStore,
                 transactions: Transactions, clock: Clock) -> None:
        self._retention = retention
        self._records = records
        self._store = store
        self._transactions = transactions
        self._clock = clock

    async def handle(self, job: MaintainCache) -> None:
        removed = await asyncio.to_thread(self.sweep)
        if removed:
            LOG.info("removed %d unwanted cache file(s): %s", len(removed),
                     ", ".join(path.name for path in removed))

    def sweep(self) -> list[Path]:
        """One run: the removed paths, at most `MAX_UNLINKS_PER_RUN`."""
        removed: list[Path] = []
        with self._transactions.begin() as tx:
            desired = self._retention.hold_desired(tx)  # the lock is held until tx ends
            now = self._clock.utc()
            served_before = now - SERVE_GRACE.total_seconds()
            idle_before = now - TEMP_GRACE.total_seconds()
            layout = self._store.layout
            for kinds in RELEASE_KINDS:
                directory = layout.directory(kinds[0])
                for path, metadata in _regular_files(directory):
                    if len(removed) >= MAX_UNLINKS_PER_RUN:
                        return removed
                    if path.name.startswith(TEMP_PREFIX):
                        if metadata.st_mtime < idle_before and _unlink(path):
                            removed.append(path)
                        continue
                    key = layout.key_of(kinds, path.name)
                    if key is None or key in desired or metadata.st_mtime >= served_before:
                        continue
                    if self._served_since(tx, key, served_before):
                        continue
                    if _unlink(path):
                        removed.append(path)
        return removed

    def _served_since(self, tx, key: AssetKey, served_before: float) -> bool:
        asset = self._records.get(tx, key)
        return (asset is not None and asset.last_served_at is not None
                and asset.last_served_at >= served_before)


def _regular_files(directory: Path) -> list[tuple[Path, os.stat_result]]:
    """The regular files directly in `directory` (never followed through a symlink), by name."""
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return []
    found = []
    for name in names:
        path = directory / name
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(metadata.st_mode):
            found.append((path, metadata))
    return found


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
