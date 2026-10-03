"""Each library connection's tag list (console DDD §38 shape A, §40, G10; PR 37 §7, §9).

The media worker re-lists a connection's tags on its refresh tick once the stored list is five
minutes old BY THE DATABASE CLOCK, replaces every list at boot, and keeps the last list (with
its age) when a re-list fails. The GET reads stored data only and serves ids, paths and names.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from media_queue import RecordingMediaQueue
from runtime_fakes import apply_procrastinate_schema
from test_immich import Upstream
from test_media_worker import SECRET, FakePreparer, configuration

from central.app import create_app
from central.db import DatabaseTransactionClock
from central.media_repository import TAG_FIELDS, MediaRepository
from central.media_store import MediaStore
from contracts.time import ManualClock
from media.models import ConnectionConfig, LibraryTag, MediaError
from media.worker import MediaWorker

ADMIN = "library-tags-operator-" + "t" * 32
AUTH = {"Authorization": "Bearer " + ADMIN}


def tag(number, path, parent=None):
    return LibraryTag(tag_ref=str(UUID(int=number)), path=path, name=path.rsplit("/", 1)[-1],
                      parent_ref=None if parent is None else str(UUID(int=parent)))


TAGS = (tag(1, "Family"), tag(2, "Family/Christmas", 1), tag(3, "Holidays/Family trip"),
        tag(4, "Work"))


# -- the client (unit) ---------------------------------------------------------------------------


def _listed(rows):
    upstream = Upstream()
    upstream.overrides["/api/tags"] = httpx.Response(200, json=rows)

    async def perform():
        async with upstream.client() as client:
            return await client.list_tags()
    return asyncio.run(perform())


def raw_tag(number, value, parent=None, name=None):
    return {"id": str(UUID(int=number)), "name": name or value.rsplit("/", 1)[-1], "value": value,
            "parentId": None if parent is None else str(UUID(int=parent)),
            "createdAt": "2026-01-01T00:00:00.000Z", "updatedAt": "2026-01-01T00:00:00.000Z"}


def test_listed_tags_are_stripped_of_control_and_bidi_characters_and_sorted():
    tags = _listed([raw_tag(2, "Zoo"), raw_tag(1, "Fam\u202eily\u2066", name="Fam\u202eily\x07"),
                    raw_tag(4, "A\u061cb\u200b", name="\ufeffb\u200d"),
                    raw_tag(3, "\u202e\u2069")])  # nothing visible: kept, with empty text
    assert [(t.path, t.name) for t in tags] == [
        ("", ""), ("Ab", "b"), ("Family", "Family"), ("Zoo", "Zoo")]
    assert tags[0].tag_ref == str(UUID(int=3))


def test_an_unnamed_tag_is_hidden_from_search_yet_never_called_gone(tags):
    """B5-FC1: the stored list keeps every listed id; only the search hides unnamed ones."""
    repository, worker, source = tags
    unnamed = LibraryTag(tag_ref=str(UUID(int=7)), path="\u202e", name="\u2069")
    source.tags = (*TAGS, unnamed)
    asyncio.run(worker.list_tags())
    listed = repository.library_tags("fixture")
    assert listed["total_matches"] == 4 and unnamed.tag_ref not in {
        t["tag_ref"] for t in listed["tags"]}
    by_id = repository.library_tags_by_id("fixture", (unnamed.tag_ref,))
    assert by_id["absent"] == [] and by_id["tags"] == [
        {"tag_ref": unnamed.tag_ref, "path": "", "name": "", "parent_ref": None}]


@pytest.mark.parametrize("rows", [
    [raw_tag(n, f"t{n}") for n in range(1, 5002)],
    [raw_tag(1, "x" * 1025)],
])
def test_over_the_caps_is_tag_limit_never_a_cut_list(rows):
    with pytest.raises(MediaError) as caught:
        _listed(rows)
    assert caught.value.code == "tag_limit"


# -- the worker's tick (DB, the database clock) --------------------------------------------------


class TagSource:
    def __init__(self):
        self.calls, self.fault, self.tags = 0, None, TAGS

    async def list_tags(self):
        self.calls += 1
        if self.fault:
            raise self.fault
        return self.tags

    async def close(self):
        pass


@pytest.fixture
def tags(registry, tmp_path):
    """A worker whose process clock is far from the database's: only the database clock decides."""
    repository = MediaRepository(registry.db, ManualClock(5.0), queue=RecordingMediaQueue(),
                                 times=DatabaseTransactionClock())
    source = TagSource()
    worker = MediaWorker(repository, MediaStore(repository, tmp_path / "media"),
                         {"fixture": ConnectionConfig(**configuration())},
                         preparer=FakePreparer(), client_factory=lambda _c: source)
    return repository, worker, source


def _age(db, seconds):
    with db.transaction() as conn:
        conn.execute("UPDATE library_tags SET checked_at=EXTRACT(EPOCH FROM clock_timestamp())-%s",
                     (seconds,))


def test_a_list_older_than_five_minutes_by_the_database_clock_is_relisted(tags):
    repository, worker, source = tags
    asyncio.run(worker.list_tags())  # never listed: listed now
    asyncio.run(worker.list_tags())
    assert source.calls == 1
    _age(repository.db, 290)
    asyncio.run(worker.list_tags())
    assert source.calls == 1
    _age(repository.db, 301)
    asyncio.run(worker.list_tags())
    assert source.calls == 2
    answer = repository.library_tags("fixture")
    assert answer["status"] == "ok" and answer["total_matches"] == 4


