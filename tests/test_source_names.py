"""Logical Source names retain immutable revisions and admitted execution.

A revision is saved from a draft (`SourceNameService.save_draft`), as the console saves one; the
pools are the photo library store's, fed by nothing here (no worker, no Immich).
"""

import pytest
from fastapi.testclient import TestClient
from support.photo_pools import fixture_connection, library_of

from central.app import create_app
from central.db import ProcessTransactionClock
from central.media_repository import MediaRepository
from central.photo_library.model import SourceQuery
from central.runtime import Child, Contribution, Program, RuntimeConflict, Scene
from central.runtime_store import RuntimeStore
from central.source_names import SourceInUse, SourceNameService


class Names:
    """The service, its repository and Runtime, and a `save` as the editor's Save."""

    def __init__(self, registry):
        self.registry = registry
        self.repository = MediaRepository(registry.db, registry.clock,
                                          times=ProcessTransactionClock(registry.clock))
        self.runtime = RuntimeStore(registry.db, registry.clock)
        self.library = library_of(registry)
        self.service = SourceNameService(registry.db, self.repository, self.runtime,
                                         self.library)
        self.connection = fixture_connection(registry)

    def save(self, name: str, *, editing: str | None = None, favourites: bool = False) -> dict:
        query = SourceQuery(connection_ref=self.connection, favourites_only=favourites)
        return self.service.save_draft(self.library.create_draft(query, editing), name)

    def reconcile(self) -> None:
        with self.registry.db.transaction() as conn, self.runtime.edit(conn) as current:
            self.repository.reconcile_source_activity_in(conn, current.planning_source_refs())

    def active(self, source_ref: str) -> bool:
        with self.registry.db.transaction() as conn:
            return conn.execute("SELECT refresh_active FROM media_sources WHERE source_ref=%s",
                                (source_ref,)).fetchone()["refresh_active"]


@pytest.fixture
def names(registry):
    return Names(registry)


def test_named_edit_revises_future_scenes_but_keeps_admitted_run(names):
    runtime = names.runtime
    assert names.save("all-photos")["source_ref"] == "all-photos:1"
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

    edited = names.save("all-photos", editing="all-photos:1", favourites=True)
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
    assert [s["name"] for s in names.repository.sources()] == ["all-photos"]
    assert names.repository.sources()[0]["source_ref"] == "all-photos:2"


def test_delete_guards_scenes_and_recreate_uses_next_hidden_revision(names):
    names.save("used")
    names.runtime.command("set_scene", Scene(scene_id="dependent", contributions=(
        Contribution(target="frame:main", source_refs=("used:1",)),)))
    with pytest.raises(SourceInUse) as error:
        names.service.delete("used", 1)
    assert error.value.scene_ids == ("dependent",)
    names.save("unused")
    assert names.service.delete("unused", 1)["deleted"] is True
    assert "unused" not in {row["name"] for row in names.repository.sources()}
    assert names.save("unused", favourites=True)["source_ref"] == "unused:2"


def test_rename_changes_future_scene_and_keeps_old_run(names):
    runtime = names.runtime
    names.save("old")
    runtime.command("set_scene", Scene(scene_id="show", loop=True, contributions=(
        Contribution(target="frame:main", source_refs=("old:1",)),)))
    run = runtime.command("activate", "show", "first", 1000)
    assert names.save("new", editing="old:1")["source_ref"] == "new:1"
    assert [row["name"] for row in names.repository.sources()] == ["new"]
    saved = runtime.read().export_state()
    assert saved["scenes"]["show"]["contributions"][0]["source_refs"] == ["new:1"]
    assert saved["runs"][run.run_id]["scene"]["contributions"][0]["source_refs"] == ["old:1"]


def test_planner_pools_exclude_dormant_revisions(names):
    names.save("photos")
    names.runtime.command("set_scene", Scene(scene_id="show", contributions=(
        Contribution(target="frame:main", source_refs=("photos:1",)),)))
    names.save("photos", editing="photos:1", favourites=True)
    needed = names.runtime.read().planning_source_refs()
    with names.registry.db.transaction() as conn:
        pools = names.repository.source_pools_in(conn, needed)
    assert set(pools) == {"photos:2"}


def test_a_dormant_revision_is_unwanted_until_a_scene_needs_it_again(names):
    """`refresh_active` is the wanted rule's Source half (the store's due_pools and
    retire_unwanted read it): a revision an admitted Run holds stays wanted; once nothing
    needs it, it is not; a Scene naming it again makes it wanted."""
    runtime = names.runtime
    names.save("photos")
    runtime.command("set_scene", Scene(scene_id="show", loop=True, contributions=(
        Contribution(target="frame:main", source_refs=("photos:1",)),)))
    first = runtime.command("activate", "show", "first", 1000)
    names.save("photos", editing="photos:1", favourites=True)
    assert names.active("photos:1") is True
    runtime.command("cancel", first.run_id, 1001)
    names.reconcile()
    assert (names.active("photos:1"), names.active("photos:2")) == (False, True)
    runtime.command("set_scene", Scene(scene_id="old-again", contributions=(
        Contribution(target="frame:other", source_refs=("photos:1",)),)))
    names.reconcile()
    assert names.active("photos:1") is True


def test_the_delete_api_reports_dependent_scene_ids(names, registry):
    names.save("family")
    names.runtime.command("set_scene", Scene(scene_id="old", contributions=(
        Contribution(target="frame:main", source_refs=("family:1",)),)))
    app = create_app(registry.db, registry.clock, "a" * 32)
    headers = {"Authorization": "Bearer " + "a" * 32}
    with TestClient(app) as client:
        media = client.get("/v1/operator/media", headers=headers).json()
        assert [(row["name"], row["source_ref"]) for row in media["sources"]] == [
            ("family", "family:1")]
        refused = client.delete("/v1/operator/source-names/family?expected_revision=1",
                                headers=headers)
        assert refused.status_code == 409
        assert refused.json() == {"error": "source_in_use", "scene_ids": ["old"]}
        retired = client.put("/v1/operator/source-names/family", headers=headers, json={})
        assert retired.status_code in (404, 405)   # a rename is a draft saved under a new name


def test_migration_preserves_legacy_collisions_and_large_numeric_suffix(registry):
    refs = ("foo", "foo:1", "foo:bar", "large:" + "9" * 22)
    with registry.db.transaction() as conn:
        conn.execute("DROP TABLE media_source_name_versions")
        # 080's drafts name a Source name: their reference goes with the table (030 re-runs).
        conn.execute("DROP TABLE media_source_names CASCADE")
        conn.execute("DELETE FROM schema_migrations WHERE name='030_source_names.sql'")
        for ref in refs:
            conn.execute("INSERT INTO media_sources(source_ref) VALUES(%s)", (ref,))
    registry.db.migrate()
    with registry.db.transaction() as conn:
        versions = conn.execute("SELECT source_ref,name,revision FROM media_source_name_versions "
                                "ORDER BY source_ref").fetchall()
        assert len(versions) == len(refs)
        assert {row["name"] for row in versions} == {"foo", "foo:bar", refs[-1]}
        assert len({(row["name"], row["revision"]) for row in versions}) == len(refs)
