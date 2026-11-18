"""Logical Source names retain immutable revisions and admitted execution.

A revision is saved from a draft (`SourceNameService.save_draft`), as the console saves one; the
pools are the photo library store's, fed by nothing here (no worker, no Immich).
"""

import psycopg
import pytest
from fastapi.testclient import TestClient
from support.house_time import UTC_HOUSE, set_house, window_program
from support.photo_pools import fixture_connection, library_of

from central.app import create_app
from central.db import ProcessTransactionClock
from central.media_repository import MediaRepository
from central.photo_library.model import SourceQuery
from central.runtime import Child, Contribution, Scene, SceneWriteRefused
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
        return self.service.save_draft(self.library.create_draft(query, editing, by="console"), name, by="console")

    def reconcile(self) -> None:
        with self.registry.db.transaction() as conn, self.runtime.edit(conn, by="console", command="reconcile") as current:
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
    runtime.command("set_scene", scene, by="console")
    set_house(runtime.db, UTC_HOUSE)
    runtime.command("create_program", window_program("next", "show", 1020, 1200), 1000, by="console")
    admission = runtime.command("activate", "show", "manual", 1000, by="console")
    assert admission.status == "admitted"
    queued = runtime.command("activate", "show", "later", 1000,
                             repeat="queue", expires_at=1100, by="console")
    assert queued.status == "queued"

    edited = names.save("all-photos", editing="all-photos:1", favourites=True)
    assert edited == {"name": "all-photos", "revision": 2,
                      "source_ref": "all-photos:2", "created": True}
    saved = runtime.read().export_state()
    assert saved["scenes"]["show"]["revision"] == 2
    assert saved["scenes"]["show"]["contributions"][0]["source_refs"] == ["all-photos:2"]
    assert saved["scenes"]["show"]["children"][0]["scene"]["contributions"][0]["source_refs"] == ["all-photos:2"]
    assert saved["runs"][admission.run_id]["scene"]["contributions"][0]["source_refs"] == ["all-photos:1"]
    with pytest.raises(SceneWriteRefused, match="changed"):
        runtime.command("set_scene", scene.model_copy(update={"revision": 2, "loop": False}), by="console")
    runtime.command("cancel", admission.run_id, 1001, by="console")
    saved = runtime.read().export_state()
    queued_run = saved["admissions"]["later"]["run_id"]
    assert saved["runs"][queued_run]["scene"]["contributions"][0]["source_refs"] == ["all-photos:1"]
    runtime.command("advance", 1020, by="console")
    saved = runtime.read().export_state()
    program_run = saved["admissions"]["program:next:1:1020"]["run_id"]
    assert saved["runs"][program_run]["scene"]["contributions"][0]["source_refs"] == ["all-photos:2"]
    assert [s["name"] for s in names.repository.sources()] == ["all-photos"]
    assert names.repository.sources()[0]["source_ref"] == "all-photos:2"


def test_delete_guards_scenes_and_recreate_uses_next_hidden_revision(names):
    names.save("used")
    names.runtime.command("set_scene", Scene(scene_id="dependent", contributions=(
        Contribution(target="frame:main", source_refs=("used:1",)),)), by="console")
    with pytest.raises(SourceInUse) as error:
        names.service.delete("used", 1, by="console")
    assert error.value.scene_ids == ("dependent",)
    names.save("unused")
    assert names.service.delete("unused", 1, by="console")["deleted"] is True
    assert "unused" not in {row["name"] for row in names.repository.sources()}
    assert names.save("unused", favourites=True)["source_ref"] == "unused:2"


def test_rename_changes_future_scene_and_keeps_old_run(names):
    runtime = names.runtime
    names.save("old")
    runtime.command("set_scene", Scene(scene_id="show", loop=True, contributions=(
        Contribution(target="frame:main", source_refs=("old:1",)),)), by="console")
    run = runtime.command("activate", "show", "first", 1000, by="console")
    assert names.save("new", editing="old:1")["source_ref"] == "new:1"
    assert [row["name"] for row in names.repository.sources()] == ["new"]
    saved = runtime.read().export_state()
    assert saved["scenes"]["show"]["contributions"][0]["source_refs"] == ["new:1"]
    assert saved["runs"][run.run_id]["scene"]["contributions"][0]["source_refs"] == ["old:1"]


