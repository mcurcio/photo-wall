"""Real PostgreSQL tests of the media repository: media variants on Asset records, their
preparation published in the caller's transaction, pool hydration, health; transport and
filesystem are separate."""

import pytest
from media_queue import RecordingMediaQueue
from support.frame_media import a_frame, seed_variant
from support.photo_pools import photo, seed_pool

from central.db import ProcessTransactionClock
from central.infra.photo_library_store import library_assets_in
from central.kernel.job_types import PrepareMedia
from central.kernel.types import media_variant_key
from central.media_queue import QueueReceipt
from central.media_repository import MediaRepository, StoreLimits, prepare_lock
from central.planner import AcquisitionRequest, MeasureRequest
from central.registry import RegistryError
from central.wall_plan.key_content import ANALYSIS_VERSION, InterestGrid, KeyContent, Measured
from contracts.models import Variant


def setup_repository(registry, *, limits=None, count=1, queue=None):
    """A repository over a wall with one Frame, and one saved Source, `source:1`, whose pool
    holds `count` photos."""
    repo = MediaRepository(registry.db, registry.clock, limits,
                           queue=queue or RecordingMediaQueue(),
                           times=ProcessTransactionClock(registry.clock))
    a_frame(registry)
    photos = tuple(photo(f"photo-{i + 1}", taken_at=900 + i) for i in range(count))
    seed_pool(registry, "source:1", photos)
    return repo, photos


def request(original):
    return AcquisitionRequest(asset_id=original.asset_id, assignment_ids=("assignment",),
                              earliest_start=1005)


def queued_jobs(registry) -> list[dict]:
    with registry.db.transaction() as conn:
        return conn.execute("SELECT task_name,args,status::text AS status FROM procrastinate_jobs "
                            "ORDER BY id").fetchall()


def test_a_planned_photo_is_named_and_its_preparation_published_with_the_callers_cut(registry):
    """Coordination's transaction holds the reference and the job: a rollback leaves neither,
    and a repeat merges into the pending copy."""
    repo, (first, second) = setup_repository(registry, count=2)
    with pytest.raises(RuntimeError, match="rolled back"), registry.db.transaction() as conn:
        assert repo.request_preparations_in(conn, (request(first),)) == 1
        raise RuntimeError("rolled back")
    assert queued_jobs(registry) == []
    with registry.db.transaction() as conn:
        assert repo.request_preparations_in(conn, (request(first), request(second))) == 2
        recipe = repo.wall_recipe_in(conn)
    with registry.db.transaction() as conn:
        assert repo.request_preparations_in(conn, (request(first),)) == 1  # merged
        recipes = conn.execute("SELECT recipe_id,need FROM media_recipes").fetchall()
        references = conn.execute("SELECT identity,owner FROM asset_references "
                                  "WHERE kind='media-variant' ORDER BY identity").fetchall()
    keys = sorted(media_variant_key(item.asset_id, recipe) for item in (first, second))
    assert sorted((row["task_name"], row["args"]["key"]) for row in queued_jobs(registry)) == [
        ("photo_wall." + PrepareMedia.job_name, key) for key in keys]
    assert [(row["identity"], row["owner"]) for row in references] == [
        (key, "frame-media") for key in keys]
    assert recipes == [{"recipe_id": recipe, "need": {"aspect": 1080 / 1920, "width": 1080,
                                                      "height": 1920}}]


def test_an_unknown_photo_or_a_wall_without_frames_publishes_nothing(registry):
    repo = MediaRepository(registry.db, registry.clock, queue=RecordingMediaQueue(),
                           times=ProcessTransactionClock(registry.clock))
    pooled = photo("photo-1", taken_at=900)
    seed_pool(registry, "source:1", (pooled,))
    with registry.db.transaction() as conn:
        assert repo.request_preparations_in(conn, (request(pooled),)) == 0  # no Frame yet
        assert repo.request_preparations_in(conn, ()) == 0
    a_frame(registry)
    with registry.db.transaction() as conn:
        assert repo.request_preparations_in(conn, (AcquisitionRequest(
            asset_id="asset-" + "0" * 64, assignment_ids=(), earliest_start=0),)) == 0
    assert queued_jobs(registry) == []


def test_a_repository_without_a_publisher_refuses_to_prepare(registry):
    repo, (pooled,) = setup_repository(registry)
    repo.publisher = None
    with pytest.raises(RegistryError, match="media_queue_unconfigured"), \
            registry.db.transaction() as conn:
        repo.request_preparations_in(conn, (request(pooled),))


def test_health_counts_preparations_by_their_latest_outcome_and_variant_bytes(registry):
    """The console's worker line reads these counts (slice 3 §14)."""
    repo, (ready, failed) = setup_repository(registry, count=2)
    seed_variant(registry, ready.asset_id, "c" * 64, size=10)
    with registry.db.transaction() as conn:
        recipe = repo.wall_recipe_in(conn)
        for item, status, reason in ((ready, "ok", None), (failed, "terminal", "unsupported_media")):
            conn.execute("INSERT INTO job_outcomes(lock_key,job_name,status,reason,"
                         "retry_not_before,seq,updated_at) VALUES(%s,%s,%s,%s,NULL,"
                         "nextval('job_outcome_seq'),1000)",
                         (prepare_lock(media_variant_key(item.asset_id, recipe)),
                          PrepareMedia.job_name, status, reason))
    health = repo.health()
    assert health["jobs"] == [{"state": "failed", "count": 1}, {"state": "ready", "count": 1}]
    assert health["accounted_bytes"] == 10


