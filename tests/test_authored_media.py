"""Bounded authored-media references and their live worker hydration."""

import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient
from media_queue import RecordingMediaQueue
from psycopg.types.json import Jsonb

from central.app import create_app
from central.catalog import CatalogSnapshot
from central.media_repository import MediaRepository, StoreLimits
from central.planner import AcquisitionRequest
from central.runtime import Contribution, Runtime, Scene
from contracts.models import Variant
from media.models import OriginalAsset, RefreshResult, SourceSpec
from media.prepare import BuildIdentity, PreparedMedia

ADMIN = "test-operator-" + "x" * 40
RECIPE = "a" * 64


def asset(number=1, *, width=30, height=50, original_bytes=None):
    original_sha1 = hashlib.sha1(original_bytes).hexdigest() if original_bytes is not None else f"{number:040x}"
    return OriginalAsset(connection_id="fixture", upstream_id=str(uuid.UUID(int=number)),
                         original_sha1=original_sha1, kind="image", raw_width=width,
                         raw_height=height, orientation=1, captured_at=900 + number,
                         file_size=len(original_bytes) if original_bytes is not None else 10)


def refresh(spec, *assets):
    return RefreshResult(snapshot=CatalogSnapshot(source_ref=spec.source_ref, refreshed_at=1000,
        status="ok", candidates=tuple(item.candidate for item in assets)), assets=assets)


def setup_source(registry, count=1, *, limits=None, assets=None):
    repo = MediaRepository(registry.db, registry.clock, limits, queue=RecordingMediaQueue())
    spec = SourceSpec(source_ref="source:1", connection_ref="fixture", favorites=True)
    repo.configure_source(spec)
    lease = repo.begin_refresh()
    originals = tuple(assets) if assets is not None else tuple(asset(i + 1) for i in range(count))
    repo.publish_refresh(lease, refresh(spec, *originals))
    repo.set_recipe(RECIPE)
    return repo, spec, originals


def client_for(registry):
    return TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False))


def test_authored_api_is_neutral_idempotent_and_membership_bound(registry):
    repo, spec, originals = setup_source(registry, count=1)
    with client_for(registry) as client:
        headers = {"Authorization": "Bearer " + ADMIN}
        response = client.post("/v1/operator/authored-candidates", headers=headers,
                               json={"source_ref": spec.source_ref, "asset_ids": [originals[0].asset_id]})
        assert response.status_code == 200
        assert response.json() == {"asset_refs": [originals[0].asset_id], "created": 1}
        assert client.post("/v1/operator/authored-candidates", headers=headers,
                           json={"source_ref": spec.source_ref, "asset_ids": [originals[0].asset_id]}).json()["created"] == 0
        source = client.get(f"/v1/operator/sources/{spec.source_ref}/candidates", headers=headers)
        assert source.status_code == 200
        candidate = source.json()["candidates"][0]
        assert set(candidate) == {"asset_id", "kind", "original_width", "original_height",
                                  "captured_at", "variant", "preparation_failure"}
        assert all(secret not in source.text for secret in ("fixture-1", "connection_ref", "upstream"))
        assert client.post("/v1/operator/authored-candidates", headers=headers,
                           json={"source_ref": spec.source_ref, "asset_ids": [originals[0].asset_id] * 2}).status_code == 422


def test_authored_api_distinguishes_unknown_and_nonmember(registry):
    repo, spec, originals = setup_source(registry)
    with repo.transaction() as conn:
        other = asset(2)
        conn.execute("INSERT INTO asset_revisions VALUES(%s,%s,1000,1000)",
                     (other.asset_id, Jsonb(other.model_dump(mode="json"))))
    with client_for(registry) as client:
        headers = {"Authorization": "Bearer " + ADMIN}
        body = {"source_ref": spec.source_ref, "asset_ids": [asset(2).asset_id]}
        assert client.post("/v1/operator/authored-candidates", headers=headers, json=body).json() == {
            "error": "authored_asset_not_member"}
        body["asset_ids"] = ["asset-never-seen"]
        assert client.post("/v1/operator/authored-candidates", headers=headers, json=body).json() == {
            "error": "authored_asset_not_found"}


@pytest.mark.parametrize("status", ["unavailable", "permission", "incompatible"])
def test_authored_api_requires_current_successful_refresh(registry, status):
    repo, spec, originals = setup_source(registry)
    with repo.transaction() as conn:
        conn.execute("UPDATE media_sources SET status=%s WHERE source_ref=%s", (status, spec.source_ref))
    with client_for(registry) as client:
        response = client.post("/v1/operator/authored-candidates",
                               headers={"Authorization": "Bearer " + ADMIN},
                               json={"source_ref": spec.source_ref,
                                     "asset_ids": [originals[0].asset_id]})
        assert response.status_code == 409
        assert response.json() == {"error": "source_not_fresh"}
    with repo.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM authored_candidates").fetchone()["n"] == 0


