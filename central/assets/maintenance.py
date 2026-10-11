"""`MaintainCacheHandler`: remove cached release files nothing wants (auto-ingest design §6.3).

The keep-set is the ONE desired set (`ContentCatalog.desired_assets`, which is `desired_in`): what
Prefetch downloads, the cleaner keeps. The sweep is eager, not budget-gated: every release file
whose key is not desired and that is past its grace is removed, whatever the free space. The
cache stays ephemeral: rows, references and produced facts stay, so a key that becomes wanted
again re-fetches by Prefetch or read-through.

* **Scope.** Only `os-images/` (OS images) and `apps/` (sealed environments and Player
  `.deb`s). Never `previews/` (library thumbnails) and never `media/` (`central/media_store.py`).
* **Never a desired file: mark and sweep, no lock.** A run reads the desired set once to pick
  its candidates, then reads it again immediately before each unlink and spares a key that
  became desired meanwhile (the repo's old orphan sweep re-confirmed each owner the same way,
  `docs/module-central-cache.md`). A root committed between that re-read and the unlink costs
  one re-download: files install by atomic `os.replace` and serve from an open descriptor, and
  read-through restores a missing one.
* **Never a file being served.** A file whose mtime is within `SERVE_GRACE` of the run's clock
  marker is spared (a fetch that landed after the read), and so is one whose `last_served_at`
  is within `SERVE_GRACE` of the database's clock (`AssetRecords.served_within`): the reader
  records a serve at every open (at most every 5 minutes per process), so a stream younger than
  55 minutes is never unlinked. An open descriptor keeps its inode on POSIX anyway.
  Precondition: no single stream lasts longer than 55 minutes.
* **Temp files** (`TEMP_PREFIX`) are removed only when idle past `TEMP_GRACE`: a live download
  advances its mtime with every write and fails after 30 s of silence.
* **Bounded.** At most `MAX_UNLINKS_PER_RUN` per run.

**One clock per comparison.** A file's mtime is set by the cache filesystem (an NFS server's
clock, possibly), so it is compared only with another mtime on that filesystem: each run touches
`CLOCK_MARKER` in the cache root first and measures every age against the marker's mtime.
`last_served_at` is written and compared by the database alone. This worker's clock is never
compared with either.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Final

from central.assets.layout import TEMP_PREFIX
from central.assets.store import CacheStore
from central.kernel.assets import AssetKey, AssetKind
from central.kernel.job_types import MaintainCache
from central.kernel.jobs import asset_key
from central.kernel.ports import AssetRecords, ContentCatalog
from central.kernel.transactions import Transactions

LOG = logging.getLogger("central.assets.maintenance")

SERVE_GRACE: Final = timedelta(hours=1)
TEMP_GRACE: Final = timedelta(hours=1)
MAX_UNLINKS_PER_RUN: Final = 50
# Touched in the cache root at the start of every run: its mtime is "now" on the cache
# filesystem's own clock. Not in a swept directory, and no asset name can equal it.
CLOCK_MARKER: Final = ".maintain-cache-clock"
# The release kinds, grouped by the directory they share. `previews/` and `media/` are absent:
# the cleaner never touches them.
RELEASE_KINDS: Final[tuple[tuple[AssetKind, ...], ...]] = (
    (AssetKind.OS_IMAGE,),
    (AssetKind.SEALED_ENVIRONMENT,),
)


@dataclass(frozen=True, slots=True)
class _Candidate:
    path: Path
    key: AssetKey | None  # None for an idle temp file
    older_than: float  # the mtime (cache filesystem clock) the file must still be below


class MaintainCacheHandler:
    """Implements `Handler[MaintainCache, None]`."""

    def __init__(self, *, catalog: ContentCatalog, records: AssetRecords, store: CacheStore,
                 transactions: Transactions) -> None:
        self._catalog = catalog
        self._records = records
        self._store = store
        self._transactions = transactions

    async def handle(self, job: MaintainCache) -> None:
        removed = await self.sweep()
        if removed:
            LOG.info("removed %d unwanted cache file(s): %s", len(removed),
                     ", ".join(path.name for path in removed))

    async def sweep(self) -> list[Path]:
        """One run: the removed paths, at most `MAX_UNLINKS_PER_RUN`."""
        marked = await self._desired_keys()
        candidates = await asyncio.to_thread(self._candidates, marked)
        removed: list[Path] = []
        for candidate in candidates:
            if len(removed) >= MAX_UNLINKS_PER_RUN:
                break
            if candidate.key is not None:
                # The sweep's re-check, immediately before this unlink (module docstring).
                if candidate.key in await self._desired_keys():
                    continue
                if await asyncio.to_thread(self._served_recently, candidate.key):
                    continue
            if await asyncio.to_thread(_unlink_if_older, candidate):
                removed.append(candidate.path)
        return removed

    async def _desired_keys(self) -> frozenset[AssetKey]:
        return frozenset(asset_key(job) for job in await self._catalog.desired_assets())

    def _candidates(self, desired: frozenset[AssetKey]) -> list[_Candidate]:
        """Every release file not in `desired` and every temp file, past its grace by the cache
        filesystem's clock (`CLOCK_MARKER`)."""
        layout = self._store.layout
        now = _filesystem_now(layout.root)
        written_before = now - SERVE_GRACE.total_seconds()
        idle_before = now - TEMP_GRACE.total_seconds()
        found: list[_Candidate] = []
        for kinds in RELEASE_KINDS:
            for path, metadata in _regular_files(layout.directory(kinds[0])):
                if path.name.startswith(TEMP_PREFIX):
                    if metadata.st_mtime < idle_before:
                        found.append(_Candidate(path, None, idle_before))
                    continue
                key = layout.key_of(kinds, path.name)
                if key is not None and key not in desired and metadata.st_mtime < written_before:
                    found.append(_Candidate(path, key, written_before))
        return found

    def _served_recently(self, key: AssetKey) -> bool:
        with self._transactions.begin() as tx:
            return self._records.served_within(tx, key, SERVE_GRACE.total_seconds())


def _filesystem_now(root: Path) -> float:
    """The cache filesystem's clock now: the mtime of `CLOCK_MARKER`, touched just before."""
    marker = root / CLOCK_MARKER
    root.mkdir(parents=True, exist_ok=True)
    marker.touch()  # an existing file gets utime(NULL): the filesystem (server) sets the time
    return os.stat(marker).st_mtime


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


def _unlink_if_older(candidate: _Candidate) -> bool:
    """Unlink the candidate unless it is gone or was rewritten since it was picked (a fetch that
    landed meanwhile installs a new file with a fresh mtime)."""
    try:
        if os.lstat(candidate.path).st_mtime >= candidate.older_than:
            return False
        candidate.path.unlink()
    except FileNotFoundError:
        return False
    return True
