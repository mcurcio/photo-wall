"""Real PostgreSQL membership/queue tests; transport and filesystem are separate."""

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from media_queue import RecordingMediaQueue
from psycopg.types.json import Jsonb

from central.catalog import CatalogSnapshot
from central.media_ports import RefreshReceipt
from central.media_queue import (
    MEDIA_REFRESH_LOCK_PREFIX,
    PREPARE_MEDIA_TASK,
    REFRESH_MEDIA_SOURCE_TASK,
    ProcrastinateMediaQueue,
)
from central.media_repository import MediaRepository, StoreLimits
from central.planner import AcquisitionRequest
from central.registry import RegistryError
from media.models import OriginalAsset, RefreshResult, SourceSpec


def asset(number=1):
    return OriginalAsset(connection_id="fixture", upstream_id=str(uuid.UUID(int=number)),
        original_sha1=f"{number:040x}", kind="image", raw_width=30, raw_height=50,
        orientation=1, captured_at=900, file_size=10)


def result(spec, *assets, status="ok"):
    return RefreshResult(snapshot=CatalogSnapshot(source_ref=spec.source_ref, refreshed_at=1000,
        status=status, candidates=tuple(a.candidate for a in assets)), assets=assets)


def setup_repository(registry, *, limits=None, count=1, queue=None):
    repo = MediaRepository(registry.db, registry.clock, limits,
                           queue=queue or RecordingMediaQueue())
    spec = SourceSpec(source_ref="source:1", connection_ref="fixture", favorites=True)
    repo.configure_source(spec)
    lease = repo.begin_scheduled_refresh()
    originals = tuple(asset(i+1) for i in range(count))
    repo.publish_refresh(lease, result(spec, *originals))
    repo.set_recipe("a" * 64)
    return repo, spec, originals


def request(original):
    return AcquisitionRequest(asset_id=original.asset_id, assignment_ids=("assignment",), earliest_start=1005)


def test_source_versions_are_immutable_and_stale_refresh_cannot_replace_members(registry):
    repo, spec, originals = setup_repository(registry)
    assert not repo.configure_source(spec)
    with pytest.raises(RegistryError, match="source_revision_immutable"):
        repo.configure_source(spec.model_copy(update={"favorites": False}))
    registry.clock.advance(31)
    first = repo.begin_scheduled_refresh()
    registry.clock.advance(91)
    newer = repo.begin_scheduled_refresh()
    assert not repo.publish_refresh(first, result(spec))
    assert repo.publish_refresh(newer, result(spec, *originals))
    with registry.db.transaction() as conn:
        snapshots, _ = repo.catalog_in(conn, registry.clock.utc())
        assert [c.asset_id for c in snapshots[spec.source_ref].candidates] == [originals[0].asset_id]


def test_failed_refresh_preserves_membership_and_distinct_latest_status(registry):
    repo, spec, originals = setup_repository(registry)
    registry.clock.advance(31)
    repo.publish_refresh(repo.begin_scheduled_refresh(), result(spec, status="permission"))
    with registry.db.transaction() as conn:
        snapshot = repo.catalog_in(conn, registry.clock.utc())[0][spec.source_ref]
        assert snapshot.status == "permission" and len(snapshot.candidates) == 1
    assert repo.sources()[0]["last_success"] == 1000


def test_refresh_capacity_rejects_replacement_before_displacing_catalog(registry):
    limits = StoreLimits(max_authored_candidates=2)
    repo, spec, originals = setup_repository(registry, limits=limits)
    extra = asset(2)
    with repo.transaction() as conn:
        conn.execute("INSERT INTO authored_candidates(asset_id,candidate,source_ref,authored_at) "
                     "VALUES(%s,%s,%s,%s)",
                     (extra.asset_id, Jsonb(extra.candidate.model_dump(mode="json")), spec.source_ref, 1000))
    registry.clock.advance(31)
    lease = repo.begin_scheduled_refresh()
    with pytest.raises(RegistryError, match="metadata_capacity"):
        repo.publish_refresh(lease, result(spec, originals[0], extra))
    with registry.db.transaction() as conn:
        snapshot = CatalogSnapshot.model_validate(conn.execute(
            "SELECT snapshot FROM catalog_snapshots WHERE source_ref=%s", (spec.source_ref,)
        ).fetchone()["snapshot"])
        assert [candidate.asset_id for candidate in snapshot.candidates] == [originals[0].asset_id]
        assert conn.execute("SELECT count(*) AS n FROM source_members WHERE source_ref=%s",
                            (spec.source_ref,)).fetchone()["n"] == 1