def pool_repository(registry, **limits):
    repo = MediaRepository(registry.db, registry.clock, StoreLimits(**limits),
                           queue=RecordingMediaQueue(),
                           times=ProcessTransactionClock(registry.clock))
    a_frame(registry)
    return repo


def test_a_failed_preparation_is_reported_until_its_retry_window_ends(registry):
    repo = pool_repository(registry)
    held = photo("held", taken_at=900)
    seed_pool(registry, "family:1", (held,))
    with registry.db.transaction() as conn:
        key = media_variant_key(held.asset_id, repo.wall_recipe_in(conn))
        conn.execute("INSERT INTO job_outcomes(lock_key,job_name,status,reason,retry_not_before,"
                     "seq,updated_at) VALUES(%s,%s,'transient','asset_missing',1010,"
                     "nextval('job_outcome_seq'),1000)", (prepare_lock(key), PrepareMedia.job_name))
    registry.clock.wall = 1000  # the repository's media times follow this clock
    with registry.db.transaction() as conn:
        pool = repo.source_pools_in(conn, {"family:1"})["family:1"]
        assert pool.ready and len(pool.candidates) == 1
        assert pool.candidates[0].preparation_failure == "asset_missing"
        registry.clock.advance(11)
        assert repo.source_pools_in(conn, {"family:1"})["family:1"].candidates[
            0].preparation_failure is None


def test_source_pools_in_hydrates_a_made_variant_and_leaves_out_an_unknown_source(registry):
    repo = pool_repository(registry)
    newer, older = photo("newer", taken_at=990), photo("older", taken_at=900)
    seed_pool(registry, "family:1", (older, newer))
    seed_pool(registry, "garden:1", (), complete=False)
    variant = seed_variant(registry, newer.asset_id, "c" * 64)
    with registry.db.transaction() as conn:
        pools = repo.source_pools_in(conn, {"family:1", "garden:1", "unknown:1"})
    assert set(pools) == {"family:1", "garden:1"}
    assert pools["family:1"].ready and [c.asset_id for c in pools["family:1"].candidates] == [
        newer.asset_id, older.asset_id]
    assert [c.variant for c in pools["family:1"].candidates] == [variant, None]
    assert variant == Variant(asset_id=newer.asset_id, recipe_id=variant.recipe_id,
                              sha256="c" * 64, size=10, media_type="image/jpeg", width=1080,
                              height=1920)
    assert pools["garden:1"].ready is False and pools["garden:1"].candidates == ()


class MeasureQueue(RecordingMediaQueue):
    def __init__(self):
        super().__init__()
        self.measured = []

    def enqueue_measure_in(self, conn, asset_id):
        self.measured.append(asset_id)
        return QueueReceipt(coalesced=False)


def test_source_pools_in_hydrates_key_content_and_a_measurement_is_requested_once(registry):
    """Roadmap 5a (M1): a measured photo carries its analysis; an unmeasured one carries None,
    and the scheduler's request queues it once."""
    queue = MeasureQueue()
    repo = MediaRepository(registry.db, registry.clock, StoreLimits(), queue=queue,
                           times=ProcessTransactionClock(registry.clock))
    measured, unmeasured = photo("measured", taken_at=990), photo("unmeasured", taken_at=900)
    seed_pool(registry, "family:1", (unmeasured, measured))
    key = KeyContent(analysis_version=ANALYSIS_VERSION,
                     interest=InterestGrid(columns=1, rows=1, weights=(1.0,)), spread=0.0)
    repo.key_content.record_measured(measured.asset_id, key)
    with registry.db.transaction() as conn:
        pool = repo.source_pools_in(conn, {"family:1"})["family:1"]
    assert [(c.asset_id, c.analysis) for c in pool.candidates] == [
        (measured.asset_id, Measured(key=key)), (unmeasured.asset_id, None)]
    wanted = tuple(MeasureRequest(asset_id=c.asset_id) for c in pool.candidates)
    assert repo.request_measurements(wanted) == 1
    assert repo.request_measurements(()) == 0
    assert queue.measured == [unmeasured.asset_id]



def test_one_read_turns_a_revision_and_its_immich_asset_into_one_library_asset(registry):
    """`library_assets_in` is the one rule: a revision's row joined to its Immich asset's row,
    the LibraryAsset the pool check wrote; an unknown id is absent."""
    _, (pooled,) = setup_repository(registry)
    with registry.db.transaction() as conn:
        assert library_assets_in(conn, [pooled.asset_id, "unknown"]) == {pooled.asset_id: pooled}
        assert library_assets_in(conn, []) == {}
