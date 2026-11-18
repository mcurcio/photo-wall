"""Pools keyed by their query, drafts, and the store half of a check (roadmap 4, slice C2; run
ledger .claude/runs/photo-4.md, C2 cases 1-11).

The worker side is fed by hand-made `CheckStep` values: a check adds what it confirms, step by
step, fenced by its number, and removes only when it completes (design rule 2). No Immich.
"""

from __future__ import annotations

import asyncio
import time
from uuid import UUID

import pytest
from psycopg.types.json import Jsonb
from test_library_checks import T0 as LIBRARY_T0
from test_library_checks import FakeClient
from test_library_checks import photo as library_photo
from test_photo_library_store import RecordingLibraryQueue, store_of

from central.infra.photo_library_store import library_assets_in
from central.photo_library.model import (
    CheckMark,
    Exclusions,
    InPool,
    LeftOut,
    LibraryAsset,
    OriginalFile,
    PoolCursor,
    SourceQuery,
    TakenLast,
    Waiting,
)
from central.photo_library.ports import (
    CheckComplete,
    CheckFailed,
    CheckIncomplete,
    CheckStep,
    KnownRevision,
    LibraryRefused,
)
from central.photo_library.views import ConnectionWrite
from contracts.time import ManualClock
from media.library_checks import LibraryChecks


def asset(store_ref, number, taken_at, kind="image"):
    return LibraryAsset(connection_ref=store_ref, upstream_id=str(UUID(int=number)),
                        revision=f"{number:040x}", kind=kind, taken_at=taken_at, width=4000,
                        height=3000, duration=5.0 if kind == "video" else None)


def step(*entries, marks=(), seen=0, up_to=None):
    return CheckStep(progress={"page": seen}, marks=marks, entries=entries, seen_so_far=seen,
                     immich_up_to=up_to)


@pytest.fixture
def pools(registry):
    queue = RecordingLibraryQueue()
    store = store_of(registry.db, queue)
    ref = store.add_connection(ConnectionWrite(name="Home", url="http://immich.invalid",
                                               key="PUBLIC-FIXTURE-KEY-1234"))
    return store, queue, ref


def _draft_pool(store, draft):
    with store.db.transaction() as conn:
        return store.pool_of_draft_in(conn, draft)


def _rows(store, key):
    with store.db.transaction() as conn:
        return {row["upstream_id"]: row for row in conn.execute(
            "SELECT upstream_id::text AS upstream_id,outcome,reason,asset_id,created_at,"
            "changed_at,created_by,changed_by "
            "FROM photo_pool_rows WHERE pool_key=%s", (key,)).fetchall()}


def _pool(store, key):
    with store.db.transaction() as conn:
        return conn.execute("SELECT * FROM photo_pools WHERE pool_key=%s", (key,)).fetchone()


def _pages(store):
    """Every stored page: (pool, check number, kind, its ids sorted)."""
    with store.db.transaction() as conn:
        return [(row["pool_key"], row["check_number"], row["kind"], sorted(row["ids"]))
                for row in conn.execute(
                    "SELECT pool_key,check_number,kind,upstream_ids::text[] AS ids "
                    "FROM photo_check_pages ORDER BY id").fetchall()]


def _save_source(store, source_ref, query, key, *, active=True):
    with store.db.transaction() as conn:
        conn.execute("INSERT INTO media_sources(source_ref,query,pool_key,refresh_active) "
                     "VALUES(%s,%s,%s,%s)",
                     (source_ref, Jsonb(query.model_dump(mode="json")), key, active))


def _age(store, table, column, key_column, key, seconds):
    with store.db.transaction() as conn:
        conn.execute(f"UPDATE {table} SET {column}=EXTRACT(EPOCH FROM clock_timestamp())-%s "
                     f"WHERE {key_column}=%s", (seconds, key))


def test_equal_queries_share_one_pool_and_a_new_query_moves_the_draft(pools):
    """Case 1."""
    store, queue, ref = pools
    query = SourceQuery(connection_ref=ref, taken=TakenLast(years=3))
    first = store.create_draft(query, None)
    second = store.create_draft(SourceQuery.model_validate(query.model_dump()), None)
    assert _draft_pool(store, first) == _draft_pool(store, second) == query.pool_key()
    assert queue.pools == [query.pool_key()] * 2
    other = SourceQuery(connection_ref=ref, favourites_only=True)
    store.set_draft_query(second, other)
    assert _draft_pool(store, second) == other.pool_key() and queue.pools[-1] == other.pool_key()
    with pytest.raises(LibraryRefused) as unknown:
        store.set_draft_query(str(UUID(int=7)), other)
    assert unknown.value.code == "draft_unknown"
    with pytest.raises(LibraryRefused) as gone:
        store.create_draft(SourceQuery(connection_ref="library-000000000000"), None)
    assert gone.value.code == "connection_unknown"