def test_a_failed_relist_keeps_the_last_list_with_its_age(tags):
    repository, worker, source = tags
    asyncio.run(worker.list_tags())
    listed_at = repository.library_tags("fixture")["observed_at"]
    source.fault = MediaError("upstream_unavailable")
    _age(repository.db, 301)
    asyncio.run(worker.list_tags())
    answer = repository.library_tags("fixture")
    assert source.calls == 2
    assert (answer["status"], answer["error"]) == ("unavailable", "upstream_unavailable")
    assert answer["observed_at"] == listed_at and answer["total_matches"] == 4
    assert answer["read_at"] - answer["observed_at"] >= 0  # one clock: the database's


def test_boot_replaces_every_list_and_drops_connections_no_longer_held(tags):
    repository, worker, source = tags
    asyncio.run(worker.list_tags())
    repository.record_library_tags("removed", (tag(9, "Old"),))
    asyncio.run(worker.list_tags(boot=True))  # fresh, yet listed again at boot
    assert source.calls == 2
    with repository.db.transaction() as conn:
        refs = [row["connection_ref"] for row in conn.execute(
            "SELECT connection_ref FROM library_tags").fetchall()]
    assert refs == ["fixture"]


# -- the GET (DB) --------------------------------------------------------------------------------


@pytest.fixture
def client(registry, tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTO_WALL_CACHE_ROOT", str(tmp_path))
    apply_procrastinate_schema(registry.db.dsn)
    app = create_app(registry.db, registry.clock, ADMIN, media_queue=RecordingMediaQueue(),
                     mdns_enabled=False)
    repository = app.state.media_repository
    repository.worker_status(None, ("fixture",))
    repository.record_library_tags("fixture", TAGS)
    with TestClient(app) as test_client:
        yield test_client, repository


def _get(test_client, **params):
    return test_client.get("/v1/operator/library/tags", params={"connection": "fixture", **params},
                           headers=AUTH)


def test_the_tag_get_serves_ids_paths_and_names_only(client):
    test_client, _ = client
    response = _get(test_client)
    answer = response.json()
    assert response.status_code == 200 and answer["status"] == "ok"
    assert all(set(item) == set(TAG_FIELDS) for item in answer["tags"])
    for private in (SECRET, "immich", "http", "api_key", "owner", str(UUID(int=1)) + "/"):
        assert private not in response.text


def test_prefix_matches_come_first_and_limit_cuts_after_counting(client):
    test_client, _ = client
    answer = _get(test_client, q="FAM").json()
    assert [t["path"] for t in answer["tags"]] == [
        "Family", "Family/Christmas", "Holidays/Family trip"]
    limited = _get(test_client, q="christ", limit=1).json()
    assert limited["total_matches"] == 1 and limited["tags"][0]["parent_ref"] == str(UUID(int=1))


@pytest.mark.parametrize("params", [{"q": "x" * 129}, {"limit": 21}, {"limit": 0}])
def test_oversized_queries_are_refused(client, params):
    test_client, _ = client
    assert _get(test_client, **params).status_code == 422


def test_an_unknown_connection_is_404_and_signed_out_is_401(client):
    test_client, _ = client
    assert _get(test_client, connection="elsewhere").json() == {"error": "connection_unknown"}
    assert test_client.get("/v1/operator/library/tags?connection=fixture").status_code == 401


def test_the_tag_get_is_read_only_even_for_a_stale_list(client):
    test_client, repository = client
    _age(repository.db, 3600)

    def state():
        with repository.db.transaction() as conn:
            row = conn.execute("SELECT checked_at, observed_at FROM library_tags").fetchone()
            jobs = conn.execute("SELECT count(*) AS n FROM procrastinate_jobs").fetchone()["n"]
        return row, jobs, len(repository.queue.refreshes), len(repository.queue.previews)

    before = state()
    assert _get(test_client).status_code == 200
    assert state() == before


# -- the lookup by id (C5; B5-L3-1) --------------------------------------------------------------


def test_ids_answer_the_named_tags_and_the_absent_from_an_ok_list(client):
    test_client, _ = client
    present, missing = str(UUID(int=4)), str(UUID(int=99))
    response = test_client.get("/v1/operator/library/tags",
                               params=[("connection", "fixture"), ("ids", present), ("ids", missing)],
                               headers=AUTH)
    answer = response.json()
    assert response.status_code == 200 and answer["status"] == "ok"
    assert [t["path"] for t in answer["tags"]] == ["Work"] and answer["absent"] == [missing]
    assert all(set(item) == set(TAG_FIELDS) for item in answer["tags"])
    for private in (SECRET, "immich", "http", "api_key", "owner"):
        assert private not in response.text


def test_a_pending_or_failed_list_names_nothing_absent(client):
    test_client, repository = client
    missing = str(UUID(int=99))
    repository.record_library_tags("fixture", None, MediaError("upstream_unavailable"))
    failed = _get(test_client, ids=[missing, str(UUID(int=1))]).json()
    assert failed["status"] == "unavailable" and failed["absent"] == []
    assert [t["path"] for t in failed["tags"]] == ["Family"]  # the last list still names it
    repository.worker_status(None, ("fixture", "fresh"))
    pending = _get(test_client, connection="fresh", ids=[missing]).json()
    assert pending["status"] == "pending" and pending["absent"] == [] and pending["tags"] == []


@pytest.mark.parametrize("params", [
    [("ids", "not-a-uuid")],
    [("ids", str(UUID(int=n))) for n in range(1, 6)],
    [("ids", str(UUID(int=1))), ("q", "fam")],
])
def test_malformed_too_many_or_ids_with_a_query_are_refused(client, params):
    test_client, _ = client
    response = test_client.get("/v1/operator/library/tags",
                               params=[("connection", "fixture"), *params], headers=AUTH)
    assert response.status_code == 422
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
