"""The one-item re-check before removal (follow-up S4).

A complete walk of Immich's include query can miss a photo that still matches: its capture time
moved behind the keyset edge while the check ran. So a row the walk neither saw nor marked is
asked about alone (`LibraryClient.still_matches`), one photo per step, and leaves only when
Immich no longer answers it. The first half runs the worker over the fake client and store
(tests/test_library_checks.py's); the second runs the real `ImmichClient` against the Immich
3.1.0 fixture (CI's photo-sources leg), each test with its own tag and uploads.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from support.immich_library import run
from support.library_store_fake import check_to_end, immich_checks
from test_library_checks import FakeClient, in_pool, photo, ref, setup

from central.photo_library.model import ConnectionSecret, Exclusions, SourceQuery
from central.photo_library.ports import CheckComplete, CheckFailed
from contracts.time import ManualClock
from media.immich import ImmichClient

# --- The worker, over the fake client and store ----------------------------------------------


def _checked(client, **query):
    store, key, checks, _ = setup(client, **query)
    asyncio.run(check_to_end(checks, store, key))
    assert isinstance(store.pools[key].outcome, CheckComplete)
    store.pools[key].requested = True
    client.calls.clear()
    return store, key, checks


def test_a_photo_whose_capture_time_moves_behind_the_keyset_edge_mid_check_stays():
    """Mutation probe 1: sweep without re-checking (the finish removes unseen rows and the walk
    goes straight to the finish)."""
    client = FakeClient([photo(n) for n in range(1, 6)])
    store, key, checks = _checked(client)
    for _ in range(2):                                 # up_to, then the first include page
        asyncio.run(checks.check_pool(key))
    moved = client.photos.pop()                        # ref(5), the oldest, not read yet...
    client.photos.insert(0, moved)                     # ...is now the newest: behind the edge
    asyncio.run(check_to_end(checks, store, key))
    assert isinstance(store.pools[key].outcome, CheckComplete)
    assert in_pool(store, key) == [ref(n) for n in range(1, 6)]
    assert client.calls.count("recheck") == 1          # the walk missed only ref(5)


def test_a_deleted_or_untagged_photo_is_removed_after_its_own_re_check():
    """Mutation probe 2: read an empty answer as a match."""
    client = FakeClient([photo(n) for n in range(1, 6)])
    store, key, checks = _checked(client)
    del client.photos[3]                               # ref(4): deleted, or its tag removed
    del client.photos[0]                               # ref(1)
    asyncio.run(check_to_end(checks, store, key))
    assert in_pool(store, key) == [ref(2), ref(3), ref(5)]
    assert client.calls.count("recheck") == 2


def test_a_re_check_that_fails_removes_nothing():
    client = FakeClient([photo(n) for n in range(1, 4)])
    store, key, checks = _checked(client)
    client.photos = client.photos[:1]
    client.fail_on = "recheck"
    asyncio.run(check_to_end(checks, store, key))
    assert store.pools[key].outcome == CheckFailed(problem="connection")
    assert in_pool(store, key) == [ref(1), ref(2), ref(3)]


def test_a_photo_excluded_in_this_check_counts_as_gone_without_a_re_check():
    client = FakeClient([photo(n) for n in range(1, 4)])
    store, key, checks = _checked(client, exclude=Exclusions(people=(ref(60),),
                                                             screenshots=False))
    client.excluded = [ref(2)]
    asyncio.run(check_to_end(checks, store, key))
    assert in_pool(store, key) == [ref(1), ref(3)]
    assert "recheck" not in client.calls


# --- The real ImmichClient, against the Immich 3.1.0 fixture ----------------------------------


def _iso(instant: float) -> str:
    return datetime.fromtimestamp(instant, UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


@pytest.fixture
def tagged(immich_library):
    """Three photos under this test's own tag, a minute apart (the newest first), its id, and
    the set a test adds the ids it deleted to; the rest are deleted after the test, so the
    shared fixture keeps its counts for the tests after this one."""
    path = "photo-wall-recheck-" + uuid.uuid4().hex[:10]
    base = datetime(2021, 6, 1, 12, tzinfo=UTC).timestamp()
    photos = [immich_library.upload(f"{path}-{n}", taken=_iso(base - 60 * n), tags=[path])
              for n in range(3)]
    admin = immich_library._admin()
    try:
        tag_id = admin.tag([path], [])[path]
    finally:
        admin.close()
    deleted: set[str] = set()
    yield tag_id, [asset.upstream_id for asset in photos], deleted
    for asset in photos:
        if asset.upstream_id not in deleted:
            immich_library.delete(asset.upstream_id)


@pytest.fixture
def client(immich_library):
    made = ImmichClient.for_connection(ConnectionSecret(
        connection_ref="main", base_url=immich_library.base_url,
        api_key=SecretStr(immich_library.api_key)))
    yield made
    run(made.close())


def _query(tag_id: str) -> SourceQuery:
    return SourceQuery(connection_ref="main", tags=(tag_id,),
                       exclude=Exclusions(screenshots=False))


def _untag(immich_library, tag_id: str, upstream_id: str) -> None:
    admin = immich_library._admin()
    try:
        admin.request("DELETE", f"/tags/{tag_id}/assets", json={"ids": [upstream_id]})
    finally:
        admin.close()


def test_immich_answers_one_photo_while_it_matches_and_not_once_untagged_or_deleted(
        immich_library, tagged, client):
    tag_id, (first, second, third), deleted = tagged
    query = _query(tag_id)
    assert run(client.still_matches(query, first)) is True
    other = next(asset.upstream_id for asset in immich_library.assets.values()
                 if asset.kind == "image")
    assert run(client.still_matches(query, other)) is False      # never under this tag
    _untag(immich_library, tag_id, second)
    immich_library.delete(third)
    deleted.add(third)
    assert run(client.still_matches(query, second)) is False
    assert run(client.still_matches(query, third)) is False
    assert run(client.still_matches(query, first)) is True


def test_a_real_check_keeps_a_moved_photo_and_removes_an_untagged_one(immich_library, tagged):
    """The walk at page size 1, against real Immich: once it has read the newest photo, the
    oldest's capture time moves ahead of that one (behind the keyset edge) and the middle one
    is untagged; the complete check keeps the first and removes the second."""
    tag_id, (newest, middle, oldest), _ = tagged
    store, checks = immich_checks(immich_library.base_url, immich_library.api_key,
                                  ManualClock(wall=time.time()), page_size=1)
    try:
        run(checks.contact("main"))
        key = store.add_pool(_query(tag_id))
        run(check_to_end(checks, store, key))
        assert set(in_pool(store, key)) == {newest, middle, oldest}
        store.pools[key].requested = True
        for _ in range(2):                             # up_to, then the page holding `newest`
            run(checks.check_pool(key))
        moved = _iso(datetime(2021, 6, 1, 13, tzinfo=UTC).timestamp())
        admin = immich_library._admin()
        try:
            admin.request("PUT", "/assets/" + oldest, json={"dateTimeOriginal": moved})
            admin.wait_until_read(oldest, moved, deadline=time.monotonic() + 60)
        finally:
            admin.close()
        _untag(immich_library, tag_id, middle)
        run(check_to_end(checks, store, key))
        assert isinstance(store.pools[key].outcome, CheckComplete)
        assert set(in_pool(store, key)) == {newest, oldest}
    finally:
        run(checks.close())