def test_a_superseded_lease_writes_nothing(pools):
    """Case 2. Mutation probe: drop the check-number fence."""
    store, _, ref = pools
    key = _draft_pool(store, store.create_draft(SourceQuery(connection_ref=ref), None))
    store.finish_check(store.begin_check(key), CheckComplete())
    second = store.begin_check(key)
    assert second.check_number == 2
    store.finish_check(second, CheckComplete())
    third = store.begin_check(key)
    assert third.check_number == 3
    assert store.record_step(second, step(InPool(asset=asset(ref, 1, 100.0)), seen=1)) is False
    assert store.finish_check(second, CheckFailed(problem="connection")) is False
    assert _rows(store, key) == {} and _pool(store, key)["check_state"] == "checking"
    assert store.begin_check(key).check_number == 3   # resumes the running check


def test_a_step_adds_its_rows_except_marked_ids_and_queues_the_next_in_one_transaction(pools):
    """Case 3. Mutation probes: let a marked entry in; drop `updated_at` from the upsert."""
    store, queue, ref = pools
    key = _draft_pool(store, store.create_draft(SourceQuery(connection_ref=ref), None))
    lease = store.begin_check(key)
    excluded, copy = str(UUID(int=3)), str(UUID(int=4))
    queued = len(queue.pools)
    assert store.record_step(lease, step(marks=(CheckMark(upstream_id=excluded, mark="excluded"),),
                                         seen=0, up_to=10)) is True
    edited_at = "2026-10-01T12:00:00.000Z"
    assert store.record_step(lease, step(
        InPool(asset=asset(ref, 1, 100.0), updated_at=edited_at),
        Waiting(upstream_id=str(UUID(int=2)), kind="image", taken_at=90.0),
        InPool(asset=asset(ref, 3, 80.0)),                          # marked by an earlier step
        InPool(asset=asset(ref, 4, 70.0)),                          # marked in this step
        LeftOut(upstream_id=str(UUID(int=5)), reason="unreadable_answer"),
        marks=(CheckMark(upstream_id=copy, mark="stacked_copy"),), seen=5)) is True
    assert len(queue.pools) == queued + 2
    rows = _rows(store, key)
    assert sorted(rows) == [str(UUID(int=n)) for n in (1, 2, 5)]
    assert {(row["created_by"], row["changed_by"]) for row in rows.values()} == {
        ("library-check", "library-check")}
    assert rows[str(UUID(int=5))]["reason"] == "unreadable_answer"
    with store.db.transaction() as conn:
        stored = conn.execute("SELECT connection_ref,upstream_id::text AS upstream_id,"
                              "updated_at FROM asset_revisions WHERE asset_id=%s",
                              (asset(ref, 1, 100.0).asset_id,)).fetchone()
        leased = library_assets_in(conn, [asset(ref, 1, 100.0).asset_id])
        marks = conn.execute("SELECT u::text AS id,kind AS mark FROM photo_check_pages, "
                             "unnest(upstream_ids) AS u WHERE kind<>'seen' ORDER BY kind").fetchall()
    assert (stored["connection_ref"], stored["upstream_id"]) == (ref, str(UUID(int=1)))
    assert leased == {asset(ref, 1, 100.0).asset_id: asset(ref, 1, 100.0)}
    assert stored["updated_at"] == edited_at
    assert store.known_revisions(ref, [str(UUID(int=1)), str(UUID(int=2))]) == {
        str(UUID(int=1)): KnownRevision(updated_at=edited_at, revision=f"{1:040x}")}
    moved_at = "2026-10-02T12:00:00.000Z"   # a later read of the same revision moves it
    assert store.record_step(lease, step(InPool(asset=asset(ref, 1, 100.0), updated_at=moved_at),
                                         seen=5)) is True
    assert store.known_revisions(ref, [str(UUID(int=1))])[str(UUID(int=1))].updated_at == moved_at
    assert [(m["id"], m["mark"]) for m in marks] == [(excluded, "excluded"), (copy, "stacked_copy")]
    pool = _pool(store, key)
    assert (pool["seen_so_far"], pool["immich_up_to"], pool["progress"]) == (5, 10, {"page": 5})

    queue.fail = True
    with pytest.raises(RuntimeError):
        store.record_step(lease, step(InPool(asset=asset(ref, 6, 60.0)), seen=6))
    assert str(UUID(int=6)) not in _rows(store, key) and _pool(store, key)["seen_so_far"] == 5


