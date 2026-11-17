"""The photo library store's connections, contacts and lists (roadmap 4, slice C1; run ledger
.claude/runs/photo-4.md, C1 cases 6-11).

Driven through the store's protocol methods only, the worker side fed by hand-made
`ContactResult` values: no Immich, no worker. Every time is the database's.
"""

from __future__ import annotations

from uuid import UUID

import psycopg
import pytest
from psycopg.types.json import Jsonb
from support.migrations import migrate_before

from central.db import Database, DatabaseTransactionClock
from central.infra.photo_library_store import PgPhotoLibrary
from central.media_queue import QueueReceipt
from central.photo_library.model import (
    ContactAnswer,
    ContactRefused,
    ImmichUser,
    ImmichVersion,
    KeyRejected,
    LibraryAlbum,
    LibraryCounts,
    LibraryTag,
    Place,
    SourceQuery,
    TakenBetween,
)
from central.photo_library.views import ConnectionWrite

ANNA = ImmichUser(user_ref=str(UUID(int=1)), name="Anna", email="anna@example.test")
BEN = ImmichUser(user_ref=str(UUID(int=2)), name="Ben", email="ben@example.test")
COUNTS = LibraryCounts(photos=41203, videos=310, albums=212, people=96)


class RecordingLibraryQueue:
    """The two queuing methods the store uses, recorded; coalescing as Procrastinate's lock."""

    def __init__(self):
        self.connections, self.pools, self.fail = [], [], False

    def enqueue_connection_check_in(self, conn, connection_ref):
        coalesced = connection_ref in self.connections
        self.connections.append(connection_ref)
        return QueueReceipt(coalesced=coalesced)

    def enqueue_pool_check_in(self, conn, pool_key):
        if self.fail:
            raise RuntimeError("queue down")
        self.pools.append(pool_key)
        return QueueReceipt(coalesced=False)


def store_of(db, queue=None):
    return PgPhotoLibrary(db, times=DatabaseTransactionClock(),
                          queue=queue if queue is not None else RecordingLibraryQueue())


def answer(user=ANNA, *, missing=(), counts=COUNTS, version=(3, 1, 0)):
    return ContactAnswer(version=ImmichVersion(major=version[0], minor=version[1], patch=version[2]),
                         user=user, missing_permissions=missing, counts=counts,
                         full_size_copies=True)


def health_of(store, ref):
    return next(view for view in store.connections() if view.connection_ref == ref)


@pytest.fixture
def store(registry):
    library = store_of(registry.db)
    ref = library.add_connection(ConnectionWrite(name="Home", url="http://immich.invalid:2283",
                                                 key="PUBLIC-FIXTURE-KEY-1234"))
    return library, ref


def _set_contact_age(db, ref, seconds):
    with db.transaction() as conn:
        conn.execute("UPDATE library_connections SET contact_started_at="
                     "EXTRACT(EPOCH FROM clock_timestamp())-%s WHERE connection_ref=%s",
                     (seconds, ref))


# -- contacts: newest wins, the user binds once, health over time --------------------------------


def test_newest_started_contact_wins(store):
    """Case 6. Mutation probe: order by contact_started_at instead of the number."""
    library, ref = store
    first, second = library.begin_contact(ref), library.begin_contact(ref)
    assert (first.contact_number, second.contact_number) == (1, 2)
    assert library.record_contact(second, answer()) is True
    assert library.record_contact(first, ContactRefused(problem=KeyRejected())) is False
    assert health_of(library, ref).health.state == "ok"
    assert library.record_contact(second, answer()) is False   # applied once


