"""One behaviour suite for both `LibraryWorkerStore`s (roadmap 4, slice I2; run ledger
.claude/runs/photo-4.md, seam b): `FakeLibraryStore` (tests/support/library_store_fake.py), which
the media worker's tests run against, and `PgPhotoLibrary`, which production runs. Each case runs
once per store and asserts only what the protocol (central.photo_library.ports) promises, so a
media test that passes on the fake means the same on PostgreSQL.

Each store is seeded through its own side (`Harness`): the PostgreSQL store through its operator
protocol (a connection pasted, a draft wanting a pool), the fake through its seeding helpers. Both
read one ManualClock, so time moves the same for both (`ProcessTransactionClock` stands in for the
database's clock, as it does in every store test that steps time).
"""

from __future__ import annotations

from typing import NamedTuple
from uuid import UUID

import pytest
from support.house_time import set_house
from support.library_store_fake import FakeLibraryStore

from central.db import ProcessTransactionClock
from central.house_time.model import HouseTime
from central.infra.photo_library_store import PgPhotoLibrary, _original_of, library_assets_in
from central.media_queue import QueueReceipt
from central.photo_library.model import (
    CheckMark,
    ContactAnswer,
    ContactRefused,
    HealthOk,
    ImmichTooOld,
    ImmichUser,
    ImmichVersion,
    InPool,
    KeyRejected,
    LeftOut,
    LibraryAlbum,
    LibraryAsset,
    LibraryCounts,
    MeasuredCopy,
    MissingPermissions,
    NeedsALook,
    OriginalFile,
    OtherUser,
    PoolThumbnail,
    SourceQuery,
    UnexpectedAnswer,
    Unreachable,
    Waiting,
)
from central.photo_library.policy import CHECK_EVERY_SECONDS
from central.photo_library.ports import (
    CheckComplete,
    CheckFailed,
    CheckIncomplete,
    CheckStep,
    KnownRevision,
)
from central.photo_library.views import ConnectionWrite
from media.library_checks import _code as problem_code

URL = "http://immich.invalid/api"
KEY = "PUBLIC-CONFORMANCE-KEY-0001"
ANNA = ImmichUser(user_ref=str(UUID(int=1)), name="Anna", email="anna@example.test")
BEN = ImmichUser(user_ref=str(UUID(int=2)), name="Ben", email="ben@example.test")
COUNTS = LibraryCounts(photos=25, videos=1, albums=2, people=2)
EDITED = "a" * 40 + "-" + "b" * 16   # a checksum, then an edit fingerprint


def answer(user=ANNA, *, missing=(), version=(3, 1, 0), counts=COUNTS):
    return ContactAnswer(version=ImmichVersion(major=version[0], minor=version[1],
                                               patch=version[2]),
                         user=user, missing_permissions=missing,
                         counts=None if missing else counts, full_size_copies=True)


def uid(number: int) -> str:
    return str(UUID(int=number))


def photo(ref: str, number: int, taken_at: float, *, revision: str | None = None):
    return LibraryAsset(connection_ref=ref, upstream_id=uid(number),
                        revision=revision or f"{number:040x}", kind="image", taken_at=taken_at,
                        width=4000, height=3000)


class Parent(NamedTuple):
    """An Immich asset's own record (`library_assets`, slice A2), as either store holds it."""
    kind: str
    taken_at: float
    original: OriginalFile | None
    created_at: float
    changed_at: float


ORIGINAL = OriginalFile(media_type="image/jpeg", width=4032, height=3024, orientation=6,
                        size_bytes=3_456_789)


def step(*entries, marks=(), seen=0, up_to=None, page=0):
    return CheckStep(progress={"page": page}, marks=tuple(marks), entries=tuple(entries),
                     seen_so_far=seen, immich_up_to=up_to)


# --- The two stores behind one seeding surface ------------------------------------------------


class _RecordingQueue:
    """PgPhotoLibrary's two queueing calls, recorded (Procrastinate is not under test here):
    `due` holds the due time of the last check queued per connection or pool."""

    def __init__(self):
        self.pools: list[str] = []
        self.due: dict[str, float] = {}

    def enqueue_connection_check_in(self, conn, connection_ref, *, due_at):
        self.due[connection_ref] = due_at
        return QueueReceipt(coalesced=False)

    def enqueue_pool_check_in(self, conn, pool_key, *, due_at):
        self.pools.append(pool_key)
        self.due[pool_key] = due_at
        return QueueReceipt(coalesced=False)


