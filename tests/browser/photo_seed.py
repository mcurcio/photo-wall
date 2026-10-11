"""Seeding roadmap 4's photo library for the console's browser tests (lane console, K1-K4).

What the media worker would write, written through the store's own worker side
(`PgPhotoLibrary.begin_contact`/`record_contact`, `record_list`, `begin_check`/`record_step`/
`finish_check`), with no worker and no Immich. The store queues its follow-up work on a recording
queue here, so nothing runs it.
"""

from uuid import UUID

from psycopg.types.json import Jsonb
from runtime_fakes import apply_procrastinate_schema

from central.db import DatabaseTransactionClock
from central.infra.photo_library_store import PgPhotoLibrary
from central.media_queue import QueueReceipt
from central.photo_library.model import (
    CheckMark,
    ContactAnswer,
    ImmichUser,
    ImmichVersion,
    InPool,
    LibraryAlbum,
    LibraryAsset,
    LibraryCounts,
    LibraryPerson,
    SourceQuery,
    Waiting,
)
from central.photo_library.ports import CheckComplete, CheckStep
from central.photo_library.views import ConnectionWrite

KEY = "PUBLIC-FIXTURE-KEY-1234"
ANNA = ImmichUser(user_ref="5b0f3c0e-3d1c-4a8a-9f6e-2a8c1f0f7a11", name="Anna", email="anna@example.com")
COUNTS = LibraryCounts(photos=41203, videos=2110, albums=212, people=96)
TESTED = ImmichVersion(major=2, minor=5, patch=6)

__all__ = ["ANNA", "COUNTS", "KEY", "CheckMark", "TESTED", "Waiting"]


class NoQueue:
    """The store's queue in a browser test: what it would queue is recorded, never run."""

    def __init__(self):
        self.connections, self.pools = [], []

    def enqueue_connection_check_in(self, conn, connection_ref):
        self.connections.append(connection_ref)
        return QueueReceipt(coalesced=False)

    def enqueue_pool_check_in(self, conn, pool_key):
        self.pools.append(pool_key)
        return QueueReceipt(coalesced=False)


def library_of(registry) -> PgPhotoLibrary:
    """The store, as the worker sees it, over the test's database (Procrastinate's schema too,
    for the app's own queue)."""
    apply_procrastinate_schema(registry.db.dsn)
    return PgPhotoLibrary(registry.db, times=DatabaseTransactionClock(), queue=NoQueue())


def connected(library: PgPhotoLibrary, name="Family", answer: ContactAnswer | None = None) -> str:
    """A connection whose first check passed (Anna's key, Immich's counts)."""
    ref = library.add_connection(ConnectionWrite(name=name, url="http://immich.invalid:2283", key=KEY))
    library.record_contact(library.begin_contact(ref), answer or ContactAnswer(
        version=TESTED, user=ANNA, counts=COUNTS, full_size_copies=True))
    return ref


def person(number: int, name: str) -> LibraryPerson:
    return LibraryPerson(person_ref=str(UUID(int=0x1000 + number)), name=name)


def album(number: int, name: str, photos=10) -> LibraryAlbum:
    return LibraryAlbum(album_ref=str(UUID(int=0x2000 + number)), name=name, photos=photos)


def listed(library: PgPhotoLibrary, ref: str, *, people=(), albums=()) -> None:
    """The connection's lists as the worker records them (empty lists for the rest)."""
    library.record_list(ref, "people", list(people), None)
    library.record_list(ref, "albums", list(albums), None)
    library.record_list(ref, "places", [], None)
    library.record_list(ref, "tags", [], None)


def asset(ref: str, number: int, taken_at: float, kind="image") -> LibraryAsset:
    return LibraryAsset(connection_ref=ref, upstream_id=str(UUID(int=number)), revision=f"{number:040x}",
                        kind=kind, taken_at=taken_at, width=4000, height=3000,
                        duration=5.0 if kind == "video" else None)


def in_pool(ref: str, numbers, kind="image", newest=1_790_000_000.0):
    """Pool entries for `numbers`, one day apart, the lowest number newest."""
    return [InPool(asset=asset(ref, n, newest - n * 86400, kind)) for n in numbers]


def draft_pool_key(registry, draft_ref: str | None = None) -> str:
    """The pool of the newest draft (or of `draft_ref`)."""
    with registry.db.transaction() as conn:
        row = conn.execute(
            "SELECT pool_key FROM photo_drafts WHERE (%s::uuid IS NULL OR draft_ref=%s::uuid) "
            "ORDER BY created_at DESC LIMIT 1", (draft_ref, draft_ref)).fetchone()
    assert row is not None, "no draft yet"
    return row["pool_key"]


def drafts(registry) -> list[dict]:
    with registry.db.transaction() as conn:
        return conn.execute("SELECT d.draft_ref,d.pool_key,p.query FROM photo_drafts d "
                            "JOIN photo_pools p USING (pool_key) ORDER BY d.created_at").fetchall()


def begin(library: PgPhotoLibrary, pool_key: str):
    lease = library.begin_check(pool_key)
    assert lease is not None, "the pool is not wanted, or its connection has no key"
    return lease


def step(library: PgPhotoLibrary, lease, entries, *, seen: int, up_to: int | None = None) -> None:
    assert library.record_step(lease, CheckStep(progress={"seen": seen}, entries=tuple(entries),
                                                seen_so_far=seen, immich_up_to=up_to))


def finish(library: PgPhotoLibrary, lease, finish=None) -> None:
    assert library.finish_check(lease, finish or CheckComplete())


def filled(library: PgPhotoLibrary, pool_key: str, entries, finish_with=None) -> None:
    """One whole check: every entry in one step, then its finish (complete by default)."""
    lease = begin(library, pool_key)
    step(library, lease, entries, seen=len(entries), up_to=len(entries))
    finish(library, lease, finish_with)


def saved_source(registry, library: PgPhotoLibrary, name: str, query: SourceQuery) -> str:
    """A saved Photo source (`name:1`) of `query` and its pool, wanted as Runtime's are."""
    draft = library.create_draft(query, None)
    with registry.db.transaction() as conn:
        pool_key = library.pool_of_draft_in(conn, draft)
        conn.execute("INSERT INTO media_sources(source_ref,query,pool_key,refresh_active) "
                     "VALUES(%s,%s,%s,TRUE)",
                     (f"{name}:1", Jsonb(query.model_dump(mode="json")), pool_key))
        conn.execute("INSERT INTO media_source_names(name,current_ref,revision) VALUES(%s,%s,1)",
                     (name, f"{name}:1"))
        conn.execute("INSERT INTO media_source_name_versions(source_ref,name,revision) VALUES(%s,%s,1)",
                     (f"{name}:1", name))
        library.remove_draft_in(conn, draft)
    return f"{name}:1"
