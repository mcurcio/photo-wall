"""Seed a saved Source's pool through the photo library store's own sides (roadmap 4, P1).

The real save path (a draft saved under a name) makes the Source revision; the store's worker
side (`begin_check`, `record_step`, `finish_check`) fills its pool, as a check against Immich
would. No worker, no Immich. Each Source name gets its own query (one album named after it), so
two Sources never share a pool by accident (F-2).
"""

from __future__ import annotations

from collections.abc import Iterable
from hashlib import sha1
from uuid import UUID

from central.db import ProcessTransactionClock
from central.infra.photo_library_store import PgPhotoLibrary
from central.media_queue import QueueReceipt
from central.media_repository import MediaRepository
from central.photo_library.model import InPool, LibraryAsset, SourceQuery
from central.photo_library.ports import CheckComplete, CheckStep
from central.photo_library.views import ConnectionWrite
from central.runtime_store import RuntimeStore
from central.source_names import SourceNameService

FIXTURE_CONNECTION = "fixture"   # its ref and its name, as migration 080 names a cut-over row


class NullLibraryQueue:
    """The store's two queueing calls, accepted and dropped: no worker runs a check here."""

    def enqueue_connection_check_in(self, conn, connection_ref):
        return QueueReceipt(coalesced=False)

    def enqueue_pool_check_in(self, conn, pool_key):
        return QueueReceipt(coalesced=False)


def library_of(registry) -> PgPhotoLibrary:
    return PgPhotoLibrary(registry.db, times=ProcessTransactionClock(registry.clock),
                          queue=NullLibraryQueue())


def fixture_connection(registry) -> str:
    """The test database's one keyed connection `fixture`, added on first use. Its ref is fixed
    (as a cut-over row's, C12) so a photo can be built before the database is touched; the
    operator side then pastes its address and key."""
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO library_connections(connection_ref,name,created_at) "
                     "VALUES(%s,%s,0) ON CONFLICT DO NOTHING",
                     (FIXTURE_CONNECTION, FIXTURE_CONNECTION))
        keyed = conn.execute("SELECT api_key IS NOT NULL AS keyed FROM library_connections "
                             "WHERE connection_ref=%s", (FIXTURE_CONNECTION,)).fetchone()["keyed"]
    if not keyed:
        library_of(registry).change_connection(FIXTURE_CONNECTION, ConnectionWrite(
            name=FIXTURE_CONNECTION, url="http://immich.invalid", key="PUBLIC-FIXTURE-KEY-1234"))
    return FIXTURE_CONNECTION


def _uuid(text: str) -> str:
    return str(UUID(bytes=sha1(text.encode()).digest()[:16]))


def photo(label: str, *, taken_at: float, width: int = 1080, height: int = 1920,
          kind: str = "image") -> LibraryAsset:
    """One unedited photo (or video) of the fixture connection, named by `label`."""
    return LibraryAsset(connection_ref=FIXTURE_CONNECTION, upstream_id=_uuid(label),
                        revision=sha1(label.encode()).hexdigest(), kind=kind, taken_at=taken_at,
                        width=width, height=height, duration=10.0 if kind == "video" else None)


def seed_pool(registry, source_ref: str, photos: Iterable[LibraryAsset], *,
              complete: bool = True) -> str:
    """Make `source_ref` (`<name>:1`) a saved Source on first use, then record one step of its
    pool's check finding `photos`. `complete` finishes the check, so it sweeps whatever an earlier
    check found; otherwise the check stays running and the next call adds to it. `registry` is
    anything with the test database's `db` and `clock` (the registry, a repository). Returns the
    pool key."""
    library = library_of(registry)
    with registry.db.transaction() as conn:
        exists = conn.execute("SELECT 1 FROM media_sources WHERE source_ref=%s",
                              (source_ref,)).fetchone()
    if exists is None:
        name = source_ref.rsplit(":", 1)[0]
        query = SourceQuery(connection_ref=fixture_connection(registry), albums=(_uuid(name),))
        names = SourceNameService(
            registry.db,
            MediaRepository(registry.db, registry.clock,
                            times=ProcessTransactionClock(registry.clock)),
            RuntimeStore(registry.db, registry.clock), library)
        saved = names.save_draft(library.create_draft(query, None), name)
        assert saved["source_ref"] == source_ref, saved
    with registry.db.transaction() as conn:
        key = library.pool_key_of_source_in(conn, source_ref)
    lease = library.begin_check(key)
    assert lease is not None, "the seeded pool is not wanted"
    entries = tuple(InPool(asset=asset) for asset in photos)
    assert library.record_step(lease, CheckStep(progress={"page": 1}, marks=(), entries=entries,
                                                seen_so_far=len(entries), immich_up_to=None))
    if complete:
        assert library.finish_check(lease, CheckComplete())
    return key
