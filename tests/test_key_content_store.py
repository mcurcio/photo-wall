"""Key content over PostgreSQL through its ports (roadmap 5a, run ledger slice M1): migration
082's `photo_key_content`, read by hydration, requested by the scheduler, written by the worker.
"""

from __future__ import annotations

import pytest
from support.photo_pools import photo, seed_pool

from central.db import ProcessTransactionClock
from central.infra.key_content_store import MEASURE_RETRY_SECONDS, PgKeyContent
from central.media_queue import (
    KEY_CONTENT_QUEUE_PREFIX,
    MEASURE_KEY_CONTENT_TASK,
    ProcrastinateMediaQueue,
    QueueReceipt,
)
from central.registry import RegistryError
from central.wall_plan.key_content import (
    ANALYSIS_VERSION,
    InterestGrid,
    KeyContent,
    Measured,
    MeasureFailed,
    UnitBox,
)

KEY = KeyContent(analysis_version=ANALYSIS_VERSION,
                 faces=(UnitBox(left=0.1, top=0.1, right=0.3, bottom=0.4),),
                 interest=InterestGrid(columns=2, rows=1, weights=(0.75, 0.25)))


class Queue:
    """Records measurement requests; one waits per photo until `run` empties it."""

    def __init__(self) -> None:
        self.waiting: list[str] = []

    def enqueue_measure_in(self, conn, asset_id):
        if asset_id in self.waiting:
            return QueueReceipt(coalesced=True)
        self.waiting.append(asset_id)
        return QueueReceipt(coalesced=False)


def store_of(registry, queue=None) -> PgKeyContent:
    return PgKeyContent(registry.db, times=ProcessTransactionClock(registry.clock), queue=queue)


def seeded(registry, *labels):
    photos = tuple(photo(label, taken_at=900 + i) for i, label in enumerate(labels))
    seed_pool(registry, "family:1", photos)
    return [p.asset_id for p in photos]


def analyses(registry, store, ids):
    with registry.db.transaction() as conn:
        return store.analyses_in(conn, ids, now=registry.clock.utc())


def request(registry, store, ids):
    with registry.db.transaction() as conn:
        return store.request_in(conn, ids, now=registry.clock.utc())


def test_a_measurement_is_queued_once_per_photo_and_never_for_a_measured_one(registry):
    queue = ProcrastinateMediaQueue(registry.db.dsn)
    queue.apply_schema(registry.db.dsn)
    store = store_of(registry, queue)
    measured, waiting, other = seeded(registry, "measured", "waiting", "other")
    store.record_measured(measured, KEY)

    assert request(registry, store, [waiting, measured, "asset-unknown", waiting]) == 1
    assert request(registry, store, [waiting, other]) == 1      # waiting coalesces
    with registry.db.transaction() as conn:
        jobs = conn.execute("SELECT task_name,args,queueing_lock,lock FROM procrastinate_jobs "
                            "ORDER BY id").fetchall()
    assert jobs == [
        {"task_name": MEASURE_KEY_CONTENT_TASK, "args": {"asset_id": asset_id},
         "queueing_lock": KEY_CONTENT_QUEUE_PREFIX + asset_id, "lock": None}
        for asset_id in (waiting, other)]
    assert analyses(registry, store, [measured, waiting]) == {measured: Measured(key=KEY)}


def test_a_failed_photo_reads_failed_until_its_retry_then_is_measured_again(registry):
    queue = Queue()
    store = store_of(registry, queue)
    (failed,) = seeded(registry, "failed")
    store.record_failed(failed, "decode")

    assert analyses(registry, store, [failed]) == {failed: MeasureFailed(reason="decode")}
    assert request(registry, store, [failed]) == 0
    registry.clock.advance(MEASURE_RETRY_SECONDS - 1)
    assert analyses(registry, store, [failed]) == {failed: MeasureFailed(reason="decode")}
    registry.clock.advance(1)
    assert analyses(registry, store, [failed]) == {}
    assert request(registry, store, [failed]) == 1 and queue.waiting == [failed]

    store.record_measured(failed, KEY)          # the new measurement replaces the failure
    assert analyses(registry, store, [failed]) == {failed: Measured(key=KEY)}


def test_a_row_at_another_analysis_version_or_unreadable_is_not_measured(registry):
    store = store_of(registry, Queue())
    older, unreadable = seeded(registry, "older", "unreadable")
    store.record_measured(older, KEY)
    store.record_measured(unreadable, KEY)
    with registry.db.transaction() as conn:
        conn.execute("UPDATE photo_key_content SET analysis_version='0' WHERE asset_id=%s",
                     (older,))
        conn.execute("UPDATE photo_key_content SET key='{\"interest\": 1}' WHERE asset_id=%s",
                     (unreadable,))
    assert analyses(registry, store, [older, unreadable]) == {}
    assert request(registry, store, [older, unreadable]) == 2


def test_the_asset_row_carries_its_key_content_with_it(registry):
    store = store_of(registry)
    (gone,) = seeded(registry, "gone")
    assert store.asset_to_measure(gone).asset_id == gone
    store.record_measured(gone, KEY)
    with registry.db.transaction() as conn:
        conn.execute("DELETE FROM photo_pool_rows WHERE asset_id=%s", (gone,))
        conn.execute("DELETE FROM asset_revisions WHERE asset_id=%s", (gone,))
        assert conn.execute("SELECT count(*) AS n FROM photo_key_content").fetchone()["n"] == 0
    assert store.asset_to_measure(gone) is None
    store.record_measured(gone, KEY)            # a measurement finishing late writes nothing
    store.record_failed(gone, "timeout")
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM photo_key_content").fetchone()["n"] == 0


def test_requesting_without_a_queue_refuses_only_when_something_is_due(registry):
    store = store_of(registry)
    (due,) = seeded(registry, "due")
    assert request(registry, store, []) == 0
    with pytest.raises(RegistryError, match="media_queue_unconfigured"):
        request(registry, store, [due])
