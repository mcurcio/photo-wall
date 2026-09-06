"""Real PostgreSQL membership/queue tests; transport and filesystem are separate."""

import uuid

import pytest
from psycopg.types.json import Jsonb

from central.catalog import CatalogSnapshot
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


def setup_repository(registry, *, limits=None, count=1):
    repo = MediaRepository(registry.db, registry.clock, limits)
    spec = SourceSpec(source_ref="source:1", connection_ref="fixture", favorites=True)
    repo.configure_source(spec)
    lease = repo.begin_refresh()
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
    first = repo.begin_refresh()
    registry.clock.advance(91)
    newer = repo.begin_refresh()
    assert not repo.publish_refresh(first, result(spec))
    assert repo.publish_refresh(newer, result(spec, *originals))
    with registry.db.transaction() as conn:
        snapshots, _ = repo.catalog_in(conn, registry.clock.utc())
        assert [c.asset_id for c in snapshots[spec.source_ref].candidates] == [originals[0].asset_id]


def test_failed_refresh_preserves_membership_and_distinct_latest_status(registry):
    repo, spec, originals = setup_repository(registry)
    registry.clock.advance(31)
    repo.publish_refresh(repo.begin_refresh(), result(spec, status="permission"))
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
    lease = repo.begin_refresh()
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
    lease = repo.begin_refresh()
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
