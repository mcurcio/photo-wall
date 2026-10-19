"""Settings › Photo library's routes (roadmap 4, slice C1; run ledger .claude/runs/photo-4.md, C1
cases 1-5): a connection is pasted with its key, which no answer, job or log ever carries again.

FastAPI's TestClient over create_app with a real database and Procrastinate's real queue.
"""

from __future__ import annotations

import logging
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from runtime_fakes import apply_procrastinate_schema

from central.app import create_app
from central.media_queue import CHECK_CONNECTION_TASK
from central.photo_library.model import (
    InPool,
    LibraryAsset,
    MeasuredCopy,
    OriginalFile,
    SourceQuery,
)
from central.photo_library.policy import REQUIRED_PERMISSIONS
from central.photo_library.ports import CheckStep

ADMIN = "photo-library-operator-" + "t" * 32
AUTH = {"Authorization": "Bearer " + ADMIN}
KEY = "PUBLIC-FIXTURE-KEY-1234"
CONNECTIONS = "/v1/operator/library/connections"
PHOTOS = "/v1/operator/library/photos/"


@pytest.fixture
def client(registry, tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTO_WALL_CACHE_ROOT", str(tmp_path))
    apply_procrastinate_schema(registry.db.dsn)
    app = create_app(registry.db, registry.clock, ADMIN)
    with TestClient(app) as test_client:
        yield test_client, app.state.photo_library


def _jobs(db):
    with db.transaction() as conn:
        return conn.execute("SELECT task_name,args,status,queueing_lock FROM procrastinate_jobs "
                            "ORDER BY id").fetchall()


def _ran(db):
    """The worker took and finished every queued job."""
    with db.transaction() as conn:
        conn.execute("UPDATE procrastinate_jobs SET status='succeeded' WHERE status='todo'")


def _add(test_client, **body):
    return test_client.post(CONNECTIONS, json={"name": "Home", "url": "http://immich.invalid:2283",
                                               "key": KEY, **body}, headers=AUTH)


def test_a_pasted_connection_is_listed_with_a_key_hint_and_not_checked(client):
    """Case 1."""
    test_client, _ = client
    created = _add(test_client)
    assert created.status_code == 201
    ref = created.json()["connection_ref"]
    assert ref.startswith("library-") and len(ref) == len("library-") + 12
    [row] = test_client.get(CONNECTIONS, headers=AUTH).json()
    assert row == {"connection_ref": ref, "name": "Home", "url": "http://immich.invalid:2283/api",
                   "key_hint": "…1234", "immich_user": None, "health": {"state": "not_checked"},
                   "counts": None, "required_permissions": list(REQUIRED_PERMISSIONS),
                   "managed": False}


def test_the_key_is_in_no_answer_job_or_log(client, caplog):
    """Case 2. Mutation probe: return api_key in LibraryConnectionView."""
    test_client, library = client
    caplog.set_level(logging.DEBUG)
    created = _add(test_client)
    ref = created.json()["connection_ref"]
    answers = [created, test_client.get(CONNECTIONS, headers=AUTH),
               test_client.put(f"{CONNECTIONS}/{ref}", json={"name": "Home", "url": "http://h",
                                                             "key": KEY + "-2"}, headers=AUTH),
               test_client.post(CONNECTIONS, json={"name": "x", "url": "ftp://x", "key": KEY},
                                headers=AUTH),
               test_client.post(CONNECTIONS, json={"name": "", "url": "http://h", "key": KEY},
                                headers=AUTH),
               test_client.get(CONNECTIONS, headers=AUTH)]
    for response in answers:
        assert KEY not in response.text
    jobs = _jobs(library.db)
    assert [(job["task_name"], job["args"]) for job in jobs] == [
        (CHECK_CONNECTION_TASK, {"connection_ref": ref})]
    assert KEY not in str(jobs)
    assert all(KEY not in record.getMessage() and KEY not in str(record.args)
               for record in caplog.records)


def test_refusals(client):
    """Case 3."""
    test_client, _ = client
    assert test_client.post(CONNECTIONS, json={"name": "Home", "url": "http://h"},
                            headers=AUTH).json() == {"error": "key_required"}
    refused = _add(test_client, url="ftp://x")
    assert (refused.status_code, refused.json()) == (422, {"error": "invalid_url"})
    unknown = test_client.put(f"{CONNECTIONS}/library-000000000000",
                              json={"name": "Home", "url": "http://h"}, headers=AUTH)
    assert (unknown.status_code, unknown.json()) == (404, {"error": "connection_unknown"})
    gone = test_client.delete(f"{CONNECTIONS}/library-000000000000", headers=AUTH)
    assert (gone.status_code, gone.json()) == (404, {"error": "connection_unknown"})
    assert test_client.get(CONNECTIONS).status_code == 401
    ref = _add(test_client).json()["connection_ref"]
    taken = _add(test_client)
    assert (taken.status_code, taken.json()) == (409, {"error": "connection_name_exists"})
    bad = test_client.put(f"{CONNECTIONS}/{ref}", json={"name": "Home", "url": "h"}, headers=AUTH)
    assert (bad.status_code, bad.json()) == (422, {"error": "invalid_url"})


def test_a_put_keeps_or_replaces_the_key_and_queues_a_check_each_time(client):
    """Case 4."""
    test_client, library = client
    ref = _add(test_client).json()["connection_ref"]
    _ran(library.db)
    renamed = test_client.put(f"{CONNECTIONS}/{ref}",
                              json={"name": "Family", "url": "https://photos.example/immich/"},
                              headers=AUTH)
    assert (renamed.status_code, renamed.json()) == (200, {"connection_ref": ref})
    secret = library.connection_secret(ref)
    assert secret.api_key.get_secret_value() == KEY
    assert secret.base_url == "https://photos.example/immich/api"
    assert [view.name for view in library.connections()] == ["Family"]
    _ran(library.db)
    test_client.put(f"{CONNECTIONS}/{ref}", json={"name": "Family", "url": "http://h",
                                                  "key": "NEW-KEY-5678"}, headers=AUTH)
    assert library.connection_secret(ref).api_key.get_secret_value() == "NEW-KEY-5678"
    assert library.connections()[0].key_hint == "…5678"
    _ran(library.db)
    assert test_client.post(f"{CONNECTIONS}/{ref}/checks", headers=AUTH).status_code == 202
    assert test_client.post(f"{CONNECTIONS}/{ref}/checks", headers=AUTH).status_code == 202
    jobs = _jobs(library.db)
    assert [job["status"] for job in jobs] == ["succeeded", "succeeded", "succeeded", "todo"]
    assert {job["task_name"] for job in jobs} == {CHECK_CONNECTION_TASK}
    unknown = test_client.post(f"{CONNECTIONS}/library-000000000000/checks", headers=AUTH)
    assert (unknown.status_code, unknown.json()) == (404, {"error": "connection_unknown"})


def test_a_cut_over_row_needs_its_key_once(client):
    test_client, library = client
    with library.db.transaction() as conn:
        conn.execute("INSERT INTO library_connections(connection_ref,name,created_at) "
                     "VALUES('family-library','family-library',0)")
    path = f"{CONNECTIONS}/family-library"
    assert test_client.put(path, json={"name": "family-library", "url": "http://h"},
                           headers=AUTH).json() == {"error": "key_required"}
    assert test_client.put(path, json={"name": "family-library", "url": "http://h", "key": KEY},
                           headers=AUTH).status_code == 200
    assert library.connection_secret("family-library").base_url == "http://h/api"


def test_a_connection_a_source_uses_is_not_removed(client):
    """Case 5."""
    test_client, library = client
    ref = _add(test_client).json()["connection_ref"]
    query = SourceQuery(connection_ref=ref)
    with library.db.transaction() as conn:
        conn.execute("INSERT INTO media_sources(source_ref,query) VALUES('family:1',%s)",
                     (Jsonb(query.model_dump(mode="json")),))
        conn.execute("INSERT INTO media_source_names(name,current_ref,revision) "
                     "VALUES('family','family:1',1)")
    refused = test_client.delete(f"{CONNECTIONS}/{ref}", headers=AUTH)
    assert (refused.status_code, refused.json()) == (409, {"error": "sources_use_connection"})
    with library.db.transaction() as conn:
        conn.execute("UPDATE media_source_names SET deleted=TRUE")
        conn.execute("UPDATE media_sources SET refresh_active=FALSE")
    assert test_client.delete(f"{CONNECTIONS}/{ref}", headers=AUTH).status_code == 204
    assert test_client.get(CONNECTIONS, headers=AUTH).json() == []


def test_a_pool_photos_facts_say_which_frames_the_preview_holds(client, registry):
    """Follow-ups review (the photo sheet's "plays from"): each Frame's need (its Frame output)
    and whether Immich's preview, as measured, holds it. A 1,080 x 1,440 preview holds a 600 x 800
    (3:4) box, not a 16:9 Frame full screen on a reported 1,920 x 1,080 Output; before the preview
    is measured, neither is known. A Frame with no Frame output (unbound here) asks nothing and is
    not listed (frame box run fb-8)."""
    from support.position import save_position
    from test_registry import enroll

    from central.registry import FrameCreate
    from contracts.models import Calibration, FrameProfile, PositionBox

    test_client, library = client
    ref = _add(test_client).json()["connection_ref"]
    shown = LibraryAsset(connection_ref=ref, upstream_id=str(UUID(int=8)), revision="c" * 40,
                         kind="image", taken_at=1_700_000_000.0, width=3024, height=4032)
    draft = library.create_draft(SourceQuery(connection_ref=ref), None)
    with library.db.transaction() as conn:
        pool_key = library.pool_of_draft_in(conn, draft)
    library.record_step(library.begin_check(pool_key),
                        CheckStep(progress={}, entries=(InPool(asset=shown),), seen_so_far=1))
    player, _, _ = enroll(registry, count=2)
    profile = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24)
    for frame_id in ("attic", "lounge", "study"):
        registry.create_frame(FrameCreate(id=frame_id, width_mm=530, height_mm=300, profile=profile))
    registry.bind("lounge", player["player_id"], "HDMI-A-1", expected_generation=0)
    save_position(registry, "lounge")
    registry.bind("study", player["player_id"], "HDMI-A-2", expected_generation=0)
    save_position(registry, "study", Calibration(fit=PositionBox(width=600 / 1920, height=800 / 1080)))

    unknown = test_client.get(PHOTOS + shown.asset_id, headers=AUTH).json()["frames"]
    assert unknown == [
        {"frame_id": "lounge", "need_px": [1920, 1080], "preview_holds": None},
        {"frame_id": "study", "need_px": [600, 800], "preview_holds": None}]
    library.record_copies(shown.asset_id, [MeasuredCopy(name="preview", media_type="image/jpeg",
                                                        width=1080, height=1440)])
    frames = test_client.get(PHOTOS + shown.asset_id, headers=AUTH).json()["frames"]
    assert {f["frame_id"]: f["preview_holds"] for f in frames} == {"lounge": False, "study": True}