HOUSE_ZONE = "America/New_York"   # Settings › House in both harnesses


class FakeHarness:
    name = "fake"

    def __init__(self, clock):
        self.clock = clock
        self.store = FakeLibraryStore(clock, zone=HOUSE_ZONE)
        self._refs = 0

    def connect(self, *, key: str | None = KEY) -> str:
        self._refs += 1
        ref = f"library-{self._refs:012d}"
        self.store.add_connection(ref, URL, key)
        return ref

    def want(self, query: SourceQuery) -> str:
        key = self.store.add_pool(query)
        self.store.pools[key].wanted = True
        return key

    def unwant(self, pool_key: str) -> None:
        self.store.pools[pool_key].wanted = False

    def request_check(self, pool_key: str) -> None:
        self.store.request_check(pool_key)

    def next_due(self, ref: str) -> float | None:
        return self.store.next_due.get(ref)

    def rows(self, pool_key: str) -> dict[str, str]:
        if pool_key not in self.store.pools:
            return {}
        return {ref: entry.outcome for ref, entry in self.store.rows(pool_key).items()}

    def queued(self, pool_key: str) -> int:
        return self.store.queued.count(pool_key)

    def health(self, ref: str):
        return self.store.connection(ref).health

    def user(self, ref: str):
        return self.store.connection(ref).user

    def counts(self, ref: str):
        return self.store.connection(ref).counts

    def listed(self, ref: str, kind: str) -> tuple[str, ...]:
        return tuple(item.album_ref for item in self.store.connection(ref).lists.get(kind, ()))

    def parent(self, ref: str, upstream_id: str) -> Parent | None:
        record = self.store.assets.get((ref, upstream_id))
        if record is None:
            return None
        return Parent(record.kind, record.taken_at, record.original, record.created_at,
                      record.changed_at)

    def leased(self, asset_id: str) -> LibraryAsset | None:
        return self.store.library_asset(asset_id)


class PgHarness:
    name = "postgres"

    def __init__(self, registry):
        self.queue = _RecordingQueue()
        self.store = PgPhotoLibrary(registry.db, times=ProcessTransactionClock(registry.clock),
                                    queue=self.queue)
        self.db = registry.db
        set_house(registry.db, HouseTime(zone=HOUSE_ZONE))
        self._names = 0
        self._drafts: dict[str, str] = {}

    def connect(self, *, key: str | None = KEY) -> str:
        self._names += 1
        name = f"Library {self._names}"
        if key is not None:
            return self.store.add_connection(ConnectionWrite(name=name, url=URL, key=key), by="console")
        ref = f"library-{self._names:012d}"   # a cut-over row (C12): no URL, no key yet
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO library_connections(connection_ref,name,created_at,"
                         "created_by,changed_at,changed_by) VALUES(%s,%s,0,'unknown',0,'unknown')",
                         (ref, name))
        return ref

    def want(self, query: SourceQuery) -> str:
        draft = self.store.create_draft(query, None, by="console")
        with self.db.transaction() as conn:
            key = self.store.pool_of_draft_in(conn, draft)
        self._drafts[key] = draft
        return key

    def unwant(self, pool_key: str) -> None:
        with self.db.transaction() as conn:
            self.store.remove_draft_in(conn, self._drafts.pop(pool_key))

    def request_check(self, pool_key: str) -> None:
        with self.db.transaction() as conn:
            query = self.store.draft_for_save_in(conn, self._drafts[pool_key]).query
        self.store.set_draft_query(self._drafts[pool_key], query, by="console")   # Check now on a draft

    def rows(self, pool_key: str) -> dict[str, str]:
        with self.db.transaction() as conn:
            return {row["upstream_id"]: row["outcome"] for row in conn.execute(
                "SELECT upstream_id::text AS upstream_id,outcome FROM photo_pool_rows "
                "WHERE pool_key=%s", (pool_key,)).fetchall()}

    def queued(self, pool_key: str) -> int:
        return self.queue.pools.count(pool_key)

    def next_due(self, ref: str) -> float | None:
        return self.queue.due.get(ref)

    def _view(self, ref: str):
        return next(view for view in self.store.connections() if view.connection_ref == ref)

    def health(self, ref: str):
        return self._view(ref).health

    def user(self, ref: str):
        return self._view(ref).immich_user

    def counts(self, ref: str):
        return self._view(ref).counts

    def listed(self, ref: str, kind: str) -> tuple[str, ...]:
        with self.db.transaction() as conn:
            return tuple(row["item_ref"] for row in conn.execute(
                "SELECT e->>'ref' AS item_ref FROM library_connections c, "
                "jsonb_array_elements(COALESCE(c.lists->%s->'items','[]'::jsonb)) AS e "
                "WHERE c.connection_ref=%s ORDER BY 1", (kind, ref)).fetchall())

    def parent(self, ref: str, upstream_id: str) -> Parent | None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM library_assets WHERE connection_ref=%s AND "
                               "upstream_id=%s", (ref, upstream_id)).fetchone()
        if row is None:
            return None
        return Parent(row["kind"], row["taken_at"], _original_of(row), row["created_at"],
                      row["changed_at"])

    def leased(self, asset_id: str) -> LibraryAsset | None:
        with self.db.transaction() as conn:
            return library_assets_in(conn, [asset_id]).get(asset_id)


