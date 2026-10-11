"""Each library connection's tag list (console DDD §38 shape A, §40, G10; PR 37 §7, §9).

The GET reads stored data only and serves ids, paths and names. Listing is a connection check's
(roadmap 4: `LibraryChecks.contact`, tests/test_library_checks.py).
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
from test_media_worker import SECRET

from central.app import create_app
from central.media_repository import TAG_FIELDS
from media.models import LibraryTag, MediaError

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


@pytest.mark.parametrize("rows", [
    [raw_tag(n, f"t{n}") for n in range(1, 5002)],
    [raw_tag(1, "x" * 1025)],
])
def test_over_the_caps_is_tag_limit_never_a_cut_list(rows):
    with pytest.raises(MediaError) as caught:
        _listed(rows)
    assert caught.value.code == "tag_limit"


# -- the GET (DB): roadmap 4 serves it from the photo library store's lists ----------------------


def _seed_connection(db, connection_ref):
    """A connection row as C12's cut-over makes it (no URL or key yet): lists need only the row."""
    with db.transaction() as conn:
        conn.execute("INSERT INTO library_connections(connection_ref,name,created_at) "
                     "VALUES(%s,%s,0)", (connection_ref, connection_ref))


@pytest.fixture
def client(registry, tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTO_WALL_CACHE_ROOT", str(tmp_path))
    apply_procrastinate_schema(registry.db.dsn)
    app = create_app(registry.db, registry.clock, ADMIN, media_queue=RecordingMediaQueue())
    library = app.state.photo_library
    _seed_connection(registry.db, "fixture")
    library.record_list("fixture", "tags", TAGS, None)
    with TestClient(app) as test_client:
        yield test_client, library


def _get(test_client, **params):
    return test_client.get("/v1/operator/library/tags", params={"connection": "fixture", **params},
                           headers=AUTH)


def test_the_tag_get_serves_ids_paths_and_names_only(client):
    test_client, _ = client
    response = _get(test_client)
    answer = response.json()
    assert response.status_code == 200 and answer["status"] == "ok"
    assert answer["tags"] == answer["items"]   # the pre-4 key, until the integrate slice
    assert all(set(item) == set(TAG_FIELDS) for item in answer["items"])
    for private in (SECRET, "immich", "http", "api_key", "owner", str(UUID(int=1)) + "/"):
        assert private not in response.text


def test_prefix_matches_come_first_and_limit_cuts_after_counting(client):
    test_client, _ = client
    answer = _get(test_client, q="FAM").json()
    assert [t["path"] for t in answer["items"]] == [
        "Family", "Family/Christmas", "Holidays/Family trip"]
    limited = _get(test_client, q="christ", limit=1).json()
    assert limited["total_matches"] == 1 and limited["items"][0]["parent_ref"] == str(UUID(int=1))


@pytest.mark.parametrize("params", [{"q": "x" * 129}, {"limit": 21}, {"limit": 0}])
def test_oversized_queries_are_refused(client, params):
    test_client, _ = client
    assert _get(test_client, **params).status_code == 422


def test_an_unknown_connection_is_404_and_signed_out_is_401(client):
    test_client, _ = client
    assert _get(test_client, connection="elsewhere").json() == {"error": "connection_unknown"}
    assert test_client.get("/v1/operator/library/tags?connection=fixture").status_code == 401


def test_the_tag_get_is_read_only_even_for_a_stale_list(client):
    test_client, library = client
    with library.db.transaction() as conn:
        conn.execute("UPDATE library_lists SET checked_at=checked_at-3600")

    def state():
        with library.db.transaction() as conn:
            row = conn.execute("SELECT checked_at, observed_at FROM library_lists").fetchone()
            jobs = conn.execute("SELECT count(*) AS n FROM procrastinate_jobs").fetchone()["n"]
        return row, jobs

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
    assert [t["path"] for t in answer["items"]] == ["Work"] and answer["absent"] == [missing]
    assert all(set(item) == set(TAG_FIELDS) for item in answer["items"])
    for private in (SECRET, "immich", "http", "api_key", "owner"):
        assert private not in response.text


def test_a_pending_or_failed_list_names_nothing_absent(client):
    test_client, library = client
    missing = str(UUID(int=99))
    library.record_list("fixture", "tags", None, "unreachable")
    failed = _get(test_client, ids=[missing, str(UUID(int=1))]).json()
    assert failed["status"] == "unavailable" and failed["absent"] == []
    assert [t["path"] for t in failed["items"]] == ["Family"]  # the last list still names it
    _seed_connection(library.db, "fresh")
    pending = _get(test_client, connection="fresh", ids=[missing]).json()
    assert pending["status"] == "pending" and pending["absent"] == [] and pending["items"] == []


@pytest.mark.parametrize("params", [
    [("ids", "not-a-uuid")],
    [("ids", str(UUID(int=n))) for n in range(1, 34)],
    [("ids", str(UUID(int=1))), ("q", "fam")],
])
def test_malformed_too_many_or_ids_with_a_query_are_refused(client, params):
    test_client, _ = client
    response = test_client.get("/v1/operator/library/tags",
                               params=[("connection", "fixture"), *params], headers=AUTH)
    assert response.status_code == 422
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