def test_a_pool_photos_facts_name_its_original_and_immichs_copies(client):
    """Follow-ups 2026-10-11, slice A3 case 5: the original's facts from discovery, each of
    Immich's copies in order with the size Photo Wall measured, and the copy the current
    recipe's prepared copy was made from. An id no wanted pool holds is not found, and no
    Immich id reaches the answer (R22)."""
    test_client, library = client
    ref = _add(test_client).json()["connection_ref"]
    upstream = str(UUID(int=7))
    shown = LibraryAsset(
        connection_ref=ref, upstream_id=upstream, revision="b" * 40, kind="image",
        taken_at=1_700_000_000.0, width=3024, height=4032,
        original=OriginalFile(media_type="image/heic", width=4032, height=3024, orientation=6,
                              size_bytes=2_000_000))
    draft = library.create_draft(SourceQuery(connection_ref=ref), None)
    with library.db.transaction() as conn:
        pool_key = library.pool_of_draft_in(conn, draft)
        conn.execute("UPDATE library_connections SET full_size_copies=FALSE")
    library.record_step(library.begin_check(pool_key),
                        CheckStep(progress={}, entries=(InPool(asset=shown),), seen_so_far=1))
    library.record_copies(shown.asset_id, [MeasuredCopy(name="preview", media_type="image/jpeg",
                                                        width=1080, height=1440)])
    with library.db.transaction() as conn:
        # Two made media variants of the revision: an earlier recipe's and the newest one.
        for recipe, made_from, created in (("a" * 64, "preview", 1), ("b" * 64, "original", 2)):
            conn.execute("INSERT INTO assets(kind,identity,produced_size,produced_sha256,"
                         "produced_detail,created_at) VALUES('media-variant',%s,10,%s,%s,%s)",
                         (shown.asset_id + "." + recipe, recipe,
                          Jsonb({"media_type": "image/jpeg", "width": 10, "height": 10,
                                 "duration": None, "made_from": made_from}), created))

    answer = test_client.get(PHOTOS + shown.asset_id, headers=AUTH)
    assert answer.status_code == 200
    facts = answer.json()
    assert (facts["width"], facts["height"], facts["edited"]) == (3024, 4032, False)
    assert facts["original"] == {"media_type": "image/heic", "width": 4032, "height": 3024,
                                 "orientation": 6, "size_bytes": 2_000_000}
    assert [(c["name"], c["in_immich"]) for c in facts["copies"]] == [
        ("thumbnail", True), ("preview", True), ("fullsize", False), ("original", True)]
    measured = {c["name"]: c["measured"] for c in facts["copies"]}
    assert (measured["preview"]["width"], measured["preview"]["height"]) == (1080, 1440)
    assert measured["thumbnail"] is None and measured["original"] is None
    assert facts["prepared_from"] == "original"
    assert upstream not in answer.text and "immich.invalid" not in answer.text

    for unknown in ("asset-" + "0" * 64, "anything"):
        refused = test_client.get(PHOTOS + unknown, headers=AUTH)
        assert (refused.status_code, refused.json()) == (404, {"error": "photo_not_found"})
    with library.db.transaction() as conn:   # the pool is wanted only while its draft is
        library.remove_draft_in(conn, draft)
    refused = test_client.get(PHOTOS + shown.asset_id, headers=AUTH)
    assert (refused.status_code, refused.json()) == (404, {"error": "photo_not_found"})
    assert test_client.get(PHOTOS + shown.asset_id).status_code == 401