@pytest.fixture(params=["fake", "postgres"])
def harness(request, registry):
    return FakeHarness(registry.clock) if request.param == "fake" else PgHarness(registry)


@pytest.fixture
def clock(registry):
    return registry.clock


# --- Contacts: the fence, health, binding, the next contact -----------------------------------


def test_a_keyed_connection_is_queued_at_once_and_each_contact_queues_the_next(harness, clock):
    """No poll (S3a): a contact queues the connection's next check CHECK_EVERY_SECONDS after it
    began. Mutation probe: queue the next check without its due time (at once)."""
    store = harness.store
    keyed, keyless = harness.connect(), harness.connect(key=None)
    assert harness.next_due(keyed) == clock.utc() and harness.next_due(keyless) is None
    assert store.begin_contact(keyless) is None
    assert store.begin_contact("library-unknown00000") is None
    assert harness.next_due(keyless) is None
    assert store.connection_secret(keyless) is None
    secret = store.connection_secret(keyed)
    assert (secret.base_url, secret.api_key.get_secret_value()) == (URL, KEY)
    clock.advance(7)
    assert store.begin_contact(keyed) is not None
    assert harness.next_due(keyed) == clock.utc() + CHECK_EVERY_SECONDS


def test_the_latest_started_contact_wins_and_an_older_answer_writes_nothing(harness, clock):
    """Mutation probe: apply a contact whose number is not newer."""
    store = harness.store
    ref = harness.connect()
    older, newer = store.begin_contact(ref), store.begin_contact(ref)
    assert newer.contact_number > older.contact_number
    assert store.record_contact(newer, answer()) is True
    assert store.record_contact(older, ContactRefused(problem=KeyRejected())) is False
    assert isinstance(harness.health(ref), HealthOk)
    assert store.record_contact(newer, answer()) is False   # applied once


def test_health_names_each_problem_and_keeps_its_since_while_the_kind_holds(harness, clock):
    store = harness.store
    ref = harness.connect()
    assert store.record_contact(store.begin_contact(ref), answer())
    assert harness.health(ref) == HealthOk(checked_at=clock.utc())
    first = clock.utc()
    assert store.record_contact(store.begin_contact(ref), ContactRefused(problem=KeyRejected()))
    clock.advance(60)
    assert store.record_contact(store.begin_contact(ref), ContactRefused(problem=KeyRejected()))
    health = harness.health(ref)
    assert isinstance(health, NeedsALook) and health.problem == KeyRejected()
    assert (health.since, health.checked_at) == (first, clock.utc())
    for result, problem in (
            (ContactRefused(problem=Unreachable()), Unreachable()),
            (ContactRefused(problem=UnexpectedAnswer(detail="assets")),
             UnexpectedAnswer(detail="assets")),
            (answer(missing=("face.read",)), MissingPermissions(missing=("face.read",))),
            (answer(version=(1, 140, 0)), ImmichTooOld(found="1.140.0", oldest="3.1.0")),
            (answer(BEN), OtherUser(expected="Anna", found="Ben"))):
        clock.advance(60)
        assert store.record_contact(store.begin_contact(ref), result)
        health = harness.health(ref)
        assert isinstance(health, NeedsALook) and health.problem == problem
        assert health.since == clock.utc()   # a new kind starts its own since
    assert harness.counts(ref) == COUNTS     # kept from the passing contact (C7)
    clock.advance(60)
    assert store.record_contact(store.begin_contact(ref), answer())
    assert isinstance(harness.health(ref), HealthOk)


