"""Logical Source names retain immutable revisions and admitted execution."""

import uuid

import pytest
from fastapi.testclient import TestClient
from media_queue import RecordingMediaQueue
from psycopg.types.json import Jsonb

from central.app import create_app
from central.catalog import CatalogSnapshot
from central.media_repository import MediaRepository
from central.registry import RegistryError
from central.runtime import Child, Contribution, Program, RuntimeConflict, Scene
from central.runtime_store import RuntimeStore
from central.source_names import NamedSourceWrite, SourceInUse, SourceNameService
from media.models import OriginalAsset, RefreshResult, SourceSpec


def write(expected, *, favorites=None):
    return NamedSourceWrite(expected_revision=expected, connection_ref="immich-main",
                            favorites=favorites)


def test_named_edit_revises_future_scenes_but_keeps_admitted_run(registry):
    repository = MediaRepository(registry.db, registry.clock)
    runtime = RuntimeStore(registry.db, registry.clock)
    names = SourceNameService(registry.db, repository, runtime)
    assert names.put("all-photos", write(None))["source_ref"] == "all-photos:1"
    child = Scene(scene_id="child", contributions=(Contribution(target="frame:child",
                              source_refs=("all-photos:1",)),))
    scene = Scene(scene_id="show", loop=True, children=(Child(scene=child),),
                  contributions=(Contribution(target="frame:main",
                                              source_refs=("all-photos:1",)),))
    runtime.command("set_scene", scene)
    runtime.command("set_program", Program(program_id="next", scene_id="show",
                                           starts_at=1010, ends_at=1200))
    admission = runtime.command("activate", "show", "manual", 1000)
    assert admission.status == "admitted"
    queued = runtime.command("activate", "show", "later", 1000,
                             repeat="queue", expires_at=1100)
    assert queued.status == "queued"

    edited = names.put("all-photos", write(1, favorites=True))
    assert edited == {"name": "all-photos", "revision": 2,
                      "source_ref": "all-photos:2", "created": True}
    saved = runtime.read().export_state()
    assert saved["scenes"]["show"]["revision"] == 2
    assert saved["scenes"]["show"]["contributions"][0]["source_refs"] == ["all-photos:2"]
    assert saved["scenes"]["show"]["children"][0]["scene"]["contributions"][0]["source_refs"] == ["all-photos:2"]
    assert saved["runs"][admission.run_id]["scene"]["contributions"][0]["source_refs"] == ["all-photos:1"]
    with pytest.raises(RuntimeConflict, match="scene_revision_conflict"):
        runtime.command("set_scene", scene.model_copy(update={"revision": 2, "loop": False}))
    runtime.command("cancel", admission.run_id, 1001)
    saved = runtime.read().export_state()
    queued_run = saved["admissions"]["later"]["run_id"]
    assert saved["runs"][queued_run]["scene"]["contributions"][0]["source_refs"] == ["all-photos:1"]
    runtime.command("advance", 1010)
    saved = runtime.read().export_state()
    program_run = saved["admissions"]["program:next:1010"]["run_id"]
    assert saved["runs"][program_run]["scene"]["contributions"][0]["source_refs"] == ["all-photos:2"]
    assert names.put("all-photos", write(1, favorites=True))["created"] is False
    with pytest.raises(RegistryError, match="source_revision_conflict"):
        names.put("all-photos", write(1, favorites=False))
    assert [s["name"] for s in repository.sources()] == ["all-photos"]
    assert repository.sources()[0]["source_ref"] == "all-photos:2"


def test_delete_guards_scenes_and_recreate_uses_next_hidden_revision(registry):
    repository = MediaRepository(registry.db, registry.clock)
    runtime = RuntimeStore(registry.db, registry.clock)
    names = SourceNameService(registry.db, repository, runtime)
    names.put("used", write(None))
    runtime.command("set_scene", Scene(scene_id="dependent", contributions=(
        Contribution(target="frame:main", source_refs=("used:1",)),)))
    with pytest.raises(SourceInUse) as error:
        names.delete("used", 1)
    assert error.value.scene_ids == ("dependent",)
    names.put("unused", write(None))
    assert names.delete("unused", 1)["deleted"] is True
    assert "unused" not in {row["name"] for row in repository.sources()}
    recreated = names.put("unused", write(None, favorites=True))
    assert recreated["source_ref"] == "unused:2"


def test_rename_changes_future_scene_and_keeps_old_run(registry):
    repository = MediaRepository(registry.db, registry.clock)
    runtime = RuntimeStore(registry.db, registry.clock)
    names = SourceNameService(registry.db, repository, runtime)
    names.put("old", write(None))
    runtime.command("set_scene", Scene(scene_id="show", loop=True, contributions=(
        Contribution(target="frame:main", source_refs=("old:1",)),)))
    run = runtime.command("activate", "show", "first", 1000)
    renamed = names.put("old", NamedSourceWrite(expected_revision=1,
                        connection_ref="immich-main", new_name="new"))
    assert renamed["source_ref"] == "new:1"
    assert names.put("old", NamedSourceWrite(expected_revision=1,
                     connection_ref="immich-main", new_name="new"))["created"] is False
    assert [row["name"] for row in repository.sources()] == ["new"]
    saved = runtime.read().export_state()
    assert saved["scenes"]["show"]["contributions"][0]["source_refs"] == ["new:1"]
    assert saved["runs"][run.run_id]["scene"]["contributions"][0]["source_refs"] == ["old:1"]