def test_authored_limit_and_immutable_snapshot(registry):
    limits = StoreLimits(max_authored_candidates=2)
    repo, spec, originals = setup_source(registry, limits=limits)
    with client_for(registry) as client:
        client.app.state.coordinator.media.limits = limits
        headers = {"Authorization": "Bearer " + ADMIN}
        assert client.post("/v1/operator/authored-candidates", headers=headers,
                           json={"source_ref": spec.source_ref, "asset_ids": [originals[0].asset_id]}).status_code == 200
        second = asset(2)
        with repo.transaction() as conn:
            conn.execute("INSERT INTO asset_revisions VALUES(%s,%s,1000,1000)",
                         (second.asset_id, Jsonb(second.model_dump(mode="json"))))
            conn.execute("INSERT INTO source_members VALUES(%s,%s)", (spec.source_ref, second.asset_id))
            repo.refresh_catalog_in(conn, spec.source_ref, 1000, "ok")
        assert client.post("/v1/operator/authored-candidates", headers=headers,
                           json={"source_ref": spec.source_ref, "asset_ids": [second.asset_id]}).json() == {
            "error": "authored_candidate_limit"}
        changed = originals[0].candidate.model_copy(update={"captured_at": 1})
        with repo.transaction() as conn:
            conn.execute("UPDATE authored_candidates SET candidate=%s WHERE asset_id=%s",
                         (Jsonb(changed.model_dump(mode="json")), originals[0].asset_id))
        assert client.post("/v1/operator/authored-candidates", headers=headers,
                           json={"source_ref": spec.source_ref, "asset_ids": [originals[0].asset_id]}).json() == {
            "error": "authored_candidate_immutable"}


def test_worker_publication_eviction_and_failure_cooldown_hydrate_authored_refs(registry, tmp_path):
    from central.media_store import MediaStore

    original_bytes, variant_bytes = b"0123456789", b"prepared"
    worker_asset = asset(1, original_bytes=original_bytes)
    repo, spec, originals = setup_source(registry, assets=(worker_asset,))
    repo.author_authored_candidates(spec.source_ref, (originals[0].asset_id,))
    store = MediaStore(repo, tmp_path / "media")
    repo.request_acquisitions((AcquisitionRequest(asset_id=originals[0].asset_id,
                             assignment_ids=("a",), earliest_start=1000),))
    with store.worker_lock():
        lease = repo.claim_job()
        staged = store.staging(lease)
        staged.original.write_bytes(original_bytes)
        staged.variant.write_bytes(variant_bytes)
        variant = Variant(sha256=hashlib.sha256(variant_bytes).hexdigest(), size=len(variant_bytes),
                          media_type="image/jpeg", width=30, height=50)
        build = BuildIdentity(preparation_sha256="a" * 64, ffmpeg_sha256="b" * 64,
            ffprobe_sha256="c" * 64, ffmpeg_version="fixture", ffprobe_version="fixture",
            python_version="fixture", pillow_version="fixture", littlecms_version="fixture",
            jpeg_version="fixture", zlib_version="fixture", platform="fixture",
            memory_limit_enforced=False)
        prepared = PreparedMedia(path=staged.variant, variant=variant,
            original_sha256=hashlib.sha256(original_bytes).hexdigest(), recipe_id=RECIPE, build=build)
        store.publish(lease, prepared)
    with repo.db.transaction() as conn:
        assert repo.catalog_in(conn, registry.clock.utc())[1][originals[0].asset_id].variant == variant
    with store.worker_lock():
        store.collect(target_bytes=0)
    with repo.db.transaction() as conn:
        hydrated = repo.catalog_in(conn, registry.clock.utc())[1][originals[0].asset_id]
        assert hydrated.variant is None
        conn.execute("UPDATE media_jobs SET state='retry',retry_at=1010,failure_code='asset_missing',"
                     "variant_sha=NULL WHERE asset_id=%s", (originals[0].asset_id,))
        assert repo.catalog_in(conn, 1000)[1][originals[0].asset_id].preparation_failure == "asset_missing"
        assert repo.catalog_in(conn, 1011)[1][originals[0].asset_id].preparation_failure is None


def test_scene_asset_refs_are_per_frame_and_admitted_runs_are_immutable():
    runtime = Runtime()
    first = Scene(scene_id="scene", revision=1, contributions=(Contribution(
        target="frame:left", asset_refs=("asset-a",)),))
    second = first.model_copy(update={"revision": 2, "contributions": (Contribution(
        target="frame:left", asset_refs=("asset-b",)),)})
    runtime.set_scene(first)
    admission = runtime.activate("scene", "activation:a", 1000)
    assert runtime.project(1000).visible[0].asset_refs == ("asset-a",)
    runtime.set_scene(second)
    assert runtime.project(1000).visible[0].asset_refs == ("asset-a",)
    runtime.cancel(admission.run_id, 1000)
    runtime.activate("scene", "activation:b", 1000)
    assert runtime.project(1000).visible[0].asset_refs == ("asset-b",)