def test_the_first_passing_contact_binds_its_user_and_another_user_never_rebinds(harness):
    store = harness.store
    ref = harness.connect()
    store.record_contact(store.begin_contact(ref), ContactRefused(problem=Unreachable()))
    store.record_contact(store.begin_contact(ref), answer(BEN, missing=("face.read",)))
    assert harness.user(ref) is None          # neither contact passed
    store.record_contact(store.begin_contact(ref), answer(ANNA))
    store.record_contact(store.begin_contact(ref), answer(BEN))
    assert harness.user(ref) == ANNA and harness.health(ref).problem.kind == "other_user"
    assert store.connection_secret(ref).bound_user == ANNA.user_ref
    assert store.begin_contact(ref).connection.bound_user == ANNA.user_ref


def test_an_immich_too_old_to_use_binds_no_user(harness):
    """A contact that reads Needs a look (Update Immich) has not passed, so the next passing
    contact's user is the connection's."""
    store = harness.store
    ref = harness.connect()
    store.record_contact(store.begin_contact(ref), answer(BEN, version=(1, 140, 0)))
    assert harness.user(ref) is None
    store.record_contact(store.begin_contact(ref), answer(ANNA))
    assert harness.user(ref) == ANNA and isinstance(harness.health(ref), HealthOk)


def test_a_listing_replaces_its_kind_and_a_failed_one_keeps_it(harness):
    store = harness.store
    ref = harness.connect()
    albums = [LibraryAlbum(album_ref=uid(n), name=f"Album {n}", photos=n) for n in (1, 2)]
    store.record_list(ref, "albums", albums, None)
    assert harness.listed(ref, "albums") == (uid(1), uid(2))
    store.record_list(ref, "albums", albums[1:], None)
    assert harness.listed(ref, "albums") == (uid(2),)
    store.record_list(ref, "albums", None, "unreachable")
    assert harness.listed(ref, "albums") == (uid(2),)
    with pytest.raises(ValueError):
        store.record_list(ref, "albums", None, None)


# --- Checks: begin, steps, finish, due --------------------------------------------------------


def test_a_wanted_pool_is_checked_at_once_and_each_check_queues_the_next(harness, clock):
    """No poll (S3a): a new pool's first check and each next step at once; a step whose retries
    ran out queues the next check at its due time, which resumes from the stored progress; a
    finished check queues the next at its due time. Mutation probe: queue the next check only
    when a check completes (a failed step leaves none)."""
    store = harness.store
    ref = harness.connect()
    key = harness.want(SourceQuery(connection_ref=ref))
    assert harness.next_due(key) == clock.utc()
    lease = store.begin_check(key)
    assert lease.check_number == 1 and lease.progress is None
    assert lease.zone == HOUSE_ZONE                  # relative dates count on the house calendar
    clock.advance(5)
    assert store.record_step(lease, step(page=1))
    assert harness.next_due(key) == clock.utc()
    clock.advance(5)
    store.check_step_failed(key)                     # the next step raised until its retries ran out
    assert harness.next_due(key) == clock.utc() + CHECK_EVERY_SECONDS
    resumed = store.begin_check(key)
    assert (resumed.check_number, resumed.progress) == (1, {"page": 1})
    clock.advance(5)
    assert store.finish_check(resumed, CheckComplete())
    assert harness.next_due(key) == clock.utc() + CHECK_EVERY_SECONDS
    assert store.begin_check(key).check_number == 2