def test_only_a_complete_check_sweeps(pools):
    """Case 4: a row the check did not see leaves through its re-check (follow-up S4), and a
    finish leaves no page of its check. Mutation probe: sweep on an incomplete finish."""
    store, _, ref = pools
    key = _draft_pool(store, store.create_draft(SourceQuery(connection_ref=ref), None))
    first = store.begin_check(key)
    store.record_step(first, step(*(InPool(asset=asset(ref, n, 100.0 + n)) for n in (1, 2, 3))))
    store.finish_check(first, CheckComplete())
    completed_at = _pool(store, key)["last_complete_at"]
    for finish in (CheckIncomplete(reason="over_1000_at_one_instant"),
                   CheckFailed(problem="connection")):
        lease = store.begin_check(key)
        store.record_step(lease, step(InPool(asset=asset(ref, 1, 101.0))))
        assert store.finish_check(lease, finish) is True
        assert sorted(_rows(store, key)) == [str(UUID(int=n)) for n in (1, 2, 3)]
        assert _pool(store, key)["last_complete_at"] == completed_at
    lease = store.begin_check(key)
    store.record_step(lease, step(InPool(asset=asset(ref, 1, 101.0)),
                                  marks=(CheckMark(upstream_id=str(UUID(int=9)), mark="excluded"),)))
    for unseen in store.unseen_rows(lease, 10):
        store.record_recheck(lease, unseen, False)
    store.finish_check(lease, CheckComplete())
    assert sorted(_rows(store, key)) == [str(UUID(int=1))]
    assert _pool(store, key)["last_complete_at"] > completed_at
    assert _pages(store) == []


def _all_pages(store, draft, after=None):
    seen, page = [], store.draft_pool(draft, after)
    seen.extend(item.asset_id for item in page.items)
    while page.next is not None:
        page = store.draft_pool(draft, PoolCursor.decode(page.next))
        seen.extend(item.asset_id for item in page.items)
    return seen