def test_planner_catalog_excludes_dormant_revisions(registry):
    repository = MediaRepository(registry.db, registry.clock)
    runtime = RuntimeStore(registry.db, registry.clock)
    names = SourceNameService(registry.db, repository, runtime)
    names.put("photos", write(None))
    runtime.command("set_scene", Scene(scene_id="show", contributions=(
        Contribution(target="frame:main", source_refs=("photos:1",)),)))
    names.put("photos", write(1, favorites=True))
    needed = runtime.read().planning_source_refs()
    with registry.db.transaction() as conn:
        snapshots, _ = repository.catalog_in(conn, registry.clock.utc(), needed)
    assert set(snapshots) == {"photos:2"}


def test_dormant_revision_stops_scheduled_refresh_and_reactivates_for_run(registry):
    repository = MediaRepository(registry.db, registry.clock)
    runtime = RuntimeStore(registry.db, registry.clock)
    names = SourceNameService(registry.db, repository, runtime)
    names.put("photos", write(None))
    runtime.command("set_scene", Scene(scene_id="show", loop=True, contributions=(
        Contribution(target="frame:main", source_refs=("photos:1",)),)))
    first = runtime.command("activate", "show", "first", 1000)
    names.put("photos", write(1, favorites=True))
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT refresh_active FROM media_sources WHERE source_ref='photos:1'").fetchone()[
            "refresh_active"] is True
    runtime.command("cancel", first.run_id, 1001)
    with registry.db.transaction() as conn, runtime.edit(conn) as current:
        repository.reconcile_source_activity_in(conn, current.planning_source_refs())
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT refresh_active FROM media_sources WHERE source_ref='photos:1'").fetchone()[
            "refresh_active"] is False
    assert repository.begin_scheduled_refresh().source.source_ref == "photos:2"
    # A newly authored legacy Scene can make the old immutable revision live
    # again; it first becomes unavailable and immediately due for refresh.
    runtime.command("set_scene", Scene(scene_id="old-again", contributions=(
        Contribution(target="frame:other", source_refs=("photos:1",)),)))
    with registry.db.transaction() as conn, runtime.edit(conn) as current:
        repository.reconcile_source_activity_in(conn, current.planning_source_refs())
    with registry.db.transaction() as conn:
        old = conn.execute("SELECT refresh_active,status,next_refresh FROM media_sources "
                           "WHERE source_ref='photos:1'").fetchone()
    assert old == {"refresh_active": True, "status": "unavailable", "next_refresh": 0}


def test_retirement_waits_for_lease_then_fences_expired_publication(registry):
    repository = MediaRepository(registry.db, registry.clock)
    names = SourceNameService(registry.db, repository, RuntimeStore(registry.db, registry.clock))
    names.put("photos", write(None))
    lease = repository.begin_scheduled_refresh()
    assert lease.source.source_ref == "photos:1"
    names.put("photos", write(1, favorites=True))
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT refresh_active FROM media_sources WHERE source_ref='photos:1'").fetchone()[
            "refresh_active"] is True
    registry.clock.advance(91)
    runtime = RuntimeStore(registry.db, registry.clock)
    with registry.db.transaction() as conn, runtime.edit(conn) as current:
        repository.reconcile_source_activity_in(conn, current.planning_source_refs())
    result = RefreshResult(snapshot=CatalogSnapshot(source_ref="photos:1",
                                                    refreshed_at=registry.clock.utc(), status="ok"))
    assert repository.publish_refresh(lease, result) is False
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT refresh_active FROM media_sources WHERE source_ref='photos:1'").fetchone()[
            "refresh_active"] is False


def test_retirement_waits_for_requested_refresh_completion(registry):
    repository = MediaRepository(registry.db, registry.clock)
    runtime = RuntimeStore(registry.db, registry.clock)
    names = SourceNameService(registry.db, repository, runtime)
    names.put("photos", write(None))
    repository.queue = RecordingMediaQueue()
    receipt = repository.request_refresh("photos:1")
    assert receipt.requested_revision == 1
    names.put("photos", write(1, favorites=True))
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT refresh_active FROM media_sources WHERE source_ref='photos:1'").fetchone()[
            "refresh_active"] is True
    lease = repository.begin_requested_refresh("photos:1")
    assert lease is not None and lease.request_revision == 1
    assert repository.publish_refresh(lease, RefreshResult(snapshot=CatalogSnapshot(
        source_ref="photos:1", refreshed_at=registry.clock.utc(), status="ok")))
    with registry.db.transaction() as conn, runtime.edit(conn) as current:
        repository.reconcile_source_activity_in(conn, current.planning_source_refs())
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT refresh_active FROM media_sources WHERE source_ref='photos:1'").fetchone()[
            "refresh_active"] is False


