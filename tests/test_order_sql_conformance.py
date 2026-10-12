"""The order declaration's two interpreters agree (follow-up to 5a, slice S3; design rule 3):
central/wall_plan/order.py declares each Scene order and the tiers once; `PoolLines` interprets it
in Python and the store (`PgPhotoLibrary.ranked_line_in`) renders it to SQL. On one seeded pair of
overlapping pools, both rank every order, tier and wrap alike, at the whole line and at a short
limit, with the same shape filter, pool keys and wrap mark.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from support.photo_pools import photo, seed_pool

from central.catalog import Candidate, SourcePool
from central.infra.photo_library_store import PgPhotoLibrary
from central.wall_plan.order import (
    ORDERS,
    LineQuery,
    PoolLines,
    ShapeBounds,
    ShapeClass,
)


def _at(year: int, month: int, day: int, hour: int = 12, second: float = 0) -> float:
    return datetime(year, month, day, hour, tzinfo=UTC).timestamp() + second


NOW = _at(2026, 10, 11)
ANY_SHAPE = ShapeBounds(classes=(ShapeClass(video=True, min_ratio=1e-6, max_ratio=1e6),))
LANDSCAPE = ShapeBounds(classes=(ShapeClass(video=False, min_ratio=1.2, max_ratio=2.5,
                                            min_width=2000, min_height=1000),))


def _photos():
    """Mixed capture times: ties at one instant, On this day in earlier years (and either side
    of its UTC midnight), shapes that pass and fail the landscape class, and a video."""
    photos = [photo(f"plain{n:02d}", taken_at=_at(2020, 1, 1) + (n % 7) * 3600,
                    width=(3840, 2160, 1600)[n % 3], height=(2160, 3840, 900)[n % 3])
              for n in range(30)]
    photos += [photo(f"today{n}", taken_at=_at(2015 + n, 10, 11, hour=n)) for n in range(5)]
    photos += [photo("before-midnight", taken_at=_at(2019, 10, 10, hour=23, second=3599.5)),
               photo("after-midnight", taken_at=_at(2019, 10, 12, hour=0, second=0.5)),
               photo("clip", taken_at=_at(2021, 5, 5), width=1920, height=1080, kind="video")]
    return photos


@pytest.fixture
def line(registry):
    photos = _photos()
    family = seed_pool(registry, "family:1", photos[:28])
    garden = seed_pool(registry, "garden:1", photos[20:])        # eight photos in both
    with registry.db.transaction() as conn:
        members = PgPhotoLibrary.pool_members_in(conn, {"family:1", "garden:1"})
    pools = {}
    for ref, key in (("family:1", family), ("garden:1", garden)):
        pools[key] = SourcePool(source_ref=key, ready=True, candidates=tuple(
            Candidate(asset_id=m.asset_id, kind=m.kind, original_width=m.width,
                      original_height=m.height, captured_at=m.taken_at)
            for m in members[ref][0]))
    return registry, (family, garden), PoolLines(pools), photos


def test_sql_and_python_rank_every_order_tier_and_wrap_alike(line):
    registry, keys, python, photos = line
    ids = sorted({photo.asset_id for photo in photos})
    tied = next(p.asset_id for p in photos if p.taken_at == photos[7].taken_at
                and p.asset_id != photos[0].asset_id)
    afters = (None, photos[3].asset_id, tied, photos[31].asset_id, "not-in-the-line")
    tiers = ((frozenset(), frozenset()),
             (frozenset(ids[::3]), frozenset(ids[1::4])))
    compared = 0
    with registry.db.transaction() as conn:
        for order in ORDERS:
            for pass_number in (1, 2):
                for after in afters:
                    for picked, recent in tiers:
                        for shape in (ANY_SHAPE, LANDSCAPE):
                            for limit in (len(ids), 5):
                                query = LineQuery(
                                    pool_keys=tuple(sorted(keys)), scene_id="scene",
                                    pass_number=pass_number, order=order, after=after,
                                    now=NOW, picked=picked, recent=recent, shape=shape,
                                    limit=limit)
                                sql = PgPhotoLibrary.ranked_line_in(conn, query)
                                memory = python.line(query)
                                assert [(m.asset_id, frozenset(m.pool_keys))
                                        for m in sql.members] == [
                                    (m.candidate.asset_id, m.pools) for m in memory.members
                                ], (order, pass_number, after, shape, limit)
                                assert (sql.after and sql.after.asset_id) == (
                                    memory.after and memory.after.asset_id)
                                compared += 1
    assert compared == len(ORDERS) * 2 * len(afters) * len(tiers) * 2 * 2


def test_the_line_holds_each_photo_once_with_every_pool_that_has_it(line):
    registry, keys, _, photos = line
    with registry.db.transaction() as conn:
        answer = PgPhotoLibrary.ranked_line_in(conn, LineQuery(
            pool_keys=tuple(sorted(keys)), scene_id="scene", pass_number=1,
            order="newest_first", now=NOW, shape=ANY_SHAPE, limit=1000))
    assert len(answer.members) == len(photos)
    shared = [member for member in answer.members if len(member.pool_keys) == 2]
    assert len(shared) == 8