def test_original_geometry_cannot_change_without_new_byte_revision(registry):
    repo, spec, originals = setup_repository(registry)
    registry.clock.advance(31)
    lease = repo.begin_scheduled_refresh()
    changed = originals[0].model_copy(update={"raw_width": 50, "raw_height": 30})
    with pytest.raises(RegistryError, match="original_metadata_conflict"):
        repo.publish_refresh(lease, result(spec, changed))
    with registry.db.transaction() as conn:
        candidate = repo.catalog_in(conn, registry.clock.utc())[0][spec.source_ref].candidates[0]
        assert (candidate.original_width, candidate.original_height) == (30, 50)


def test_queue_idempotency_limit_and_reservation_pressure_are_transactional(registry):
    limits = StoreLimits(max_bytes=35, max_original_bytes=10, max_image_bytes=20, max_jobs=2)
    repo, spec, originals = setup_repository(registry, limits=limits, count=3)
    assert repo.request_acquisitions((request(originals[0]), request(originals[1]))) == 2
    assert repo.request_acquisitions((request(originals[0]),)) == 0
    with pytest.raises(RegistryError, match="job_capacity"):
        repo.request_acquisitions((request(originals[2]),))
    lease = repo.claim_job()
    assert lease.reserved_bytes == 30
    assert repo.claim_job() is None
    assert repo.health()["accounted_bytes"] == 30
    assert repo.health()["worker_error"] == "storage_pressure"


def test_procrastinate_enqueue_rolls_back_with_domain_request(registry):
    queue = ProcrastinateMediaQueue(registry.db.dsn)
    queue.apply_schema(registry.db.dsn)
    limits = StoreLimits(max_jobs=1)
    repo, _, originals = setup_repository(registry, limits=limits, count=2, queue=queue)

    with pytest.raises(RegistryError, match="job_capacity"):
        repo.request_acquisitions((request(originals[0]), request(originals[1])))
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM media_jobs").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM procrastinate_jobs").fetchone()["n"] == 0

    assert repo.request_acquisitions((request(originals[0]),)) == 1
    with registry.db.transaction() as conn:
        queued = conn.execute(
            "SELECT task_name,args FROM procrastinate_jobs"
        ).fetchone()
        domain = conn.execute("SELECT id,state FROM media_jobs").fetchone()
    assert queued == {"task_name": PREPARE_MEDIA_TASK, "args": {"job_id": domain["id"]}}
    assert domain["state"] == "queued"


def test_refresh_request_and_queue_job_commit_and_roll_back_atomically(registry):
    queue = ProcrastinateMediaQueue(registry.db.dsn)
    queue.apply_schema(registry.db.dsn)
    repo, spec, _ = setup_repository(registry, queue=queue)
    before = repo.sources()[0]["next_refresh"]

    class FailingQueue:
        def enqueue_in(self, conn, job_id):
            return queue.enqueue_in(conn, job_id)

        def enqueue_refresh_in(self, conn, source_ref):
            queue.enqueue_refresh_in(conn, source_ref)
            raise RuntimeError("forced_rollback")

    repo.queue = FailingQueue()
    with pytest.raises(RuntimeError, match="forced_rollback"):
        repo.request_refresh(spec.source_ref)
    with registry.db.transaction() as conn:
        source = conn.execute(
            "SELECT next_refresh,refresh_requested_revision FROM media_sources"
        ).fetchone()
        assert source == {"next_refresh": before, "refresh_requested_revision": 0}
        assert conn.execute(
            "SELECT count(*) AS n FROM procrastinate_jobs"
        ).fetchone()["n"] == 0

    repo.queue = queue
    receipt = repo.request_refresh(spec.source_ref)
    with registry.db.transaction() as conn:
        source = conn.execute(
            "SELECT next_refresh,refresh_requested_revision FROM media_sources"
        ).fetchone()
        job = conn.execute(
            "SELECT task_name,args,status FROM procrastinate_jobs"
        ).fetchone()
    assert receipt.requested_revision == 1 and not receipt.coalesced
    assert source == {"next_refresh": before, "refresh_requested_revision": 1}
    assert job == {
        "task_name": REFRESH_MEDIA_SOURCE_TASK,
        "args": {"source_ref": spec.source_ref},
        "status": "todo",
    }


def test_refresh_receipt_requires_a_newer_requested_revision():
    with pytest.raises(ValueError, match="completed_revision must precede requested_revision"):
        RefreshReceipt(
            source_ref="source:1",
            requested_revision=2,
            completed_revision=2,
            coalesced=True,
        )


