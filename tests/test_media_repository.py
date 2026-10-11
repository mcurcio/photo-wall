"""Real PostgreSQL pool, job and queue tests of the media repository; transport and filesystem
are separate."""


import pytest
from media_queue import RecordingMediaQueue
from psycopg.types.json import Jsonb
from pydantic import ValidationError
from support.photo_pools import photo, seed_pool

from central.db import ProcessTransactionClock
from central.media_queue import PREPARE_MEDIA_TASK, ProcrastinateMediaQueue
from central.media_repository import MediaRepository, StoreLimits, lease_asset_of
from central.planner import AcquisitionRequest
from central.registry import RegistryError
from contracts.models import Variant


def setup_repository(registry, *, limits=None, count=1, queue=None):
    """A repository with the recipe set and one saved Source, `source:1`, whose pool holds
    `count` photos."""
    repo = MediaRepository(registry.db, registry.clock, limits,
                           queue=queue or RecordingMediaQueue(), times=ProcessTransactionClock(registry.clock))
    photos = tuple(photo(f"photo-{i + 1}", taken_at=900 + i) for i in range(count))
    seed_pool(registry, "source:1", photos)
    repo.set_recipe("a" * 64)
    return repo, photos


def request(original):
    return AcquisitionRequest(asset_id=original.asset_id, assignment_ids=("assignment",), earliest_start=1005)


def test_queue_idempotency_limit_and_reservation_pressure_are_transactional(registry):
    limits = StoreLimits(max_bytes=35, max_original_bytes=10, max_image_bytes=20, max_jobs=2)
    repo, originals = setup_repository(registry, limits=limits, count=3)
    assert repo.request_acquisitions((request(originals[0]), request(originals[1]))) == 2
    assert repo.request_acquisitions((request(originals[0]),)) == 0
    with pytest.raises(RegistryError, match="job_capacity"):
        repo.request_acquisitions((request(originals[2]),))
    lease = repo.claim_job()
    assert lease.reserved_bytes == 30
    assert repo.claim_job() is None
    assert repo.health()["accounted_bytes"] == 30
    assert repo.health()["worker_error"] == "storage_pressure"


def test_health_counts_only_the_current_recipes_jobs(registry):
    """The console's worker line reads these counts (slice 3 §14): a recipe change
    fails the old recipe's queued jobs, and those are not the worker's failures now."""
    repo, originals = setup_repository(registry, count=2)
    repo.request_acquisitions((request(originals[0]),))
    assert repo.health()["jobs"] == [{"state": "queued", "count": 1}]
    repo.set_recipe("b" * 64)  # the old recipe's queued job fails as recipe_changed
    assert repo.health()["jobs"] == []
    repo.request_acquisitions((request(originals[1]),))
    assert repo.health()["jobs"] == [{"state": "queued", "count": 1}]


def test_procrastinate_enqueue_rolls_back_with_domain_request(registry):
    queue = ProcrastinateMediaQueue(registry.db.dsn)
    queue.apply_schema(registry.db.dsn)
    limits = StoreLimits(max_jobs=1)
    repo, originals = setup_repository(registry, limits=limits, count=2, queue=queue)

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


def pool_repository(registry, **limits):
    repo = MediaRepository(registry.db, registry.clock, StoreLimits(**limits),
                           queue=RecordingMediaQueue(), times=ProcessTransactionClock(registry.clock))
    repo.set_recipe("a" * 64)
    return repo


def test_failed_unsecured_candidate_cooldown_is_not_misreported_as_empty_upstream(registry):
    repo = pool_repository(registry)
    held = photo("held", taken_at=900)
    seed_pool(registry, "family:1", (held,))
    repo.request_acquisitions((request(held),))
    lease = repo.claim_job()
    with repo.transaction() as conn:
        conn.execute("UPDATE media_jobs SET state='retry',retry_at=1010,failure_code='asset_missing',reserved_bytes=0 WHERE id=%s",
                     (lease.job_id,))
    registry.clock.wall = 1000  # the repository's media times follow this clock (ProcessTransactionClock)
    with registry.db.transaction() as conn:
        pool = repo.source_pools_in(conn, {"family:1"})["family:1"]
        assert pool.ready and len(pool.candidates) == 1
        assert pool.candidates[0].preparation_failure == "asset_missing"
        registry.clock.advance(11)
        assert repo.source_pools_in(conn, {"family:1"})["family:1"].candidates[0].preparation_failure is None


def test_source_pools_in_hydrates_a_ready_variant_and_leaves_out_an_unknown_source(registry):
    repo = pool_repository(registry)
    newer, older = photo("newer", taken_at=990), photo("older", taken_at=900)
    seed_pool(registry, "family:1", (older, newer))
    seed_pool(registry, "garden:1", (), complete=False)
    variant = Variant(sha256="c" * 64, size=10, media_type="image/jpeg", width=1080, height=1920)
    with repo.transaction() as conn:
        conn.execute("INSERT INTO media_blobs VALUES(%s,%s,10,'ready',1000)",
                     (variant.sha256, Jsonb(variant.model_dump(mode="json"))))
        conn.execute("INSERT INTO media_jobs(id,asset_id,recipe_id,state,earliest_start,variant_sha,"
                     "updated_at) VALUES('job-newer',%s,%s,'ready',1000,%s,1000)",
                     (newer.asset_id, "a" * 64, variant.sha256))
    with registry.db.transaction() as conn:
        pools = repo.source_pools_in(conn, {"family:1", "garden:1", "unknown:1"})
    assert set(pools) == {"family:1", "garden:1"}
    assert pools["family:1"].ready and [c.asset_id for c in pools["family:1"].candidates] == [
        newer.asset_id, older.asset_id]
    assert [c.variant for c in pools["family:1"].candidates] == [variant, None]
    assert pools["garden:1"].ready is False and pools["garden:1"].candidates == ()



def test_a_job_leases_its_pool_row_as_a_library_asset(registry):
    """`lease_asset_of` is the one rule: a row a pool check wrote leases its LibraryAsset; a
    row that is not one is refused."""
    repo, (pooled,) = setup_repository(registry)
    repo.request_acquisitions((request(pooled),))
    assert repo.claim_job().asset == pooled
    with registry.db.transaction() as conn:
        row = conn.execute("SELECT metadata,upstream_id FROM asset_revisions WHERE asset_id=%s",
                           (pooled.asset_id,)).fetchone()
    assert lease_asset_of(row) == pooled
    with pytest.raises(ValidationError):
        lease_asset_of({**row, "metadata": {"original_sha1": "0" * 40}})
