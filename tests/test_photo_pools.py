"""Pools keyed by their query, drafts, and the store half of a check (roadmap 4, slice C2; run
ledger .claude/runs/photo-4.md, C2 cases 1-11).

The worker side is fed by hand-made `CheckStep` values: a check adds what it confirms, step by
step, fenced by its number, and removes only when it completes (design rule 2). No Immich.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from psycopg.types.json import Jsonb
from test_photo_library_store import RecordingLibraryQueue, store_of

from central.photo_library.model import (
    CheckMark,
    InPool,
    LeftOut,
    LibraryAsset,
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
            "SELECT upstream_id::text AS upstream_id,outcome,reason,asset_id,seen_check "
            "FROM photo_pool_rows WHERE pool_key=%s", (key,)).fetchall()}


def _pool(store, key):
    with store.db.transaction() as conn:
        return conn.execute("SELECT * FROM photo_pools WHERE pool_key=%s", (key,)).fetchone()


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
    assert {row["seen_check"] for row in rows.values()} == {lease.check_number}
    assert rows[str(UUID(int=5))]["reason"] == "unreadable_answer"
    with store.db.transaction() as conn:
        stored = conn.execute("SELECT connection_ref,upstream_id::text AS upstream_id,metadata,"
                              "updated_at FROM asset_revisions WHERE asset_id=%s",
                              (asset(ref, 1, 100.0).asset_id,)).fetchone()
        marks = conn.execute("SELECT upstream_id::text AS id,mark FROM photo_check_marks "
                             "ORDER BY mark").fetchall()
    assert (stored["connection_ref"], stored["upstream_id"]) == (ref, str(UUID(int=1)))
    assert LibraryAsset.model_validate(stored["metadata"]) == asset(ref, 1, 100.0)
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
    """Case 4. Mutation probe: sweep on an incomplete finish."""
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
    store.finish_check(lease, CheckComplete())
    assert sorted(_rows(store, key)) == [str(UUID(int=1))]
    assert _pool(store, key)["last_complete_at"] > completed_at
    with store.db.transaction() as conn:
        assert {row["check_number"] for row in conn.execute(
            "SELECT check_number FROM photo_check_marks").fetchall()} == {lease.check_number}


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
            conn.execute("INSERT INTO assets(kind,identity,created_at) "
                         "VALUES('library-thumbnail',%s,0)", (asset(ref, number, 1.0).asset_id,))
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