def test_concurrent_refresh_requests_coalesce_per_source_without_losing_versions(registry):
    queue = ProcrastinateMediaQueue(registry.db.dsn)
    queue.apply_schema(registry.db.dsn)
    repo, spec, _ = setup_repository(registry, queue=queue)

    with ThreadPoolExecutor(max_workers=6) as pool:
        receipts = list(pool.map(lambda _: repo.request_refresh(spec.source_ref), range(6)))

    assert sorted(receipt.requested_revision for receipt in receipts) == list(range(1, 7))
    assert sum(not receipt.coalesced for receipt in receipts) == 1
    assert repo.refresh_revisions(spec.source_ref) == (6, 0)
    with registry.db.transaction() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM procrastinate_jobs WHERE status='todo'"
        ).fetchone()["n"] == 1


def test_refresh_queueing_is_source_scoped(registry):
    queue = ProcrastinateMediaQueue(registry.db.dsn)
    queue.apply_schema(registry.db.dsn)
    repo, _, _ = setup_repository(registry, queue=queue)
    other = SourceSpec(source_ref="source:2", connection_ref="fixture")
    repo.configure_source(other)

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(repo.request_refresh, ("source:1", "source:2")))

    assert {receipt.source_ref for receipt in receipts} == {"source:1", "source:2"}
    with registry.db.transaction() as conn:
        jobs = conn.execute(
            "SELECT args,lock FROM procrastinate_jobs WHERE task_name=%s ORDER BY args->>'source_ref'",
            (REFRESH_MEDIA_SOURCE_TASK,),
        ).fetchall()
    assert jobs == [
        {"args": {"source_ref": "source:1"}, "lock": MEDIA_REFRESH_LOCK_PREFIX + "source:1"},
        {"args": {"source_ref": "source:2"}, "lock": MEDIA_REFRESH_LOCK_PREFIX + "source:2"},
    ]


def test_request_during_active_refresh_waits_then_eventually_completes(registry):
    queue = RecordingMediaQueue()
    repo, spec, originals = setup_repository(registry, queue=queue)

    first = repo.request_refresh(spec.source_ref)
    active = repo.begin_requested_refresh(spec.source_ref)
    second = repo.request_refresh(spec.source_ref)

    assert (first.requested_revision, second.requested_revision) == (1, 2)
    assert repo.begin_requested_refresh(spec.source_ref) is None
    assert repo.publish_refresh(active, result(spec, *originals))
    assert repo.refresh_revisions(spec.source_ref) == (2, 1)
    follow_up = repo.begin_requested_refresh(spec.source_ref)
    assert follow_up is not None and follow_up.request_revision == 2
    assert repo.publish_refresh(follow_up, result(spec, *originals))
    assert repo.refresh_revisions(spec.source_ref) == (2, 2)


def test_stale_requested_lease_cannot_overwrite_newer_completed_revision(registry):
    queue = RecordingMediaQueue()
    repo, spec, originals = setup_repository(registry, queue=queue)
    repo.request_refresh(spec.source_ref)
    stale = repo.begin_requested_refresh(spec.source_ref)
    registry.clock.advance(91)
    repo.request_refresh(spec.source_ref)
    current = repo.begin_requested_refresh(spec.source_ref)

    assert current.request_revision == 2
    assert repo.publish_refresh(current, result(spec, *originals))
    assert repo.refresh_revisions(spec.source_ref) == (2, 2)
    assert not repo.publish_refresh(stale, result(spec, status="permission"))
    assert repo.refresh_revisions(spec.source_ref) == (2, 2)
    with registry.db.transaction() as conn:
        source = repo.catalog_in(conn, registry.clock.utc())[0][spec.source_ref]
    assert source.status == "ok"
    assert [candidate.asset_id for candidate in source.candidates] == [originals[0].asset_id]


def test_failed_unsecured_candidate_cooldown_is_not_misreported_as_empty_upstream(registry):
    repo, spec, originals = setup_repository(registry)
    repo.request_acquisitions((request(originals[0]),))
    lease = repo.claim_job()
    with repo.transaction() as conn:
        conn.execute("UPDATE media_jobs SET state='retry',retry_at=1010,failure_code='asset_missing',reserved_bytes=0 WHERE id=%s",
                     (lease.job_id,))
    with registry.db.transaction() as conn:
        snapshot = repo.catalog_in(conn, 1000)[0][spec.source_ref]
        assert snapshot.status == "ok" and len(snapshot.candidates) == 1
        assert snapshot.candidates[0].preparation_failure == "asset_missing"
        assert repo.catalog_in(conn, 1011)[0][spec.source_ref].candidates[0].preparation_failure is None