def test_check_now_queues_a_check_at_once_even_while_one_runs(harness, clock):
    """Mutation probe: finish a check at its due time although a Check now came while it ran."""
    store = harness.store
    key = harness.want(SourceQuery(connection_ref=harness.connect()))
    store.finish_check(store.begin_check(key), CheckComplete())
    assert harness.next_due(key) == clock.utc() + CHECK_EVERY_SECONDS
    clock.advance(10)
    harness.request_check(key)
    assert harness.next_due(key) == clock.utc()
    lease = store.begin_check(key)
    harness.request_check(key)
    clock.advance(10)
    store.finish_check(lease, CheckComplete())
    assert harness.next_due(key) == clock.utc()


def test_an_unwanted_pool_is_never_checked_but_its_checks_go_on_while_it_stands(harness, clock):
    store = harness.store
    unwanted = harness.want(SourceQuery(connection_ref=harness.connect()))
    harness.unwant(unwanted)
    clock.advance(10)
    assert store.begin_check(unwanted) is None
    assert harness.next_due(unwanted) == clock.utc() + CHECK_EVERY_SECONDS


def test_a_pool_of_a_connection_with_no_key_waits_for_its_key(harness, clock):
    """A cut-over connection (C12) waits for its key: begin_check refuses, and the pool's next
    check is queued at its due time, so it is checked once the key is pasted."""
    store = harness.store
    keyless = harness.want(SourceQuery(connection_ref=harness.connect(key=None)))
    assert store.begin_check(keyless) is None
    assert harness.next_due(keyless) == clock.utc() + CHECK_EVERY_SECONDS
    store.check_step_failed("0" * 64)                # a pool that is gone: nothing queued
    assert harness.next_due("0" * 64) is None


def test_a_superseded_lease_writes_nothing(harness):
    """Mutation probe: drop the check-number fence."""
    store = harness.store
    ref = harness.connect()
    key = harness.want(SourceQuery(connection_ref=ref))
    first = store.begin_check(key)
    assert store.finish_check(first, CheckComplete())
    second = store.begin_check(key)
    assert second.check_number == 2
    assert store.record_step(first, step(InPool(asset=photo(ref, 1, 100.0)), seen=1)) is False
    assert store.finish_check(first, CheckFailed(problem="connection")) is False
    assert harness.rows(key) == {}
    assert store.begin_check(key).check_number == 2   # still running


def test_a_step_adds_rows_except_marked_ids_and_queues_the_next_step(harness):
    """Marks subtract whether stored by an earlier step or this one. Mutation probe: let a
    marked entry in."""
    store = harness.store
    ref = harness.connect()
    key = harness.want(SourceQuery(connection_ref=ref))
    lease = store.begin_check(key)
    queued = harness.queued(key)
    assert store.record_step(lease, step(marks=(CheckMark(upstream_id=uid(3), mark="excluded"),),
                                         up_to=10))
    assert store.record_step(lease, step(
        InPool(asset=photo(ref, 1, 100.0)),
        Waiting(upstream_id=uid(2), kind="image", taken_at=90.0),
        InPool(asset=photo(ref, 3, 80.0)),                       # marked by the earlier step
        InPool(asset=photo(ref, 4, 70.0)),                       # marked by this step
        LeftOut(upstream_id=uid(5), reason="unreadable_answer"),
        marks=(CheckMark(upstream_id=uid(4), mark="stacked_copy"),), seen=4))
    assert harness.queued(key) == queued + 2
    assert harness.rows(key) == {uid(1): "in_pool", uid(2): "waiting", uid(5): "left_out"}


def test_a_step_records_updated_at_as_the_known_revision(harness):
    """E-P4-C2-1: Immich's updatedAt reaches the store, so an edited photo's edits are re-read
    only when it moved. Mutation probe: drop updated_at from the step's write."""
    store = harness.store
    ref = harness.connect()
    key = harness.want(SourceQuery(connection_ref=ref))
    lease = store.begin_check(key)
    edited = "2026-10-01T12:00:00.000Z"
    store.record_step(lease, step(
        InPool(asset=photo(ref, 1, 100.0, revision=EDITED), updated_at=edited),
        InPool(asset=photo(ref, 2, 90.0))))
    assert store.known_revisions(ref, [uid(1), uid(2), uid(9)]) == {
        uid(1): KnownRevision(updated_at=edited, revision=EDITED)}
    assert store.known_revisions(ref, []) == {}


