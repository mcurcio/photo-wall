"""The Photo source editor's routes (roadmap 4, slice C2; run ledger .claude/runs/photo-4.md, C2
cases 12-18): a draft is a Source not yet saved; Save makes the next revision of a name with the
draft's pool, and Scenes move to it.

FastAPI's TestClient over create_app with a real database; pools are fed through the store's
worker side (no worker, no Immich).
"""

from __future__ import annotations

from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from runtime_fakes import apply_procrastinate_schema
from test_photo_pools import asset, step

from central.app import create_app
from central.photo_library.model import InPool, SourceQuery
from central.photo_library.ports import CheckComplete
from central.runtime import Contribution, Scene
from central.runtime_store import RuntimeStore

ADMIN = "photo-sources-operator-" + "t" * 32
AUTH = {"Authorization": "Bearer " + ADMIN}
DRAFTS = "/v1/operator/photo-drafts"


@pytest.fixture
def client(registry, tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTO_WALL_CACHE_ROOT", str(tmp_path))
    apply_procrastinate_schema(registry.db.dsn)
    app = create_app(registry.db, registry.clock, ADMIN)
    with TestClient(app) as test_client:
        ref = test_client.post("/v1/operator/library/connections", headers=AUTH, json={
            "name": "Home", "url": "http://immich.invalid", "key": "PUBLIC-FIXTURE-KEY-1234"},
        ).json()["connection_ref"]
        yield test_client, app.state.photo_library, ref


def _draft(test_client, query, editing=None):
    response = test_client.post(DRAFTS, headers=AUTH, json={
        "query": query.model_dump(mode="json"), "editing": editing})
    assert response.status_code == 201, response.text
    return response.json()["draft_ref"]


def _save(test_client, draft, name):
    return test_client.post(f"{DRAFTS}/{draft}/save", headers=AUTH, json={"name": name})


def _source_row(library, source_ref):
    with library.db.transaction() as conn:
        return conn.execute("SELECT query,pool_key FROM media_sources WHERE source_ref=%s",
                            (source_ref,)).fetchone()


def test_a_draft_starts_checking_at_once(client):
    """Case 12."""
    test_client, library, ref = client
    draft = _draft(test_client, SourceQuery(connection_ref=ref))
    before = test_client.get(f"{DRAFTS}/{draft}/pool", headers=AUTH).json()
    assert before["check"] == {"state": "never_checked"} and before["items"] == []
    with library.db.transaction() as conn:
        assert [row["args"] for row in conn.execute(
            "SELECT args FROM procrastinate_jobs WHERE task_name='photo_wall.library.check_pool'"
        ).fetchall()] == [{"pool_key": SourceQuery(connection_ref=ref).pool_key()}]
    library.begin_check(SourceQuery(connection_ref=ref).pool_key())
    during = test_client.get(f"{DRAFTS}/{draft}/pool", headers=AUTH).json()
    assert during["check"]["state"] == "checking"
    assert test_client.get(f"{DRAFTS}/{str(UUID(int=1))}/pool", headers=AUTH).json() == {
        "error": "draft_unknown"}
    bad = test_client.get(f"{DRAFTS}/{draft}/pool", params={"after": "garbage"}, headers=AUTH)
    assert (bad.status_code, bad.json()) == (422, {"error": "cursor_invalid"})
    moved = test_client.put(f"{DRAFTS}/{draft}/query", headers=AUTH, json={
        "query": SourceQuery(connection_ref=ref, favourites_only=True).model_dump(mode="json")})
    assert moved.status_code == 204
    assert test_client.get(f"{DRAFTS}/{draft}/pool", headers=AUTH).json()["check"] == {
        "state": "never_checked"}


def test_saving_a_new_draft_makes_revision_one_with_its_pool(client):
    """Case 13."""
    test_client, library, ref = client
    query = SourceQuery(connection_ref=ref, favourites_only=True)
    saved = _save(test_client, _draft(test_client, query), "family")
    assert (saved.status_code, saved.json()) == (200, {"source_ref": "family:1"})
    row = _source_row(library, "family:1")
    assert (row["pool_key"], row["query"]) == (query.pool_key(), query.model_dump(mode="json"))
    with library.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM photo_drafts").fetchone()["n"] == 0


def test_saving_an_edit_makes_the_next_revision_and_moves_scenes(client, registry):
    """Case 14."""
    test_client, library, ref = client
    _save(test_client, _draft(test_client, SourceQuery(connection_ref=ref)), "family")
    runtime = RuntimeStore(registry.db, registry.clock)
    runtime.command("set_scene", Scene(scene_id="show", contributions=(
        Contribution(target="frame:main", source_refs=("family:1",)),)), by="console")
    edited = SourceQuery(connection_ref=ref, partners=True)
    saved = _save(test_client, _draft(test_client, edited, "family:1"), "family")
    assert saved.json() == {"source_ref": "family:2"}
    assert _source_row(library, "family:2")["pool_key"] == edited.pool_key()
    scene = runtime.read().export_state()["scenes"]["show"]
    assert scene["contributions"][0]["source_refs"] == ["family:2"]
    renamed = _save(test_client, _draft(test_client, edited, "family:2"), "relatives")
    assert renamed.json() == {"source_ref": "relatives:1"}
    assert runtime.read().export_state()["scenes"]["show"]["contributions"][0][
        "source_refs"] == ["relatives:1"]
    media = test_client.get("/v1/operator/media", headers=AUTH).json()
    assert [row["name"] for row in media["sources"]] == ["relatives"]


def test_save_refusals(client):
    """Case 15."""
    test_client, _, ref = client
    _save(test_client, _draft(test_client, SourceQuery(connection_ref=ref)), "family")
    taken = _save(test_client, _draft(test_client, SourceQuery(connection_ref=ref)), "family")
    assert (taken.status_code, taken.json()) == (409, {"error": "source_name_exists"})
    unknown = _save(test_client, str(UUID(int=1)), "other")
    assert (unknown.status_code, unknown.json()) == (404, {"error": "draft_unknown"})
    invalid = _save(test_client, _draft(test_client, SourceQuery(connection_ref=ref)), "-bad name")
    assert (invalid.status_code, invalid.json()) == (422, {"error": "invalid_source_name"})
    editing = test_client.post(DRAFTS, headers=AUTH, json={
        "query": SourceQuery(connection_ref=ref).model_dump(mode="json"), "editing": "nobody:1"})
    assert (editing.status_code, editing.json()) == (404, {"error": "source_unknown"})
    gone = test_client.post(DRAFTS, headers=AUTH, json={
        "query": SourceQuery(connection_ref="library-000000000000").model_dump(mode="json")})
    assert (gone.status_code, gone.json()) == (404, {"error": "connection_unknown"})


def test_a_saved_sources_pool_and_check_now(client):
    """Case 16, 17."""
    test_client, library, ref = client
    query = SourceQuery(connection_ref=ref)
    _save(test_client, _draft(test_client, query), "family")
    lease = library.begin_check(query.pool_key())
    library.record_step(lease, step(InPool(asset=asset(ref, 1, 100.0))))
    library.finish_check(lease, CheckComplete())
    pool = test_client.get("/v1/operator/sources/family:1/pool", headers=AUTH).json()
    assert pool["check"]["state"] == "complete" and pool["in_pool"] == {"photos": 1, "videos": 0}
    assert [item["asset_id"] for item in pool["items"]] == [asset(ref, 1, 100.0).asset_id]
    assert test_client.post("/v1/operator/sources/family:1/checks", headers=AUTH).status_code == 202
    for response in (test_client.get("/v1/operator/sources/nobody:1/pool", headers=AUTH),
                     test_client.post("/v1/operator/sources/nobody:1/checks", headers=AUTH)):
        assert (response.status_code, response.json()) == (404, {"error": "source_unknown"})
    [row] = test_client.get("/v1/operator/media", headers=AUTH).json()["sources"]
    assert row["query"] == query.model_dump(mode="json")
    assert row["check"]["state"] == "complete"


def test_a_pool_rows_thumbnail_is_servable_and_others_are_unknown(client):
    """Case 18."""
    test_client, library, ref = client
    query = SourceQuery(connection_ref=ref)
    _draft(test_client, query)
    lease = library.begin_check(query.pool_key())
    library.record_step(lease, step(InPool(asset=asset(ref, 1, 100.0))))
    path = "/v1/operator/library/thumbnails/"
    unknown = test_client.get(path + "asset-" + "0" * 64, headers=AUTH)
    assert (unknown.status_code, unknown.json()) == (404, {"error": "thumbnail_unknown"})
    servable = test_client.get(path + asset(ref, 1, 100.0).asset_id, headers=AUTH)
    assert servable.status_code != 404   # servable: fetched through the worker (none here)