def test_pages_are_newest_first_and_a_cursor_survives_adds_and_sweeps(pools):
    """Case 5. Mutation probe: order pages by taken_at alone."""
    store, _, ref = pools
    draft = store.create_draft(SourceQuery(connection_ref=ref), None)
    key = _draft_pool(store, draft)
    lease = store.begin_check(key)
    assets = [asset(ref, n, 1000.0 + n // 3) for n in range(1, 151)]   # three per instant
    store.record_step(lease, step(*(InPool(asset=a) for a in assets)))
    store.finish_check(lease, CheckComplete())
    expected = [a.asset_id for a in sorted(assets, key=lambda a: (a.taken_at, a.asset_id),
                                           reverse=True)]
    first = store.draft_pool(draft, None)
    assert len(first.items) == 60 and [i.asset_id for i in first.items] == expected[:60]
    second = store.draft_pool(draft, PoolCursor.decode(first.next))
    third = store.draft_pool(draft, PoolCursor.decode(second.next))
    assert (len(second.items), len(third.items), third.next) == (60, 30, None)
    assert [i.asset_id for i in (*first.items, *second.items, *third.items)] == expected
    assert first.items[0].thumbnail == "/v1/operator/library/thumbnails/" + expected[0]

    cursor = PoolCursor.decode(first.next)
    swept = assets[0]                                   # the oldest: after the cursor
    lease = store.begin_check(key)
    newer = asset(ref, 500, 5000.0)
    store.record_step(lease, step(InPool(asset=newer),
                                  *(InPool(asset=a) for a in assets if a is not swept)))
    assert store.unseen_rows(lease, 10) == (swept.upstream_id,)
    store.record_recheck(lease, swept.upstream_id, False)
    store.finish_check(lease, CheckComplete())
    rest = _all_pages(store, draft, cursor)
    assert rest == [a for a in expected[60:] if a != swept.asset_id]
    with pytest.raises(ValueError):
        PoolCursor.decode("garbage")


def test_counts_by_outcome(pools):
    """Case 6."""
    store, _, ref = pools
    draft = store.create_draft(SourceQuery(connection_ref=ref), None)
    lease = store.begin_check(_draft_pool(store, draft))
    store.record_step(lease, step(
        InPool(asset=asset(ref, 1, 1.0)), InPool(asset=asset(ref, 2, 2.0, "video")),
        InPool(asset=asset(ref, 3, 3.0)),
        Waiting(upstream_id=str(UUID(int=4)), kind="image", taken_at=4.0),
        LeftOut(upstream_id=str(UUID(int=5)), reason="unreadable_answer"),
        LeftOut(upstream_id=str(UUID(int=6)), reason="over_item_limits"),
        LeftOut(upstream_id=str(UUID(int=7)), reason="over_item_limits"), seen=7, up_to=9))
    view = store.draft_pool(draft, None)
    assert (view.in_pool.photos, view.in_pool.videos, view.waiting) == (2, 1, 1)
    assert [(c.reason, c.count) for c in view.left_out] == [("over_item_limits", 2),
                                                            ("unreadable_answer", 1)]
    assert view.immich_up_to == 9
    assert view.check.state == "checking" and view.check.seen_so_far == 7
    # The Planner reads the in-pool photos only: never one Waiting or Left out, which the
    # preview does not show either. Mutation probe: drop pool_members_in's outcome filter.
    _save_source(store, "family:1", SourceQuery(connection_ref=ref), _draft_pool(store, draft))
    with store.db.transaction() as conn:
        members, _ = store.pool_members_in(conn, {"family:1"})["family:1"]
    assert [member.asset_id for member in members] == [
        asset(ref, 3, 3.0).asset_id, asset(ref, 2, 2.0, "video").asset_id,
        asset(ref, 1, 1.0).asset_id]


def test_no_ceiling_25000_photos_page_count_and_reach_the_planner(pools):
    """Case 7 (owner 2026-10-09: no limit on the count of a Source)."""
    store, _, ref = pools
    query = SourceQuery(connection_ref=ref)
    draft = store.create_draft(query, None)
    key = _draft_pool(store, draft)
    lease = store.begin_check(key)
    for page in range(25):
        assert store.record_step(lease, step(*(InPool(asset=asset(ref, page * 1000 + n,
                                                                  float(page * 1000 + n)))
                                               for n in range(1, 1001)), seen=(page + 1) * 1000))
    store.finish_check(lease, CheckComplete())
    view = store.draft_pool(draft, None)
    assert view.in_pool.photos == 25000 and view.check.state == "complete"
    assert len(_all_pages(store, draft)) == 25000
    _save_source(store, "family:1", query, key)
    with store.db.transaction() as conn:
        members = store.pool_members_in(conn, {"family:1", "unknown:1"})
    assert len(members["family:1"][0]) == 25000 and members["family:1"][1] is True
    assert members["family:1"][0][0].taken_at == 25000.0
    assert members["unknown:1"] == ((), False)


def test_retirement_keeps_wanted_pools_and_their_thumbnails_only(pools):
    """Case 8. Mutation probe: treat a shared pool as unwanted."""
    store, _, ref = pools
    shared_query, lone_query = SourceQuery(connection_ref=ref), SourceQuery(connection_ref=ref,
                                                                           favourites_only=True)
    shared = store.create_draft(shared_query, None)
    lone = store.create_draft(lone_query, None)
    fresh = store.create_draft(SourceQuery(connection_ref=ref, partners=True), None)
    _save_source(store, "family:1", shared_query, shared_query.pool_key())
    for draft in (shared, lone):
        _age(store, "photo_drafts", "created_at", "draft_ref", draft, 86400)
    for key, number in ((shared_query.pool_key(), 1), (lone_query.pool_key(), 2)):
        lease = store.begin_check(key)
        store.record_step(lease, step(InPool(asset=asset(ref, number, 1.0))))
    with store.db.transaction() as conn:
        for number in (1, 2, 3):
            conn.execute("INSERT INTO assets(kind,identity,created_at,created_by,changed_at,changed_by) "
                         "VALUES('library-thumbnail',%s,0,'central',0,'central')",
                         (asset(ref, number, 1.0).asset_id,))
    kept = store.retire_unwanted()
    assert kept == frozenset({asset(ref, 1, 1.0).asset_id})
    with store.db.transaction() as conn:
        drafts = {row["draft_ref"] for row in conn.execute(
            "SELECT draft_ref::text AS draft_ref FROM photo_drafts").fetchall()}
        keys = {row["pool_key"] for row in conn.execute("SELECT pool_key FROM photo_pools")}
    assert drafts == {fresh}
    assert keys == {shared_query.pool_key(), _draft_pool(store, fresh)}


def test_due_pools_by_the_database_clock(pools):
    """Case 9 (run 2: the staleness rule). Mutation probe: skip every checking pool again."""
    store, _, ref = pools
    key = _draft_pool(store, store.create_draft(SourceQuery(connection_ref=ref), None))
    assert store.due_pools() == (key,)                            # requested
    lease = store.begin_check(key)
    assert store.due_pools() == ()                                # its step just began
    assert store.record_step(lease, step(InPool(asset=asset(ref, 1, 100.0)), seen=1)) is True
    _age(store, "photo_pools", "step_at", "pool_key", key, 899)
    assert store.due_pools() == ()
    _age(store, "photo_pools", "step_at", "pool_key", key, 900)   # its step was lost
    assert store.due_pools() == (key,)
    resumed = store.begin_check(key)
    assert (resumed.check_number, resumed.progress) == (lease.check_number, {"page": 1})
    assert store.due_pools() == ()                                # step_at moved
    lease = resumed
    store.finish_check(lease, CheckComplete())
    _age(store, "photo_pools", "check_finished_at", "pool_key", key, 899)
    assert store.due_pools() == ()
    _age(store, "photo_pools", "check_finished_at", "pool_key", key, 900)
    assert store.due_pools() == (key,)
    _age(store, "photo_pools", "check_finished_at", "pool_key", key, 0)
    with store.db.transaction() as conn:
        conn.execute("UPDATE photo_pools SET check_requested=TRUE")
    assert store.due_pools() == (key,)


def test_a_thumbnail_is_served_only_from_a_wanted_pool(pools):
    """Case 10."""
    store, _, ref = pools
    draft = store.create_draft(SourceQuery(connection_ref=ref), None)
    key = _draft_pool(store, draft)
    lease = store.begin_check(key)
    photo = asset(ref, 1, 1.0)
    store.record_step(lease, step(InPool(asset=photo),
                                  Waiting(upstream_id=str(UUID(int=2)), kind="image", taken_at=1.0)))
    found = store.pool_thumbnail(photo.asset_id)
    assert (found.connection_ref, found.upstream_id, found.revision) == (
        ref, photo.upstream_id, photo.revision)
    assert store.pool_thumbnail("asset-" + "0" * 64) is None
    with store.db.transaction() as conn:
        conn.execute("DELETE FROM photo_drafts")
    assert store.pool_thumbnail(photo.asset_id) is None


def test_a_pre_4_source_gets_its_pool_key_once(pools):
    """Case 11."""
    store, queue, ref = pools
    query = SourceQuery(connection_ref=ref, favourites_only=True)
    _save_source(store, "family:1", query, None)
    with store.db.transaction() as conn:
        assert store.check_state_in(conn, "family:1").state == "never_checked"
        assert store.pool_key_of_source_in(conn, "family:1") == query.pool_key()
        assert store.pool_key_of_source_in(conn, "family:1") == query.pool_key()
        assert conn.execute("SELECT count(*) AS n FROM photo_pools").fetchone()["n"] == 1
        stored = conn.execute("SELECT pool_key FROM media_sources").fetchone()["pool_key"]
        assert store.pool_key_of_source_in(conn, "missing:1") is None
    assert stored == query.pool_key()
    assert store.due_pools() == (query.pool_key(),)


def test_due_pools_assigns_wanted_pre_4_sources(pools):
    store, _, ref = pools
    query = SourceQuery(connection_ref=ref, partners=True)
    _save_source(store, "old:1", query, None)
    _save_source(store, "dormant:1", SourceQuery(connection_ref=ref, favourites_only=True), None,
                 active=False)
    assert store.due_pools() == (query.pool_key(),)


# --- A pass writes what changed, plus one page per step for what it saw (decision 0033) ---------

_VERSIONED = {"library_assets": "upstream_id::text", "asset_revisions": "asset_id",
              "photo_pool_rows": "upstream_id::text"}


def _versions(store):
    """Each row of the three pool-check tables by its key: (ctid, xmin, xmax). A write moves its
    ctid and xmin; a row lock (an upsert's `DO UPDATE ... WHERE false` included) sets its xmax."""
    with store.db.transaction() as conn:
        return {table: {row["key"]: (row["ctid"], row["xmin"], row["xmax"]) for row in conn.execute(
            f"SELECT {key} AS key,ctid::text AS ctid,xmin::text AS xmin,xmax::text AS xmax "  # noqa: S608
            f"FROM {table}").fetchall()} for table, key in _VERSIONED.items()}


def _audit(store, table, where, key):
    with store.db.transaction() as conn:
        return conn.execute(f"SELECT created_at,changed_at,created_by,changed_by FROM {table} "  # noqa: S608
                            f"WHERE {where}=%s", (key,)).fetchone()


_ORIGINAL = OriginalFile(media_type="image/jpeg", width=4032, height=3024, orientation=6,
                         size_bytes=3_456_789)


def _library(ref):
    """One step's entries over a small library: two photos read in full, a waiting and a
    left-out photo."""
    return (InPool(asset=asset(ref, 1, 100.0).model_copy(update={"original": _ORIGINAL}),
                   updated_at="2026-10-01T00:00:00.000Z"),
            InPool(asset=asset(ref, 2, 90.0, "video"), updated_at="2026-10-02T00:00:00.000Z"),
            Waiting(upstream_id=str(UUID(int=3)), kind="image", taken_at=80.0),
            LeftOut(upstream_id=str(UUID(int=4)), reason="unreadable_answer"))


def _pass(store, key, *entries, marks=()):
    lease = store.begin_check(key)
    assert store.record_step(lease, step(*entries, marks=marks, seen=len(entries))) is True
    assert store.unseen_rows(lease, 10) == ()
    assert store.finish_check(lease, CheckComplete()) is True


def test_a_pass_over_an_unchanged_library_writes_no_row_and_leaves_no_page(pools):
    """Decision 0033, rule 1: a photo whose facts are as stored costs no write and no row lock.
    Mutation probe: put back the plain upsert -> every row's ctid and xmin move."""
    store, _, ref = pools
    key = _draft_pool(store, store.create_draft(SourceQuery(connection_ref=ref), None))
    _pass(store, key, *_library(ref))
    before = _versions(store)
    assert {table: len(rows) for table, rows in before.items()} == {
        "library_assets": 3, "asset_revisions": 2, "photo_pool_rows": 4}
    _pass(store, key, *_library(ref))
    assert _versions(store) == before
    assert _pages(store) == []
    assert sorted(_rows(store, key)) == [str(UUID(int=n)) for n in (1, 2, 3, 4)]


def test_a_changed_capture_time_rewrites_only_its_photos_row(pools):
    """Mutation probe: leave capture time out of the comparison -> its row is not rewritten."""
    store, _, ref = pools
    key = _draft_pool(store, store.create_draft(SourceQuery(connection_ref=ref), None))
    _pass(store, key, *_library(ref))
    before = _versions(store)
    first = _audit(store, "library_assets", "upstream_id", str(UUID(int=1)))
    moved = list(_library(ref))
    moved[0] = moved[0].model_copy(update={"asset": moved[0].asset.model_copy(
        update={"taken_at": 50.0})})
    _pass(store, key, *moved)
    after = _versions(store)
    photo = str(UUID(int=1))
    assert after["library_assets"][photo] != before["library_assets"][photo]
    assert {k: v for k, v in after["library_assets"].items() if k != photo} == {
        k: v for k, v in before["library_assets"].items() if k != photo}
    assert after["asset_revisions"] == before["asset_revisions"]
    assert after["photo_pool_rows"] == before["photo_pool_rows"]
    audit = _audit(store, "library_assets", "upstream_id", photo)
    assert audit["created_at"] == first["created_at"] and audit["changed_at"] > first["changed_at"]
    assert (audit["created_by"], audit["changed_by"]) == ("library-check", "library-check")
    with store.db.transaction() as conn:
        assert library_assets_in(conn, [asset(ref, 1, 50.0).asset_id])[
            asset(ref, 1, 50.0).asset_id].taken_at == 50.0


def test_a_newly_excluded_photo_leaves_at_completion_with_one_page_not_a_row_per_id(pools):
    """Mutation probe: skip the mark pages in finish_check -> the excluded photos stay."""
    store, _, ref = pools
    key = _draft_pool(store, store.create_draft(SourceQuery(connection_ref=ref), None))
    photos = [InPool(asset=asset(ref, n, 100.0 + n)) for n in range(1, 6)]
    _pass(store, key, *photos)
    lease = store.begin_check(key)
    excluded = sorted(str(UUID(int=n)) for n in (2, 3))
    assert store.record_step(lease, step(*photos, marks=tuple(
        CheckMark(upstream_id=ref_id, mark="excluded") for ref_id in excluded), seen=5))
    assert _pages(store) == [
        (key, lease.check_number, "excluded", excluded),
        (key, lease.check_number, "seen", sorted(str(UUID(int=n)) for n in (1, 4, 5)))]
    assert store.unseen_rows(lease, 10) == ()
    assert store.finish_check(lease, CheckComplete()) is True
    assert sorted(_rows(store, key)) == [str(UUID(int=n)) for n in (1, 4, 5)]
    assert _pages(store) == []


def test_starting_a_check_deletes_the_pages_an_unfinished_one_left(pools):
    store, _, ref = pools
    key = _draft_pool(store, store.create_draft(SourceQuery(connection_ref=ref), None))
    abandoned = store.begin_check(key)
    store.record_step(abandoned, step(InPool(asset=asset(ref, 1, 1.0)), seen=1))
    with store.db.transaction() as conn:     # its worker died; a migration or reset ended it
        conn.execute("UPDATE photo_pools SET check_state='never_checked' WHERE pool_key=%s",
                     (key,))
    fresh = store.begin_check(key)
    assert fresh.check_number == abandoned.check_number + 1
    assert _pages(store) == []
    assert store.unseen_rows(fresh, 10) == (str(UUID(int=1)),)


class _AskedClient(FakeClient):
    """The fake Immich library, recording the id each Re-check asks about."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.asked: list[str] = []

    async def still_matches(self, query, window, upstream_id):
        self.asked.append(upstream_id)
        return await super().still_matches(query, window, upstream_id)


def _check_to_end(checks, store, key):
    asyncio.run(checks.check_pool(key))
    for _ in range(1000):
        if _pool(store, key)["check_state"] != "checking":
            return
        asyncio.run(checks.check_pool(key))
    raise AssertionError("the check never finished")


def test_a_photo_that_left_immich_is_asked_about_alone_and_removed_by_the_next_pass(pools):
    """The worker's checks over PostgreSQL: a photo gone from Immich is in no `seen` page, so
    the Re-check asks about it alone and removes it. Mutation probe: record no `seen` page for
    one step -> its photos are asked about too."""
    store, _, ref = pools
    client = _AskedClient([library_photo(n) for n in range(1, 6)])
    query = SourceQuery(connection_ref=ref, exclude=Exclusions(screenshots=False))
    key = _draft_pool(store, store.create_draft(query, None))
    checks = LibraryChecks(store, lambda secret: client, ManualClock(wall=LIBRARY_T0, mono=0))
    _check_to_end(checks, store, key)
    assert len(_rows(store, key)) == 5 and client.asked == []
    before = _versions(store)
    gone = client.photos.pop(0).upstream_id
    _check_to_end(checks, store, key)
    assert client.asked == [gone]
    assert sorted(_rows(store, key)) == sorted(str(UUID(int=n)) for n in range(2, 6))
    after = _versions(store)
    assert gone not in after["photo_pool_rows"]
    assert after["photo_pool_rows"] == {k: v for k, v in before["photo_pool_rows"].items()
                                        if k != gone}
    assert after["library_assets"] == before["library_assets"]
    assert _pages(store) == []


def test_unseen_rows_and_the_finish_read_25_pages_against_21000_rows_within_a_second(pools):
    """Mutation probe: probe the pages once per row (`= ANY(page)`) -> over a second."""
    store, _, ref = pools
    key = _draft_pool(store, store.create_draft(SourceQuery(connection_ref=ref), None))
    lease = store.begin_check(key)
    for page in range(21):
        assert store.record_step(lease, step(*(Waiting(
            upstream_id=str(UUID(int=page * 1000 + n)), kind="image", taken_at=1.0)
            for n in range(1000)), seen=(page + 1) * 1000))
    store.finish_check(lease, CheckComplete())
    lease = store.begin_check(key)
    for page in range(20):                     # 20 seen pages: 20,000 of the 21,000 rows
        assert store.record_step(lease, step(*(Waiting(
            upstream_id=str(UUID(int=page * 1000 + n)), kind="image", taken_at=1.0)
            for n in range(1000)), seen=(page + 1) * 1000))
    for page in range(5):                      # 5 mark pages: 100 rows each, the rest unknown
        assert store.record_step(lease, step(marks=tuple(CheckMark(
            upstream_id=str(UUID(int=(20_000 + page * 100 + n) if n < 100
                                 else (100_000 + page * 1000 + n))), mark="excluded")
            for n in range(1000)), seen=21_000))
    assert len(_pages(store)) == 25
    started = time.perf_counter()
    unseen = store.unseen_rows(lease, 1000)
    unseen_took = time.perf_counter() - started
    assert unseen == tuple(sorted(str(UUID(int=n)) for n in range(20_500, 21_000)))
    started = time.perf_counter()
    assert store.finish_check(lease, CheckComplete()) is True
    finish_took = time.perf_counter() - started
    assert len(_rows(store, key)) == 20_500
    assert unseen_took < 1.0 and finish_took < 1.0, (unseen_took, finish_took)


def test_a_waiting_photo_read_in_full_later_gets_its_originals_facts_once(pools):
    """A photo first stored while Waiting has no original's facts; read in full later with the
    same kind and taken time, its row is rewritten with them, and a pass with the same facts
    again writes nothing. Mutation probe: leave the original's facts out of the comparison ->
    the row never gets them."""
    store, _, ref = pools
    key = _draft_pool(store, store.create_draft(SourceQuery(connection_ref=ref), None))
    photo = str(UUID(int=1))
    _pass(store, key, Waiting(upstream_id=photo, kind="image", taken_at=100.0))
    waiting = _versions(store)["library_assets"][photo]
    full = InPool(asset=asset(ref, 1, 100.0).model_copy(update={"original": _ORIGINAL}),
                  updated_at="2026-10-01T00:00:00.000Z")
    _pass(store, key, full)
    read = _versions(store)
    assert read["library_assets"][photo] != waiting
    with store.db.transaction() as conn:
        assert library_assets_in(conn, [full.asset.asset_id])[full.asset.asset_id].original == (
            _ORIGINAL)
    _pass(store, key, full)
    assert _versions(store) == read


def _with(entries, index, entry):
    return tuple(entry if n == index else e for n, e in enumerate(entries))


@pytest.mark.parametrize(("fact", "table", "number", "changed"), [
    ("a left-out photo's reason", "photo_pool_rows", 4,
     lambda e: e.model_copy(update={"reason": "over_item_limits"})),
    ("a photo's kind", "library_assets", 3, lambda e: e.model_copy(update={"kind": "video"})),
    ("a revision's width", "asset_revisions", 2,
     lambda e: e.model_copy(update={"asset": e.asset.model_copy(update={"width": 3840})})),
    ("a revision's height", "asset_revisions", 2,
     lambda e: e.model_copy(update={"asset": e.asset.model_copy(update={"height": 2160})})),
    ("a revision's duration", "asset_revisions", 2,
     lambda e: e.model_copy(update={"asset": e.asset.model_copy(update={"duration": 7.5})})),
])
def test_each_compared_fact_rewrites_only_its_own_row(pools, fact, table, number, changed):
    """Each fact a pass compares, changed alone, rewrites its one row and nothing else.
    Mutation probe: drop that fact from its comparison (`_POOL_ROW_FACTS`, `_asset_differs`,
    `_REVISION_FACTS`) -> its row keeps the old value."""
    store, _, ref = pools
    key = _draft_pool(store, store.create_draft(SourceQuery(connection_ref=ref), None))
    library = _library(ref)
    _pass(store, key, *library)
    before = _versions(store)
    index = number - 1
    _pass(store, key, *_with(library, index, changed(library[index])))
    after = _versions(store)
    row = (asset(ref, 2, 90.0, "video").asset_id if table == "asset_revisions"
           else str(UUID(int=number)))
    assert after[table][row] != before[table][row], fact
    assert {t: {k: v for k, v in rows.items() if (t, k) != (table, row)}
            for t, rows in after.items()} == {
        t: {k: v for k, v in rows.items() if (t, k) != (table, row)} for t, rows in before.items()}