def test_no_finish_removes_a_row_it_did_not_see(harness):
    """Design rule 2 and follow-up S4: a failed or incomplete check removes nothing, and a
    complete one keeps every row it did not see; only `record_recheck` removes such a row.
    Mutation probes: sweep on a failed or incomplete check; sweep unseen rows on a complete one."""
    store = harness.store
    ref = harness.connect()
    key = harness.want(SourceQuery(connection_ref=ref))
    first = store.begin_check(key)
    store.record_step(first, step(InPool(asset=photo(ref, 1, 100.0)),
                                  InPool(asset=photo(ref, 2, 90.0))))
    assert store.finish_check(first, CheckComplete())
    for finish in (CheckFailed(problem="connection"),
                   CheckIncomplete(reason="over_1000_at_one_instant"), CheckComplete()):
        harness.request_check(key)
        lease = store.begin_check(key)
        store.record_step(lease, step(InPool(asset=photo(ref, 3, 80.0))))
        assert store.finish_check(lease, finish)
        assert set(harness.rows(key)) == {uid(1), uid(2), uid(3)}


def test_the_re_check_lists_unseen_unmarked_rows_keeps_a_match_and_removes_the_rest(harness):
    """Follow-up S4. A row this check marked counts as gone without a re-check; a matching row
    becomes seen by this check (no longer listed, kept at the finish); a row Immich no longer
    answers is removed at once. Mutation probes: list a marked row; keep a row that no longer
    matches; leave a matched row unseen."""
    store = harness.store
    ref = harness.connect()
    key = harness.want(SourceQuery(connection_ref=ref))
    first = store.begin_check(key)
    store.record_step(first, step(*(InPool(asset=photo(ref, n, 100.0 - n)) for n in range(1, 6))))
    assert store.finish_check(first, CheckComplete())
    harness.request_check(key)
    lease = store.begin_check(key)
    store.record_step(lease, step(InPool(asset=photo(ref, 2, 98.0)),
                                  marks=(CheckMark(upstream_id=uid(4), mark="excluded"),)))
    assert store.unseen_rows(lease, 10) == (uid(1), uid(3), uid(5))
    assert store.unseen_rows(lease, 1) == (uid(1),)
    queued = harness.queued(key)
    assert store.record_recheck(lease, uid(1), True)
    assert store.record_recheck(lease, uid(3), False)
    assert harness.queued(key) == queued + 2
    assert store.unseen_rows(lease, 10) == (uid(5),)
    assert set(harness.rows(key)) == {uid(1), uid(2), uid(4), uid(5)}
    assert store.record_recheck(lease, uid(5), True)
    assert store.unseen_rows(lease, 10) == ()
    assert store.finish_check(lease, CheckComplete())
    assert set(harness.rows(key)) == {uid(1), uid(2), uid(5)}


def test_a_superseded_lease_re_checks_nothing(harness):
    store = harness.store
    ref = harness.connect()
    key = harness.want(SourceQuery(connection_ref=ref))
    first = store.begin_check(key)
    store.record_step(first, step(InPool(asset=photo(ref, 1, 100.0))))
    assert store.finish_check(first, CheckComplete())
    harness.request_check(key)
    second = store.begin_check(key)
    assert store.unseen_rows(first, 10) == ()
    assert store.record_recheck(first, uid(1), False) is False
    assert store.unseen_rows(second, 10) == (uid(1),)
    assert set(harness.rows(key)) == {uid(1)}


def test_a_row_added_before_its_mark_leaves_when_the_check_completes(harness):
    """A photo found by an earlier step and then marked by a later step of the same check (an
    exclusion read after it) is subtracted at completion, not kept."""
    store = harness.store
    ref = harness.connect()
    key = harness.want(SourceQuery(connection_ref=ref))
    lease = store.begin_check(key)
    store.record_step(lease, step(InPool(asset=photo(ref, 1, 100.0)),
                                  InPool(asset=photo(ref, 2, 90.0))))
    store.record_step(lease, step(marks=(CheckMark(upstream_id=uid(1), mark="excluded"),)))
    assert store.finish_check(lease, CheckComplete())
    assert harness.rows(key) == {uid(2): "in_pool"}


