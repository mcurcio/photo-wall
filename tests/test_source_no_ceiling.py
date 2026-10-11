"""No ceiling on a pool, and a check removes only when it completes (roadmap 4, slice M2).

Real Immich (the fixture, slice FX1), the real `ImmichClient` at page size 3, `LibraryChecks`
over the fake store. Owner 2026-10-09: "i dont want any limit on the count of a source".
"""

import os
import time
import uuid
from collections import Counter

import pytest
from pydantic import SecretStr
from support.immich_library import run
from support.library_store_fake import check_to_end, immich_checks

from central.photo_library.model import ConnectionSecret, InPool, SourceQuery
from contracts.time import ManualClock
from media import pool_check
from media.immich import ImmichClient, LibraryLimits
from media.library_client import TakenWindow

if not os.environ.get("PHOTO_WALL_IMMICH_FIXTURE"):
    pytest.skip("set PHOTO_WALL_IMMICH_FIXTURE", allow_module_level=True)

pytestmark = pytest.mark.immich


@pytest.fixture
def small_pages(immich_library):
    store, checks = immich_checks(immich_library.base_url, immich_library.api_key,
                                  ManualClock(wall=time.time()), page_size=3)
    run(checks.contact("main"))
    yield store, checks
    run(checks.close())


def in_pool(store, key):
    return {ref for ref, entry in store.rows(key).items() if isinstance(entry, InPool)}


def test_equal_capture_times_lose_nothing_at_page_size_three(immich_library, small_pages):
    store, checks = small_pages
    added = Counter()
    record_step = store.record_step

    def counting(lease, step):
        added.update(e.asset.upstream_id for e in step.entries if isinstance(e, InPool))
        return record_step(lease, step)
    store.record_step = counting
    key = store.add_pool(SourceQuery(connection_ref="main"))
    run(check_to_end(checks, store, key))
    same = {immich_library.assets[f"same-instant-{n}"].upstream_id for n in range(1, 9)}
    assert same <= in_pool(store, key)
    assert all(added[ref] == 1 for ref in same)
    assert store.pools[key].outcome.outcome == "complete"


async def _ids(client, page_of):
    found, position = [], None
    while True:
        page = await page_of(position)
        found.extend(page.items if hasattr(page, "items") else page.ids)
        if page.next is None:
            return found
        position = page.next


def test_the_pool_is_the_include_matches_less_exclusions_copies_and_partners(
        immich_library, small_pages):
    store, checks = small_pages
    query = SourceQuery(connection_ref="main")
    key = store.add_pool(query)
    run(check_to_end(checks, store, key))

    async def expected():
        client = ImmichClient.for_connection(ConnectionSecret(
            connection_ref="main", base_url=immich_library.base_url,
            api_key=SecretStr(immich_library.api_key)), limits=LibraryLimits(page_size=3))
        try:
            include = await _ids(client, lambda after: client.search_page(
                query, TakenWindow(), after))
            excluded = set()
            for exclusion in pool_check.exclusions_of(query):
                excluded.update(await _ids(client, lambda after, e=exclusion:
                                           client.exclusion_page(e, after)))
            return include, excluded
        finally:
            await client.close()
    include, excluded = run(expected())
    copy = immich_library.assets["stack-copy"].upstream_id
    partners = {item.upstream_id for item in include if item.owner_ref != immich_library.owner_ref}
    playable = {item.upstream_id for item in include if item.problem is None and item.width}
    assert in_pool(store, key) == playable - excluded - {copy} - partners
    assert copy in {item.upstream_id for item in include}            # Immich matched it
    assert immich_library.assets["partner-anna-ben"].upstream_id in partners


def test_a_new_photo_joins_and_a_deleted_one_leaves_at_a_complete_check(
        immich_library, small_pages):
    store, checks = small_pages
    tag = f"no-ceiling-{uuid.uuid4().hex[:8]}"
    uploaded = immich_library.upload(tag, taken="2021-06-01T12:00:00.000Z", tags=(tag,))

    async def tag_ref():
        client = ImmichClient.for_connection(ConnectionSecret(
            connection_ref="main", base_url=immich_library.base_url,
            api_key=SecretStr(immich_library.api_key)))
        try:
            return next(t.tag_ref for t in await client.tags() if t.path == tag)
        finally:
            await client.close()
    deleted = False
    try:
        key = store.add_pool(SourceQuery(connection_ref="main", tags=(run(tag_ref()),)))
        run(check_to_end(checks, store, key))
        assert in_pool(store, key) == {uploaded.upstream_id}
        immich_library.delete(uploaded.upstream_id)
        deleted = True
        store.pools[key].requested = True
        run(check_to_end(checks, store, key))
        assert in_pool(store, key) == set()
        assert store.pools[key].outcome.outcome == "complete"
    finally:
        if not deleted:   # the shared fixture keeps its counts for the tests after this one
            immich_library.delete(uploaded.upstream_id)


def test_a_check_stopped_after_two_steps_removes_nothing(small_pages):
    store, checks = small_pages
    key = store.add_pool(SourceQuery(connection_ref="main"))
    run(check_to_end(checks, store, key))
    before = dict(store.rows(key))
    store.pools[key].requested = True
    for _ in range(2):
        run(checks.check_pool(key))
    assert store.pools[key].checking
    assert dict(store.rows(key)) == before
