"""The worker's checks over a fake LibraryClient and the fake store (roadmap 4, slice M2).

The fake client answers as M1's offline tests pin ImmichClient to (tests/test_immich_library.py):
pages carry a KeysetPosition, exclusion pages carry ids only, a connection-wide failure raises
LibraryUnavailable. Steps are driven one call at a time, as the queue would.
"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from support.library_store_fake import FakeLibraryStore, check_to_end

from central.photo_library.model import (
    ContactAnswer,
    ContactRefused,
    Exclusions,
    ImmichUser,
    ImmichVersion,
    InPool,
    LibraryAlbum,
    LibraryCounts,
    NeedsALook,
    Place,
    SourceQuery,
    Unreachable,
)
from central.photo_library.ports import CheckComplete, CheckFailed
from contracts.time import ManualClock
from media.library_checks import LibraryChecks
from media.library_client import (
    ChosenItemGone,
    IdsPage,
    KeysetPosition,
    LibraryUnavailable,
    SearchItem,
    SearchPage,
    TimeBucket,
)

OWNER = str(UUID(int=100))
SHA = "a" * 40
T0 = datetime(2026, 1, 15, tzinfo=UTC).timestamp()
USER = ImmichUser(user_ref=OWNER, name="Owner", email="owner@example.test")


def ref(number):
    return str(UUID(int=number))


def photo(number, **changes):
    values = dict(upstream_id=ref(number), owner_ref=OWNER, kind="image", taken_at=T0 - number,
                  checksum=SHA, updated_at="u", width=40, height=30)
    values.update(changes)
    return SearchItem(**values)


class FakeClient:
    """An Immich library in memory, read in pages of `size` (the position is an index)."""

    def __init__(self, photos=(), *, excluded=(), size=2):
        self.photos, self.excluded, self.size = list(photos), list(excluded), size
        self.calls: list[str] = []
        self.fail_on: str | None = None
        self.stacked: tuple[TimeBucket, ...] = ()
        self.shown: frozenset[str] = frozenset()
        self.closed = False

    def _call(self, name):
        self.calls.append(name)
        if self.fail_on == name:
            raise LibraryUnavailable(Unreachable())
        if self.fail_on == "gone:" + name:
            raise ChosenItemGone()

    def _slice(self, rows, after):
        start = 0 if after is None else int(after.edge)
        end = start + self.size
        return rows[start:end], (KeysetPosition(edge=end) if end < len(rows) else None)

    async def contact(self):
        self.calls.append("contact")
        if self.fail_on == "contact":
            return ContactRefused(problem=Unreachable())
        return ContactAnswer(version=ImmichVersion(major=2, minor=5, patch=6), user=USER,
                             counts=LibraryCounts(photos=3, videos=0, albums=1, people=0))

    async def albums(self):
        self._call("albums")
        return (LibraryAlbum(album_ref=ref(30), name="Garden", photos=3),)

    async def people(self):
        self._call("people")
        return ()

    async def places(self):
        self._call("places")
        return (Place(country="France"),)

    async def tags(self):
        self._call("tags")
        return ()

    async def up_to(self, query, window):
        self._call("up_to")
        return len(self.photos)

    async def stacked_buckets(self, *, partners):
        self._call("stacked_buckets")
        return self.stacked

    async def stack_primaries(self, buckets):
        self._call("stack_primaries")
        return self.shown

    async def exclusion_page(self, exclusion, after):
        self._call("exclusion")
        ids, position = self._slice(self.excluded, after)
        return IdsPage(ids=tuple(ids), next=position)

    async def search_page(self, query, window, after):
        self._call("include")
        items, position = self._slice(self.photos, after)
        return SearchPage(items=tuple(items), next=position)

    async def edits_fingerprint(self, upstream_id):
        self._call("edits")
        return "f" * 16

    async def close(self):
        self.closed = True


def setup(client, **query_changes):
    clock = ManualClock(wall=T0, mono=0)
    store = FakeLibraryStore(clock)
    store.add_connection("main", "http://immich.test/api", "key-one")
    query = SourceQuery(connection_ref="main", **{
        "exclude": Exclusions(screenshots=False), **query_changes})
    key = store.add_pool(query)
    secrets = []

    def client_for(secret):
        secrets.append(secret)
        return client
    return store, key, LibraryChecks(store, client_for, clock), secrets


def in_pool(store, key):
    return sorted(ref for ref, entry in store.rows(key).items() if isinstance(entry, InPool))


def test_a_check_reads_up_to_then_exclusions_then_include_pages_one_per_step():
    client = FakeClient([photo(n) for n in range(1, 6)], excluded=[ref(2), ref(4), ref(99)])
    store, key, checks, _ = setup(client, exclude=Exclusions(people=(ref(60),),
                                                             screenshots=False))
    asyncio.run(check_to_end(checks, store, key))
    assert client.calls == ["up_to", "stacked_buckets", "exclusion", "exclusion", "include",
                            "include", "include"]
    assert in_pool(store, key) == [ref(1), ref(3), ref(5)]   # excluded ids never became rows
    assert store.pools[key].up_to == 5
    assert isinstance(store.pools[key].outcome, CheckComplete)
    assert len(store.queued) == 6   # six steps each queued the next; the seventh finished


def test_an_unreachable_immich_mid_check_fails_it_and_removes_nothing():
    client = FakeClient([photo(n) for n in range(1, 6)])
    store, key, checks, _ = setup(client)
    asyncio.run(check_to_end(checks, store, key))
    assert len(in_pool(store, key)) == 5
    client.photos = client.photos[:1]
    client.fail_on = "include"
    asyncio.run(check_to_end(checks, store, key))
    assert store.pools[key].outcome == CheckFailed(problem="connection")
    assert len(in_pool(store, key)) == 5


def test_a_chosen_album_immich_lost_fails_the_check_as_such():
    client = FakeClient([photo(1)])
    client.fail_on = "gone:include"
    store, key, checks, _ = setup(client, albums=(ref(30),))
    asyncio.run(check_to_end(checks, store, key))
    assert store.pools[key].outcome == CheckFailed(problem="chosen_item_gone")


def test_a_photo_a_complete_check_missed_leaves_only_when_that_check_finishes():
    client = FakeClient([photo(n) for n in range(1, 6)])
    store, key, checks, _ = setup(client)
    asyncio.run(check_to_end(checks, store, key))
    del client.photos[0]                               # ref(1) is gone from Immich
    for _ in range(3):                                 # up_to and two include pages
        asyncio.run(checks.check_pool(key))
        assert ref(1) in in_pool(store, key)
    asyncio.run(check_to_end(checks, store, key))
    assert ref(1) not in in_pool(store, key) and len(in_pool(store, key)) == 4


def test_stacked_copies_the_timeline_hides_are_marked_not_pooled():
    client = FakeClient([photo(1), photo(2)])
    client.stacked = (TimeBucket(month="2026-01-01"),)
    client.shown = frozenset({ref(1)})
    store, key, checks, _ = setup(client)
    asyncio.run(check_to_end(checks, store, key))
    assert in_pool(store, key) == [ref(1)]
    assert store.marks(key) == {ref(2): "stacked_copy"}


def test_an_edited_photo_gets_its_fingerprint_in_its_revision():
    client = FakeClient([photo(1, is_edited=True)])
    store, key, checks, _ = setup(client)
    asyncio.run(check_to_end(checks, store, key))
    assert store.rows(key)[ref(1)].asset.revision == SHA + "-" + "f" * 16


def test_a_contact_lands_and_the_lists_follow():
    client = FakeClient()
    store, _, checks, _ = setup(client)
    asyncio.run(checks.contact("main"))
    connection = store.connection("main")
    assert connection.health.state == "ok" and connection.user == USER
    assert connection.counts.photos == 3
    assert connection.lists["albums"][0].name == "Garden"
    assert connection.lists["places"] == (Place(country="France"),)
    assert set(connection.lists) == {"albums", "people", "places", "tags"}


def test_a_list_that_fails_keeps_its_error_and_the_others_still_land():
    client = FakeClient()
    client.fail_on = "people"
    store, _, checks, _ = setup(client)
    asyncio.run(checks.contact("main"))
    connection = store.connection("main")
    assert connection.list_errors == {"people": "unreachable"}
    assert set(connection.lists) == {"albums", "places", "tags"}


def test_a_refused_contact_reads_needs_a_look_and_lists_nothing():
    client = FakeClient()
    client.fail_on = "contact"
    store, _, checks, _ = setup(client)
    asyncio.run(checks.contact("main"))
    health = store.connection("main").health
    assert isinstance(health, NeedsALook) and health.problem.kind == "unreachable"
    assert client.calls == ["contact"]


def test_a_new_key_gets_a_new_client():
    client = FakeClient()
    store, _, checks, secrets = setup(client)
    asyncio.run(checks.contact("main"))
    asyncio.run(checks.contact("main"))
    assert len(secrets) == 1
    store.connection("main").key = "key-two"
    asyncio.run(checks.contact("main"))
    assert len(secrets) == 2 and secrets[1].api_key.get_secret_value() == "key-two"
    assert client.closed   # the old key's client is closed


def test_tick_queues_due_contacts_and_due_pools():
    class Queue:
        def __init__(self):
            self.queued = []

        async def connection(self, ref):
            self.queued.append(("connection", ref))

        async def pool(self, key):
            self.queued.append(("pool", key))

    client = FakeClient()
    store, key, checks, _ = setup(client)
    checks.queue = Queue()
    asyncio.run(checks.tick())
    assert checks.queue.queued == [("connection", "main"), ("pool", key)]


def test_tick_without_a_queue_refuses():
    _, _, checks, _ = setup(FakeClient())
    with pytest.raises(RuntimeError):
        asyncio.run(checks.tick())