def test_a_thumbnail_is_served_only_for_an_in_pool_row_of_a_wanted_pool(harness):
    """Seam d. Mutation probe: serve any recorded asset."""
    store = harness.store
    ref = harness.connect()
    key = harness.want(SourceQuery(connection_ref=ref))
    shown = photo(ref, 1, 100.0)
    store.record_step(store.begin_check(key), step(
        InPool(asset=shown), Waiting(upstream_id=uid(2), kind="image", taken_at=90.0)))
    assert store.pool_thumbnail(shown.asset_id) == PoolThumbnail(
        connection_ref=ref, upstream_id=shown.upstream_id, revision=shown.revision)
    assert store.pool_thumbnail(photo(ref, 2, 90.0).asset_id) is None
    harness.unwant(key)
    assert store.pool_thumbnail(shown.asset_id) is None


def test_maintenance_retires_unwanted_pools_and_keeps_wanted_ones(harness):
    """Mutation probe: retire a wanted pool."""
    store = harness.store
    ref = harness.connect()
    kept = harness.want(SourceQuery(connection_ref=ref))
    gone = harness.want(SourceQuery(connection_ref=ref, favourites_only=True))
    wanted, unwanted = photo(ref, 1, 100.0), photo(ref, 2, 90.0)
    store.record_step(store.begin_check(kept), step(InPool(asset=wanted)))
    store.record_step(store.begin_check(gone), step(InPool(asset=unwanted)))
    harness.unwant(gone)
    recorded = store.retire_unwanted()
    assert harness.rows(gone) == {} and harness.rows(kept) == {uid(1): "in_pool"}
    assert unwanted.asset_id not in recorded and recorded <= {wanted.asset_id}
    assert store.pool_thumbnail(wanted.asset_id) is not None
    due = harness.next_due(gone)
    assert store.begin_check(gone) is None
    assert harness.next_due(gone) == due             # a pool that is gone queues nothing


# --- One record per Immich asset, its revisions and copies as children (slice A2) -------------


def test_one_immich_asset_seen_by_two_pools_and_two_checks_is_one_record(harness, clock):
    """A2 case 1: one parent per Immich asset; a second sighting with the same facts leaves it
    as it was (decision 0033), and a taken-time change (a date edit in Immich, revision
    unchanged) rewrites the one record: its created time stays, its changed time moves.
    Mutation probes: keep taken_at on pool rows -> the second pool reads the old time (the
    PostgreSQL half, tests/test_asset_tables.py); move changed_at on every sighting -> the
    unchanged sighting moves it."""
    store = harness.store
    ref = harness.connect()
    first = harness.want(SourceQuery(connection_ref=ref))
    second = harness.want(SourceQuery(connection_ref=ref, favourites_only=True))
    shown = photo(ref, 1, 100.0)
    store.record_step(store.begin_check(first), step(InPool(asset=shown)))
    seen_at = clock.utc()
    clock.advance(60)
    store.record_step(store.begin_check(second), step(InPool(asset=shown)))
    assert harness.parent(ref, uid(1)) == Parent("image", 100.0, None, seen_at, seen_at)
    clock.advance(60)
    redated = shown.model_copy(update={"taken_at": 50.0})
    store.record_step(store.begin_check(second), step(InPool(asset=redated)))
    parent = harness.parent(ref, uid(1))
    assert (parent.kind, parent.taken_at) == ("image", 50.0)
    assert (parent.created_at, parent.changed_at) == (seen_at, clock.utc())
    assert harness.leased(shown.asset_id) == redated    # the same revision, the new date