def test_the_first_passing_contact_binds_the_user_and_another_user_needs_a_look(store):
    """Case 7. Mutation probe: bind the user on every passing contact."""
    library, ref = store
    assert library.record_contact(library.begin_contact(ref), answer(ANNA)) is True
    view = health_of(library, ref)
    assert view.immich_user == ANNA and view.counts == COUNTS
    other = LibraryCounts(photos=1, videos=0, albums=0, people=0)
    assert library.record_contact(library.begin_contact(ref), answer(BEN, counts=other)) is True
    view = health_of(library, ref)
    assert view.health.state == "needs_a_look"
    assert view.health.problem.model_dump() == {"kind": "other_user", "expected": "Anna",
                                                "found": "Ben"}
    assert view.immich_user == ANNA and view.counts == COUNTS
    assert library.connection_secret(ref).bound_user == ANNA.user_ref


def test_a_contact_missing_permissions_binds_nobody(store):
    library, ref = store
    library.record_contact(library.begin_contact(ref), answer(BEN, missing=("face.read",),
                                                             counts=None))
    view = health_of(library, ref)
    assert view.immich_user is None and view.health.problem.kind == "missing_permissions"
    library.record_contact(library.begin_contact(ref), answer(ANNA))
    assert health_of(library, ref).immich_user == ANNA


def test_needs_a_look_keeps_its_since_then_clears(store):
    """Case 8."""
    library, ref = store
    library.record_contact(library.begin_contact(ref), ContactRefused(problem=KeyRejected()))
    first = health_of(library, ref).health
    assert first.state == "needs_a_look" and first.problem.kind == "key_rejected"
    library.record_contact(library.begin_contact(ref), ContactRefused(problem=KeyRejected()))
    second = health_of(library, ref).health
    assert second.since == first.since and second.checked_at > first.checked_at
    library.record_contact(library.begin_contact(ref), answer())
    assert health_of(library, ref).health.state == "ok"


def test_due_contacts_use_the_database_clock_and_skip_keyless_rows(store, registry):
    """Case 9."""
    library, ref = store
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO library_connections(connection_ref,name,created_at) "
                     "VALUES('family-library','family-library',0)")
    assert library.due_contacts() == (ref,)                  # never contacted
    _set_contact_age(registry.db, ref, 899)
    assert library.due_contacts() == ()
    _set_contact_age(registry.db, ref, 900)
    assert library.due_contacts() == (ref,)
    assert library.begin_contact("family-library") is None
    assert library.connection_secret("family-library") is None


# -- lists ---------------------------------------------------------------------------------------


def _tag(number, path):
    return LibraryTag(tag_ref=str(UUID(int=number)), path=path, name=path.rsplit("/", 1)[-1])


def test_lists_have_no_cap_keep_the_last_on_failure_and_search_prefix_first(store):
    """Case 10. Mutation probe: cap the stored list at 5,000."""
    library, ref = store
    tags = [_tag(n, f"Year {n:05d}") for n in range(1, 6001)]
    library.record_list(ref, "tags", tags, None)
    assert library.tags(ref, "year 060", 20).total_matches == 1
    assert library.tags_by_id(ref, (str(UUID(int=6000)),)).items[0].path == "Year 06000"
    listed = library.tags(ref, "", 20)
    assert listed.total_matches == 6000 and listed.status == "ok"

    library.record_list(ref, "tags", None, "unreachable")
    failed = library.tags(ref, "", 1)
    assert (failed.status, failed.observed_at, failed.total_matches) == (
        "unavailable", listed.observed_at, 6000)
    assert library.tags_by_id(ref, (str(UUID(int=7000)),)).absent == ()   # failed: proves nothing
    library.record_list(ref, "tags", tags[:2], None)
    assert library.tags_by_id(ref, (str(UUID(int=7000)),)).absent == (str(UUID(int=7000)),)

    albums = [LibraryAlbum(album_ref=str(UUID(int=n)), name=name, photos=n)
              for n, name in ((1, "Summer garden"), (2, "Garden party"), (3, "‮⁩"))]
    library.record_list(ref, "albums", albums, None)
    assert [a.name for a in library.albums(ref, "GARDEN", 20).items] == [
        "Garden party", "Summer garden"]
    assert library.albums(ref, "", 20).total_matches == 2          # nothing visible: never offered
    assert library.albums_by_id(ref, (str(UUID(int=3)),)).items[0].name == ""   # yet stored

    library.record_list(ref, "places", [Place(city="Paris", country="France"),
                                        Place(state="Bavaria", country="Germany")], None)
    assert [p.country for p in library.places(ref, "bav", 20).items] == ["Germany"]
    assert library.people(ref, "", 20).status == "pending"


