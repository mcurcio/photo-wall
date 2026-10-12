"""A pool page's budget (PR 81 review, 2026-10-11): normalising the pool rows moved the taken time
to the photo's one parent row (`library_assets`), so the newest-first page joins and orders the
pool through it. 50,000 in-pool photos, recorded 1,000 per step as a check does; the first page
and a page deep in the pool (the console asks one per scroll) each return one page in order. The
gate is that work. Their times are printed, never asserted: wall-clock time on shared CI runners
swings several-fold run to run, so the 0.5 s budget is a bench measurement."""
from __future__ import annotations

import time

from support.photo_pools import library_of, photo, seed_pool

from central.photo_library.model import InPool, PoolCursor
from central.photo_library.policy import POOL_PAGE_SIZE
from central.photo_library.ports import CheckComplete, CheckStep

PHOTOS = 50_000
STEP = 1_000


def test_a_page_of_fifty_thousand_photos_returns_one_page_in_order(registry):
    seed_pool(registry, "family:1", (), complete=False)
    library = library_of(registry)
    with registry.db.transaction() as conn:
        key = library.pool_key_of_source_in(conn, "family:1")
    lease = library.begin_check(key)
    for start in range(0, PHOTOS, STEP):
        entries = tuple(InPool(asset=photo(f"p{n}", taken_at=n)) for n in range(start, start + STEP))
        assert library.record_step(lease, CheckStep(progress={"page": start // STEP}, marks=(),
                                                    entries=entries, seen_so_far=start + STEP,
                                                    immich_up_to=None))
    assert library.finish_check(lease, CheckComplete())
    with registry.db.transaction() as conn:
        conn.execute("ANALYZE")
    library.source_pool("family:1", None)                      # a warm connection pool
    started = time.perf_counter()
    first = library.source_pool("family:1", None)
    first_seconds = time.perf_counter() - started
    middle = PoolCursor(taken_at=PHOTOS / 2, asset_id=first.items[0].asset_id)
    started = time.perf_counter()
    deep = library.source_pool("family:1", middle)
    deep_seconds = time.perf_counter() - started
    print(f"pool page of {PHOTOS}: first {first_seconds:.3f} s, deep {deep_seconds:.3f} s")
    assert first.in_pool.photos == PHOTOS and len(first.items) == POOL_PAGE_SIZE
    assert [item.taken_at for item in first.items] == [float(PHOTOS - 1 - n)
                                                        for n in range(POOL_PAGE_SIZE)]
    assert deep.items[0].taken_at < PHOTOS / 2 and len(deep.items) == POOL_PAGE_SIZE