def test_an_edit_replaces_the_revision_of_its_one_photo(harness, clock):
    """A2 case 2, decision 0034: a new revision replaces the old on the photo's one record (the
    old is no photo's revision any more); known_revisions answers the newer."""
    store = harness.store
    ref = harness.connect()
    key = harness.want(SourceQuery(connection_ref=ref))
    unedited = photo(ref, 1, 100.0)
    edited = photo(ref, 1, 100.0, revision=EDITED).model_copy(update={"width": 3000})
    store.record_step(store.begin_check(key), step(
        InPool(asset=unedited, updated_at="2026-10-01T00:00:00.000Z")))
    clock.advance(60)
    harness.request_check(key)
    store.record_step(store.begin_check(key), step(
        InPool(asset=edited, updated_at="2026-10-02T00:00:00.000Z")))
    assert (harness.leased(unedited.asset_id), harness.leased(edited.asset_id)) == (
        None, edited)
    assert store.known_revisions(ref, [uid(1)]) == {
        uid(1): KnownRevision(updated_at="2026-10-02T00:00:00.000Z", revision=EDITED)}


def test_only_a_photo_read_in_full_rewrites_its_originals_facts(harness):
    """A2 case 3: a Waiting entry did not read the original, so it keeps the facts an InPool
    wrote; a later InPool with `original=None` clears them. Mutation probe: let Waiting write
    NULL originals -> this fails."""
    store = harness.store
    ref = harness.connect()
    key = harness.want(SourceQuery(connection_ref=ref))
    lease = store.begin_check(key)
    store.record_step(lease, step(InPool(asset=photo(ref, 1, 100.0).model_copy(
        update={"original": ORIGINAL}))))
    store.record_step(lease, step(Waiting(upstream_id=uid(1), kind="image", taken_at=90.0),
                                  LeftOut(upstream_id=uid(2), reason="unreadable_answer")))
    parent = harness.parent(ref, uid(1))
    assert (parent.taken_at, parent.original) == (90.0, ORIGINAL)
    assert harness.parent(ref, uid(2)) is None          # no kind or taken time: no record
    store.record_step(lease, step(InPool(asset=photo(ref, 1, 100.0))))
    assert harness.parent(ref, uid(1)).original is None


def test_a_copy_is_measured_once_per_revision_and_the_later_size_wins(harness, clock):
    """A2 case 5: one row per (revision, copy), the later measurement kept; an unknown revision
    has no copies and records none."""
    store = harness.store
    ref = harness.connect()
    key = harness.want(SourceQuery(connection_ref=ref))
    shown = photo(ref, 1, 100.0)
    store.record_step(store.begin_check(key), step(InPool(asset=shown)))
    store.record_copies(shown.asset_id, [MeasuredCopy(name="preview", media_type="image/jpeg",
                                                      width=1440, height=1080)])
    clock.advance(60)
    store.record_copies(shown.asset_id, [
        MeasuredCopy(name="preview", media_type="image/jpeg", width=2160, height=1620),
        MeasuredCopy(name="fullsize", media_type="image/jpeg", width=4000, height=3000)])
    sizes = store.copy_sizes(shown.asset_id)
    assert sorted(sizes) == ["fullsize", "preview"]
    assert (sizes["preview"].width, sizes["preview"].height,
            sizes["preview"].measured_at) == (2160, 1620, clock.utc())
    assert store.copy_sizes("unknown") == {}
    store.record_copies("unknown", [MeasuredCopy(name="preview", media_type="image/jpeg",
                                                 width=10, height=10)])
    assert store.copy_sizes("unknown") == {}


# --- The list status the store's table gives each problem the worker sends --------------------


@pytest.mark.parametrize(("problem", "status"), [
    (KeyRejected(), "permission"),
    (MissingPermissions(missing=("album.read",)), "permission"),
    (ImmichTooOld(found="1.140.0", oldest="3.1.0"), "incompatible"),
    (UnexpectedAnswer(detail="albums"), "incompatible"),
    (Unreachable(), "unavailable"),
])
def test_a_failed_listing_reads_as_the_status_of_the_problem_the_worker_sends(
        registry, problem, status):
    """Seam h: M2's `LibraryChecks.contact` records a failed listing under `_code(problem)`; the
    PostgreSQL store turns that code into the list's status by the run ledger's table (central
    lane findings): key_rejected and missing_permissions read as permission, immich_too_old and
    unexpected_answer as incompatible, anything else as unavailable."""
    pg = PgHarness(registry)
    ref = pg.connect()
    pg.store.record_list(ref, "albums", None, problem_code(problem))
    assert pg.store.albums(ref, "", 10).status == status