def test_reads_of_an_unknown_connection_are_refused(store):
    library, _ = store
    with pytest.raises(Exception) as caught:
        library.albums("elsewhere", "", 20)
    assert getattr(caught.value, "code", None) == "connection_unknown"


# -- migration 080 on a pre-4 database (C12) -----------------------------------------------------


def test_migration_080_converts_pre_4_sources_and_makes_cut_over_rows(empty_database):
    """Case 11. Mutation probe: map `favorites: false` to `favourites_only: true`."""
    db = Database(empty_database, application_name="photo-wall-test")
    try:
        migrate_before(db, "080")
        tag = str(UUID(int=9))
        pre_4 = {"schema": 1, "connection_ref": "family-library", "favorites": None,
                 "captured_from": None, "captured_until": None, "media_types": ["image", "video"]}
        specs = {   # as a pre-4 Central stored them (SourceSpec's JSON form)
            "favourites:1": {**pre_4, "source_ref": "favourites:1", "favorites": True,
                             "media_types": ["image"]},
            "window:1": {**pre_4, "source_ref": "window:1", "favorites": False,
                         "captured_from": 100.0, "captured_until": 200.0},
            "tagged:1": {**pre_4, "source_ref": "tagged:1", "tags": [tag]},
        }
        with db.transaction() as conn:
            for ref, spec in specs.items():
                conn.execute("INSERT INTO media_sources(source_ref,spec) VALUES(%s,%s)",
                             (ref, Jsonb(spec)))
        migrate_before(db, "081")   # 081 drops `spec`, which this case reads back
        with db.transaction() as conn:
            rows = {row["source_ref"]: row for row in conn.execute(
                "SELECT source_ref,spec,query,pool_key FROM media_sources").fetchall()}
            connections = conn.execute("SELECT * FROM library_connections").fetchall()
    finally:
        db.close()
    assert [(c["connection_ref"], c["name"], c["url"], c["api_key"], c["health"])
            for c in connections] == [("family-library", "family-library", None, None,
                                       {"state": "not_checked"})]
    expected = {
        "favourites:1": SourceQuery(connection_ref="family-library", favourites_only=True,
                                    media_types=("photo", "live_photo")),
        "window:1": SourceQuery(connection_ref="family-library",
                                taken=TakenBetween(start=100.0, end=200.0)),
        "tagged:1": SourceQuery(connection_ref="family-library", tags=(tag,)),
    }
    for ref, query in expected.items():
        assert rows[ref]["query"] == query.model_dump(mode="json"), ref
        assert SourceQuery.model_validate(rows[ref]["query"]) == query
        assert rows[ref]["pool_key"] is None
        assert rows[ref]["spec"] == specs[ref]


# -- migration 112: the pool check's audit fields and pages (decision 0033) ---------------------