def test_planner_pools_exclude_dormant_revisions(names):
    names.save("photos")
    names.runtime.command("set_scene", Scene(scene_id="show", contributions=(
        Contribution(target="frame:main", source_refs=("photos:1",)),)), by="console")
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
        Contribution(target="frame:main", source_refs=("photos:1",)),)), by="console")
    first = runtime.command("activate", "show", "first", 1000, by="console")
    names.save("photos", editing="photos:1", favourites=True)
    assert names.active("photos:1") is True
    runtime.command("cancel", first.run_id, 1001, by="console")
    names.reconcile()
    assert (names.active("photos:1"), names.active("photos:2")) == (False, True)
    runtime.command("set_scene", Scene(scene_id="old-again", contributions=(
        Contribution(target="frame:other", source_refs=("photos:1",)),)), by="console")
    names.reconcile()
    assert names.active("photos:1") is True


def test_the_delete_api_reports_dependent_scene_ids(names, registry):
    names.save("family")
    names.runtime.command("set_scene", Scene(scene_id="old", contributions=(
        Contribution(target="frame:main", source_refs=("family:1",)),)), by="console")
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


def test_two_sources_never_hold_one_name(names, registry):
    """A name is the operator's handle on a Source (decision 0034): a second new Source saved
    under a name another holds is refused, a rename onto a held name too, and the database holds
    one head per name and one row per (name, revision) whatever the code does. Mutation probe:
    drop the "source_name_exists" check in `save_draft` -> the second Source takes the name."""
    from central.registry import RegistryError

    names.save("family")
    names.save("holiday")
    with pytest.raises(RegistryError) as taken:
        names.save("family", favourites=True)
    assert taken.value.code == "source_name_exists"
    with pytest.raises(RegistryError) as renamed:
        names.save("family", editing="holiday:1")
    assert renamed.value.code == "source_name_exists"
    assert [(row["name"], row["source_ref"]) for row in names.repository.sources()] == [
        ("family", "family:1"), ("holiday", "holiday:1")]
    with registry.db.transaction() as conn:
        heads = conn.execute("SELECT name,source_ref,revision,deleted,renamed_to,created_by "
                             "FROM media_sources WHERE head ORDER BY name").fetchall()
    assert [tuple(row.values()) for row in heads] == [
        ("family", "family:1", 1, False, None, "console"),
        ("holiday", "holiday:1", 1, False, None, "console")]
    for statement in (   # the schema refuses a second head or a repeated revision on its own
            "INSERT INTO media_sources(source_ref,name,revision,head,created_at,created_by,"
            "changed_at,changed_by) VALUES('family:9','family',9,TRUE,0,'console',0,'console')",
            "INSERT INTO media_sources(source_ref,name,revision,created_at,created_by,"
            "changed_at,changed_by) VALUES('family:x','family',1,0,'console',0,'console')"):
        with pytest.raises(psycopg.errors.UniqueViolation), registry.db.transaction() as conn:
            conn.execute(statement)


def test_a_rename_leaves_the_old_name_deleted_and_free_for_a_new_source(names, registry):
    """A rename marks the old name's head deleted and renamed; the old name can then name a new
    Source, as its next revision (the hidden one), while the rename's new name keeps its own."""
    names.save("old")
    assert names.save("new", editing="old:1")["source_ref"] == "new:1"
    with registry.db.transaction() as conn:
        old = conn.execute("SELECT source_ref,deleted,renamed_to FROM media_sources "
                           "WHERE name='old' AND head").fetchone()
    assert tuple(old.values()) == ("old:1", True, "new")
    assert names.save("old", favourites=True)["source_ref"] == "old:2"
    assert [(row["name"], row["source_ref"]) for row in names.repository.sources()] == [
        ("new", "new:1"), ("old", "old:2")]
    assert names.service.delete("old", 2, by="console")["deleted"] is True
    assert [row["name"] for row in names.repository.sources()] == ["new"]


def test_a_source_save_that_rewrites_scenes_stamps_its_actor(names, registry):
    """A Photo source save moves every Scene using its name to the new revision and stamps
    runtime_state's `changed_by` with its actor, as any other Runtime write (`RuntimeStore.edit`
    stamps every write, no caller can skip it)."""
    runtime = names.runtime
    names.save("old")
    for scene_id in ("one", "two"):
        runtime.command("set_scene", Scene(scene_id=scene_id, loop=True, contributions=(
            Contribution(target="frame:main", source_refs=("old:1",)),)), by="console")
    names.save("old", editing="old:1", favourites=True)
    with registry.db.transaction() as conn:
        row = conn.execute("SELECT snapshot, changed_by FROM runtime_state").fetchone()
    assert row["changed_by"] == "console"
    assert {scene_id: scene["revision"] for scene_id, scene in row["snapshot"]["scenes"].items()} == {
        "one": 2, "two": 2}
