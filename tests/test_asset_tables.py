"""One row per Immich asset, its revisions and copies as children (follow-ups 2026-10-11, slice A2;
run ledger .claude/runs/photo-4.md). Owner, 2026-10-11: "I would build this as a normalized
database, with each upstream immich asset being a primary resource".

Proven through the store's own sides (the worker's `record_step`, `record_copies`, the operator's
pool pages, the Planner's members, `MediaStore.publish`), never by reading the schema. The cases
that hold for both stores (A2 cases 1, 2, 3 and 5) run in tests/test_library_store_conformance.py
against the fake and PostgreSQL; this file holds what only PostgreSQL shows.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from test_media_store import staged, storage  # noqa: F401  (the `storage` fixture)

from central.db import ProcessTransactionClock
from central.infra.photo_library_store import PgPhotoLibrary
from central.media_queue import QueueReceipt
from central.photo_library.model import (
    InPool,
    LeftOut,
    LibraryAsset,
    MeasuredCopy,
    PoolMember,
    SourceQuery,
    Waiting,
)
from central.photo_library.ports import CheckStep
from central.photo_library.views import ConnectionWrite


class _Queue:
    def enqueue_connection_check_in(self, conn, connection_ref):
        return QueueReceipt(coalesced=False)

    def enqueue_pool_check_in(self, conn, pool_key):
        return QueueReceipt(coalesced=False)


@pytest.fixture
def library(registry):
    store = PgPhotoLibrary(registry.db, times=ProcessTransactionClock(registry.clock),
                           queue=_Queue())
    ref = store.add_connection(ConnectionWrite(name="Home", url="http://immich.invalid",
                                               key="PUBLIC-FIXTURE-KEY-1234"))
    return store, ref


def uid(number: int) -> str:
    return str(UUID(int=number))


def photo(ref: str, number: int, taken_at: float, **changes) -> LibraryAsset:
    return LibraryAsset(connection_ref=ref, upstream_id=uid(number), revision=f"{number:040x}",
                        kind="image", taken_at=taken_at, width=4000, height=3000).model_copy(
        update=changes)


def step(*entries) -> CheckStep:
    return CheckStep(progress={}, entries=entries, seen_so_far=len(entries))


def _draft(store, query: SourceQuery) -> tuple[str, str]:
    draft = store.create_draft(query, None)
    with store.db.transaction() as conn:
        return draft, store.pool_of_draft_in(conn, draft)


def test_a_date_edit_in_immich_shows_in_every_pool_holding_the_photo(library):
    """A2 case 1 (the page half): the second pool's check rewrites the one parent, so the first
    pool's page reads the new taken time too. Mutation probe: keep the first taken time on
    conflict (or on a pool row) -> the first pool reads the old time and this fails."""
    store, ref = library
    first_draft, first = _draft(store, SourceQuery(connection_ref=ref))
    second_draft, second = _draft(store, SourceQuery(connection_ref=ref, favourites_only=True))
    store.record_step(store.begin_check(first), step(InPool(asset=photo(ref, 1, 100.0)),
                                                     InPool(asset=photo(ref, 2, 90.0))))
    store.record_step(store.begin_check(second), step(InPool(asset=photo(ref, 1, 80.0))))
    pages = [store.draft_pool(draft, None) for draft in (first_draft, second_draft)]
    assert [[(item.asset_id, item.taken_at) for item in page.items] for page in pages] == [
        [(photo(ref, 2, 0).asset_id, 90.0), (photo(ref, 1, 0).asset_id, 80.0)],
        [(photo(ref, 1, 0).asset_id, 80.0)]]
    with store.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM library_assets").fetchone()["n"] == 2


def test_pages_counts_and_planner_members_read_the_revision_and_its_parent(library, registry):
    """A2 case 4: the page, its counts and the Planner's members read sizes from the revision and
    kind and taken time from the parent; a LeftOut with no parent counts with kind unknown."""
    store, ref = library
    draft, key = _draft(store, SourceQuery(connection_ref=ref))
    video = photo(ref, 3, 70.0, kind="video", duration=12.5, width=1920, height=1080)
    store.record_step(store.begin_check(key), step(
        InPool(asset=photo(ref, 1, 100.0, width=3024, height=4032)), InPool(asset=video),
        Waiting(upstream_id=uid(4), kind="image", taken_at=60.0),
        LeftOut(upstream_id=uid(5), reason="unreadable_answer"),
        LeftOut(upstream_id=uid(6), kind="video", taken_at=50.0, reason="over_item_limits")))
    page = store.draft_pool(draft, None)
    assert [(i.kind, i.taken_at, i.width, i.height) for i in page.items] == [
        ("image", 100.0, 3024, 4032), ("video", 70.0, 1920, 1080)]
    assert (page.in_pool.photos, page.in_pool.videos, page.waiting) == (1, 1, 1)
    assert {(c.reason, c.count) for c in page.left_out} == {("unreadable_answer", 1),
                                                           ("over_item_limits", 1)}
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO media_sources(source_ref,query,pool_key,refresh_active) "
                     "SELECT 'home:1',query,pool_key,TRUE FROM photo_pools WHERE pool_key=%s",
                     (key,))
        members, _ready = PgPhotoLibrary.pool_members_in(conn, {"home:1"})["home:1"]
    assert members == (
        PoolMember(asset_id=photo(ref, 1, 0).asset_id, revision=f"{1:040x}", kind="image",
                   taken_at=100.0, width=3024, height=4032),
        PoolMember(asset_id=video.asset_id, revision=f"{3:040x}", kind="video", taken_at=70.0,
                   width=1920, height=1080))


def test_deleting_a_revision_removes_its_copies(library):
    """A2 case 5 (the cascade): a copy is a child of its revision."""
    store, ref = library
    _draft_ref, key = _draft(store, SourceQuery(connection_ref=ref))
    shown = photo(ref, 1, 100.0)
    store.record_step(store.begin_check(key), step(InPool(asset=shown)))
    store.record_copies(shown.asset_id, [MeasuredCopy(name="thumbnail", media_type="image/webp",
                                                      width=250, height=188)])
    assert list(store.copy_sizes(shown.asset_id)) == ["thumbnail"]
    with store.db.transaction() as conn:
        conn.execute("DELETE FROM photo_pool_rows WHERE asset_id=%s", (shown.asset_id,))
        conn.execute("DELETE FROM asset_revisions WHERE asset_id=%s", (shown.asset_id,))
    assert store.copy_sizes(shown.asset_id) == {}


def test_a_published_copy_records_the_immich_copy_it_was_made_from(storage):  # noqa: F811
    """A2 case 6: `media_jobs.made_from`, written by the UPDATE that sets the variant."""
    with storage.worker_lock():
        lease, _paths, prepared = staged(storage)
        storage.publish(lease, prepared.model_copy(update={"made_from": "preview"}))
    with storage.db.transaction() as conn:
        job = conn.execute("SELECT made_from,variant_sha FROM media_jobs WHERE id=%s",
                           (lease.job_id,)).fetchone()
    assert (job["made_from"], job["variant_sha"]) == ("preview", prepared.variant.sha256)