def test_migration_112_renames_in_the_catalog_ends_running_checks_and_drops_the_defaults(
        empty_database):
    """No table of the pool check is rewritten (each keeps its file), a row's first and last
    sighting become its created and changed times, and a running check ends so the next one
    starts with pages. Mutation probes: leave a default in place -> the unstamped insert
    succeeds; drop step 3 -> the pool stays checking."""
    db = Database(empty_database, application_name="photo-wall-test")
    upstream = str(UUID(int=1))
    pools = {name: f"{number:064x}" for number, name in enumerate(
        ("resumed", "first", "done"), start=1)}
    try:
        migrate_before(db, "112")
        with db.transaction() as conn:
            conn.execute("INSERT INTO library_connections(connection_ref,name,created_at) "
                         "VALUES('library-000000000001','Home',0)")
            for name, key in pools.items():
                conn.execute(
                    "INSERT INTO photo_pools(pool_key,connection_ref,query,check_number,"
                    "check_state,last_complete_at,check_finished_at,progress,check_requested,"
                    "created_at) VALUES(%s,'library-000000000001','{}',2,%s,%s,%s,%s,FALSE,0)",
                    (key, "complete" if name == "done" else "checking",
                     None if name == "first" else 50.0, None if name == "first" else 50.0,
                     None if name == "done" else Jsonb({"page": 3})))
            conn.execute("INSERT INTO library_assets(connection_ref,upstream_id,kind,taken_at,"
                         "first_seen,last_seen) VALUES('library-000000000001',%s,'image',1,10,20)",
                         (upstream,))
            conn.execute("INSERT INTO asset_revisions(asset_id,connection_ref,upstream_id,"
                         "revision,width,height,first_seen,last_seen) "
                         "VALUES('asset-1','library-000000000001',%s,'r',4,3,11,21)", (upstream,))
            conn.execute("INSERT INTO photo_pool_rows(pool_key,upstream_id,outcome,asset_id,"
                         "seen_check) VALUES(%s,%s,'in_pool','asset-1',2)",
                         (pools["resumed"], upstream))
            conn.execute("INSERT INTO photo_check_marks VALUES(%s,2,%s,'excluded')",
                         (pools["resumed"], upstream))
            files = _files(conn)
        db.migrate()
        with db.transaction() as conn:
            migrated_at = conn.execute(
                "SELECT EXTRACT(EPOCH FROM applied_at)::float8 AS at FROM schema_migrations "
                "WHERE name='112_pool_check_changed_only.sql'").fetchone()["at"]
            assert _files(conn) == files
            states = {row["pool_key"]: (row["check_state"], row["check_requested"],
                                        row["progress"]) for row in conn.execute(
                "SELECT pool_key,check_state,check_requested,progress FROM photo_pools")}
            parent = conn.execute("SELECT created_at,changed_at,created_by,changed_by "
                                  "FROM library_assets").fetchone()
            revision = conn.execute("SELECT created_at,changed_at,created_by,changed_by "
                                    "FROM asset_revisions").fetchone()
            row = conn.execute("SELECT created_at,changed_at,created_by,changed_by "
                               "FROM photo_pool_rows").fetchone()
            tables = {row["name"] for row in conn.execute(
                "SELECT table_name AS name FROM information_schema.tables "
                "WHERE table_schema='public'").fetchall()}
            defaults = conn.execute(
                "SELECT table_name,column_name FROM information_schema.columns WHERE "
                "table_name IN ('library_assets','asset_revisions','photo_pool_rows') AND "
                "column_name IN ('created_at','changed_at','created_by','changed_by') AND "
                "column_default IS NOT NULL").fetchall()
        with pytest.raises(psycopg.errors.NotNullViolation), db.transaction() as conn:
            conn.execute("INSERT INTO library_assets(connection_ref,upstream_id,kind,taken_at,"
                         "created_at,changed_at) VALUES('library-000000000001',%s,'image',1,1,1)",
                         (str(UUID(int=2)),))
    finally:
        db.close()
    assert states == {pools["resumed"]: ("complete", True, None),
                      pools["first"]: ("never_checked", True, None),
                      pools["done"]: ("complete", False, None)}
    assert tuple(parent.values()) == (10, 20, "library-check", "library-check")
    assert tuple(revision.values()) == (11, 21, "library-check", "library-check")
    assert row["created_at"] == row["changed_at"] == pytest.approx(migrated_at, abs=1)
    assert (row["created_by"], row["changed_by"]) == ("library-check", "library-check")
    assert "photo_check_marks" not in tables and "photo_check_pages" in tables
    assert defaults == []


def _files(conn) -> dict[str, int]:
    """Each pool-check table's file: a rewrite gives it a new one."""
    return {row["name"]: row["file"] for row in conn.execute(
        "SELECT relname AS name,relfilenode AS file FROM pg_class WHERE relname IN "
        "('library_assets','asset_revisions','photo_pool_rows')").fetchall()}