def test_dormant_snapshot_releases_metadata_capacity_after_run_ends(registry):
    repository = MediaRepository(registry.db, registry.clock)
    runtime = RuntimeStore(registry.db, registry.clock)
    names = SourceNameService(registry.db, repository, runtime)
    names.put("photos", write(None))
    asset = OriginalAsset(connection_id="immich-main", upstream_id=str(uuid.UUID(int=1)),
                          original_sha1="0" * 39 + "1", kind="image", raw_width=30,
                          raw_height=50, orientation=1, captured_at=900, file_size=10)
    snapshot = CatalogSnapshot(source_ref="photos:1", refreshed_at=1000,
                               status="ok", candidates=(asset.candidate,))
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO asset_revisions(asset_id,metadata,first_seen,last_seen) "
                     "VALUES(%s,%s,1000,1000)",
                     (asset.asset_id, Jsonb(asset.model_dump(mode="json"))))
        conn.execute("INSERT INTO source_members(source_ref,asset_id) VALUES('photos:1',%s)",
                     (asset.asset_id,))
        conn.execute("UPDATE catalog_snapshots SET snapshot=%s WHERE source_ref='photos:1'",
                     (Jsonb(snapshot.model_dump(mode="json")),))
        assert repository._candidate_capacity(conn) == 1
    runtime.command("set_scene", Scene(scene_id="show", loop=True, contributions=(
        Contribution(target="frame:main", source_refs=("photos:1",)),)))
    run = runtime.command("activate", "show", "first", 1000)
    names.put("photos", write(1, favorites=True))
    with registry.db.transaction() as conn:
        assert repository._candidate_capacity(conn) == 1
    runtime.command("cancel", run.run_id, 1001)
    with registry.db.transaction() as conn, runtime.edit(conn) as current:
        repository.reconcile_source_activity_in(conn, current.planning_source_refs())
    with registry.db.transaction() as conn:
        assert repository._candidate_capacity(conn) == 0
        assert conn.execute("SELECT count(*) AS n FROM source_members WHERE source_ref='photos:1'").fetchone()["n"] == 0


def test_legacy_versions_group_and_api_reports_dependent_scene_ids(registry):
    repository = MediaRepository(registry.db, registry.clock)
    repository.configure_source(SourceSpec(source_ref="legacy:1", connection_ref="immich-main"))
    repository.configure_source(SourceSpec(source_ref="legacy:2", connection_ref="immich-main"))
    runtime = RuntimeStore(registry.db, registry.clock)
    runtime.command("set_scene", Scene(scene_id="old", contributions=(
        Contribution(target="frame:main", source_refs=("legacy:1",)),)))
    app = create_app(registry.db, registry.clock, "a" * 32)
    headers = {"Authorization": "Bearer " + "a" * 32}
    with TestClient(app) as client:
        response = client.put("/v1/operator/source-names/legacy", headers=headers,
                              json={"expected_revision": 2, "connection_ref": "immich-main",
                                    "favorites": True})
        assert response.status_code == 200 and response.json()["revision"] == 3
        media = client.get("/v1/operator/media", headers=headers).json()
        assert [(row["name"], row["source_ref"]) for row in media["sources"]] == [
            ("legacy", "legacy:3")]
        refused = client.delete("/v1/operator/source-names/legacy?expected_revision=3",
                                headers=headers)
        assert refused.status_code == 409
        assert refused.json() == {"error": "source_in_use", "scene_ids": ["old"]}
    assert runtime.read().export_state()["scenes"]["old"]["contributions"][0]["source_refs"] == ["legacy:3"]


def test_migration_preserves_legacy_collisions_and_large_numeric_suffix(registry):
    refs = ("foo", "foo:1", "foo:bar", "large:" + "9" * 22)
    with registry.db.transaction() as conn:
        conn.execute("DROP TABLE media_source_name_versions")
        conn.execute("DROP TABLE media_source_names")
        conn.execute("DELETE FROM schema_migrations WHERE name='030_source_names.sql'")
        for ref in refs:
            spec = SourceSpec(source_ref=ref, connection_ref="immich-main")
            conn.execute("INSERT INTO media_sources(source_ref,spec) VALUES(%s,%s)",
                         (ref, Jsonb(spec.model_dump(mode="json", by_alias=True))))
    registry.db.migrate()
    with registry.db.transaction() as conn:
        versions = conn.execute("SELECT source_ref,name,revision FROM media_source_name_versions "
                                "ORDER BY source_ref").fetchall()
        assert len(versions) == len(refs)
        assert {row["name"] for row in versions} == {"foo", "foo:bar", refs[-1]}
        assert len({(row["name"], row["revision"]) for row in versions}) == len(refs)
