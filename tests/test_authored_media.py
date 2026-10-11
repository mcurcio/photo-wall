"""Bounded authored-media references and their live worker hydration."""

import hashlib
import uuid
from typing import NamedTuple

from fastapi.testclient import TestClient
from media_queue import RecordingMediaQueue
from psycopg.types.json import Jsonb
from support.photo_pools import FIXTURE_CONNECTION, seed_pool

from central.app import create_app
from central.db import ProcessTransactionClock
from central.media_repository import MediaRepository
from central.photo_library.model import LibraryAsset
from central.planner import AcquisitionRequest
from central.runtime import Contribution, Runtime, Scene
from contracts.models import Variant
from media.models import SourceSpec
from media.prepare import BuildIdentity, PreparedMedia

ADMIN = "test-operator-" + "x" * 40
RECIPE = "a" * 64


class SavedSource(NamedTuple):
    source_ref: str


def asset(number=1, *, width=30, height=50, original_bytes=None, kind="image"):
    """A pool photo; with `original_bytes`, the unedited photo whose checksum is theirs."""
    revision = (hashlib.sha1(original_bytes).hexdigest() if original_bytes is not None
                else f"{number:040x}")
    return LibraryAsset(connection_ref=FIXTURE_CONNECTION, upstream_id=str(uuid.UUID(int=number)),
                        revision=revision, kind=kind, taken_at=900 + number, width=width,
                        height=height, duration=10.0 if kind == "video" else None)


def setup_source(registry, count=1, *, limits=None, assets=None, complete=True):
    """Saved Source `source:1` whose pool holds `assets` (or `count` photos), seeded through the
    photo library store's worker side."""
    repo = MediaRepository(registry.db, registry.clock, limits, queue=RecordingMediaQueue(), times=ProcessTransactionClock(registry.clock))
    originals = tuple(assets) if assets is not None else tuple(asset(i + 1) for i in range(count))
    seed_pool(registry, "source:1", originals, complete=complete)
    repo.set_recipe(RECIPE)
    return repo, SavedSource("source:1"), originals


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
    seed_pool(registry, "other:1", (asset(2),))   # a known photo, in another Source's pool
    with client_for(registry) as client:
        headers = {"Authorization": "Bearer " + ADMIN}
        body = {"source_ref": spec.source_ref, "asset_ids": [asset(2).asset_id]}
        assert client.post("/v1/operator/authored-candidates", headers=headers, json=body).json() == {
            "error": "authored_asset_not_member"}
        body["asset_ids"] = ["asset-never-seen"]
        assert client.post("/v1/operator/authored-candidates", headers=headers, json=body).json() == {
            "error": "authored_asset_not_found"}


def test_operator_can_request_exact_asynchronous_source_refresh(registry):
    spec = SourceSpec(source_ref="source:1", connection_ref="fixture", favorites=True)
    MediaRepository(registry.db, registry.clock, queue=RecordingMediaQueue(),
                    times=ProcessTransactionClock(registry.clock)).configure_source(spec)
    path = f"/v1/operator/sources/{spec.source_ref}/refresh"
    with client_for(registry) as client:
        assert client.post(path).status_code == 401
        headers = {"Authorization": "Bearer " + ADMIN}
        first = client.post(path, headers=headers)
        second = client.post(path, headers=headers)
        missing = client.post("/v1/operator/sources/missing:1/refresh", headers=headers)

    assert first.status_code == second.status_code == 202
    assert first.json() == {
        "source_ref": spec.source_ref,
        "requested_revision": 1,
        "completed_revision": 0,
        "coalesced": False,
    }
    assert second.json() == {
        "source_ref": spec.source_ref,
        "requested_revision": 2,
        "completed_revision": 0,
        "coalesced": True,
    }
    assert missing.status_code == 404 and missing.json() == {"error": "source_not_found"}


def test_a_pool_member_authors_while_its_check_has_not_completed(registry):
    """C7: pool rows are photos a check confirmed, so a pool whose latest check stopped part-way
    (Immich down, the key rejected) still authors from what it holds."""
    repo, spec, originals = setup_source(registry, complete=False)
    with client_for(registry) as client:
        response = client.post("/v1/operator/authored-candidates",
                               headers={"Authorization": "Bearer " + ADMIN},
                               json={"source_ref": spec.source_ref,
                                     "asset_ids": [originals[0].asset_id]})
        assert response.status_code == 200 and response.json()["created"] == 1


def test_authored_snapshot_is_immutable_with_no_count_limit(registry):
    repo, spec, originals = setup_source(registry, count=3)
    with client_for(registry) as client:
        headers = {"Authorization": "Bearer " + ADMIN}
        assert client.post("/v1/operator/authored-candidates", headers=headers, json={
            "source_ref": spec.source_ref,
            "asset_ids": [original.asset_id for original in originals]}).json()["created"] == 3
        with repo.transaction() as conn:
            changed = repo.authored_candidates_in(conn, (originals[0].asset_id,))[
                originals[0].asset_id].model_copy(update={"captured_at": 1})
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
        assert repo.authored_in(conn)[originals[0].asset_id].variant == variant
    with store.worker_lock():
        store.collect(target_bytes=0)
    with repo.db.transaction() as conn:
        hydrated = repo.authored_in(conn)[originals[0].asset_id]
        assert hydrated.variant is None
        conn.execute("UPDATE media_jobs SET state='retry',retry_at=1010,failure_code='asset_missing',"
                     "variant_sha=NULL WHERE asset_id=%s", (originals[0].asset_id,))
        registry.clock.wall = 1000  # the repository's media times follow this clock
        assert repo.authored_in(conn)[originals[0].asset_id].preparation_failure == "asset_missing"
        registry.clock.advance(11)
        assert repo.authored_in(conn)[originals[0].asset_id].preparation_failure is None


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
