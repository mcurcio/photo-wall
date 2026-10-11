"""Library thumbnails on the one asset layer (console DDD §38 shape A, §40, G10; PR 37 §7-§9).

Unit: the re-encoding drops every metadata block, the client re-checks the photo's revision
before fetching its edited thumbnail, the worker's origin refuses an id no wanted pool holds
(roadmap 4, M3). DB: maintenance keeps the files of the records the photo library still holds;
the route's gate (admin first, `Sec-Fetch-Site` when sent, servability, CORP) and its short wait.
All images are generated here; no library media.
"""

from __future__ import annotations

import asyncio
import io
import stat
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from media_queue import RecordingMediaQueue
from PIL import Image
from procrastinate.testing import InMemoryConnector
from runtime_fakes import apply_procrastinate_schema
from support.library_store_fake import FakeLibraryStore
from test_immich import Upstream
from test_immich import asset as library_asset
from test_media_repository import sample, stored_member
from test_media_worker import FakePreparer, worker_storage  # noqa: F401

from central.app import create_app
from central.content_wiring import build_job_runtime, build_library_thumbnails
from central.infra.job_queue import _deferrer, build_app
from central.kernel.handling import OriginUnavailable, TerminalFailure
from central.kernel.job_types import CATALOG, FetchLibraryThumbnail, FetchOsImage
from central.kernel.jobs import QueueName
from central.media_repository import ServableThumbnail
from central.photo_library.model import ConnectionSecret, PoolThumbnail
from media.immich import THUMBNAIL_EDGE, edits_fingerprint, reencode_thumbnail
from media.library_thumbnails import LibraryThumbnailOrigin
from media.models import MediaError, SourcePreviewQuery
from media.worker import MediaWorker, ensure_previews_directory

ADMIN = "library-thumbnails-operator-" + "y" * 32
AUTH = {"Authorization": "Bearer " + ADMIN}


def jpeg_with_metadata(size=(800, 600)) -> bytes:
    image = Image.new("RGB", size, (200, 30, 30))
    exif = Image.Exif()
    exif[0x010F] = "SyntheticCam"  # Make
    output = io.BytesIO()
    image.save(output, "JPEG", exif=exif.tobytes(), icc_profile=b"synthetic-icc-profile",
               comment=b"synthetic private comment")
    return output.getvalue()


# -- re-encoding and the client (unit) -----------------------------------------------------------


def test_a_preview_tile_is_picked_after_every_other_fetch_job():
    """24 tiles share the FETCH queue with a Player's boot fetches: procrastinate picks
    `priority DESC, id ASC`, so a queued tile is picked after every queued fetch (pick order
    only: a running tile still holds a FETCH slot for up to `metadata_seconds`). The deferred
    job carries it, not only the declaration. Mutation probe: give `FetchLibraryThumbnail`
    the default priority and this fails."""
    others = [job for job in CATALOG
              if job.delivery.queue is QueueName.FETCH and job is not FetchLibraryThumbnail]
    assert others
    assert all(FetchLibraryThumbnail.delivery.priority < job.delivery.priority for job in others)
    app = build_app(InMemoryConnector(), CATALOG, None)

    def deferred(job):
        return _deferrer(app, job, connection=None, schedule_at=None).job.priority
    assert deferred(FetchLibraryThumbnail(asset_id="asset-" + "a" * 64)) < deferred(
        FetchOsImage(tarball_sha256="b" * 64))


def test_the_reencoded_thumbnail_carries_no_metadata_and_fits_320():
    source = jpeg_with_metadata()
    assert b"SyntheticCam" in source and b"synthetic-icc-profile" in source  # the probe is real
    data = reencode_thumbnail(source)
    assert b"SyntheticCam" not in data and b"synthetic-icc-profile" not in data
    assert b"synthetic private comment" not in data and b"Exif" not in data
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "JPEG" and max(image.size) == THUMBNAIL_EDGE
        assert len(image.getexif()) == 0
        assert not {"exif", "icc_profile", "comment", "xmp"} & set(image.info)


@pytest.mark.parametrize("hostile", [
    b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
    b"<html><body>not an image</body></html>",
])
def test_bytes_that_are_not_a_listed_image_never_become_a_thumbnail(hostile):
    with pytest.raises(MediaError) as caught:
        reencode_thumbnail(hostile)
    assert caught.value.code == "thumbnail_invalid"


def test_a_thumbnail_over_four_megapixels_is_refused_before_decoding():
    output = io.BytesIO()
    Image.new("RGB", (2100, 2000)).save(output, "PNG")
    with pytest.raises(MediaError) as caught:
        reencode_thumbnail(output.getvalue())
    assert caught.value.code == "thumbnail_oversize"


def _thumbnail_upstream(row, *, response):
    upstream = Upstream([row])
    upstream.overrides[f"/api/assets/{row['id']}/thumbnail"] = response
    return upstream


def _fetch(upstream, row, checksum=None):
    async def perform():
        async with upstream.client() as client:
            return await client.thumbnail(row["id"], checksum or stored_checksum(row))
    return asyncio.run(perform())


def stored_checksum(row):
    import base64
    return base64.b64decode(row["checksum"]).hex()


def test_the_client_rechecks_the_item_then_returns_its_own_encoding_of_the_edited_one():
    row = library_asset(7)
    upstream = _thumbnail_upstream(row, response=httpx.Response(
        200, content=jpeg_with_metadata(), headers={"content-type": "image/jpeg"}))
    data = _fetch(upstream, row)
    assert b"SyntheticCam" not in data
    paths = [request.url.path for request in upstream.requests]
    assert paths[-2:] == [f"/api/assets/{row['id']}", f"/api/assets/{row['id']}/thumbnail"]
    assert dict(upstream.requests[-1].url.params) == {"size": "thumbnail", "edited": "true"}


def test_an_edited_photos_thumbnail_is_fetched_at_its_edit_revision():
    edits = [{"action": "crop", "parameters": {"x": 0, "y": 0, "width": 40, "height": 30}}]
    row = {**library_asset(12), "isEdited": True}
    upstream = _thumbnail_upstream(row, response=httpx.Response(
        200, content=jpeg_with_metadata(), headers={"content-type": "image/jpeg"}))
    upstream.overrides[f"/api/assets/{row['id']}/edits"] = httpx.Response(
        200, json={"assetId": row["id"], "edits": edits})
    revision = f"{stored_checksum(row)}-{edits_fingerprint(edits)}"
    assert b"SyntheticCam" not in _fetch(upstream, row, revision)
    with pytest.raises(MediaError) as caught:   # the unedited revision is no longer shown
        _fetch(upstream, row, stored_checksum(row))
    assert caught.value.code == "asset_changed"


@pytest.mark.parametrize(("change", "response", "code"), [
    ({"isTrashed": True}, None, "asset_changed"),
    ({"checksum": "AAAAAAAAAAAAAAAAAAAAAAAAAAA="}, None, "asset_changed"),
    ({}, httpx.Response(404), "thumbnail_not_ready"),
    ({}, httpx.Response(200, content=b"<svg/>", headers={"content-type": "image/svg+xml"}),
     "thumbnail_unsupported"),
])
def test_a_changed_item_or_an_unusable_thumbnail_is_refused(change, response, code):
    original = library_asset(8)
    row = {**original, **change}
    upstream = _thumbnail_upstream(row, response=response or httpx.Response(
        200, content=jpeg_with_metadata(), headers={"content-type": "image/jpeg"}))
    with pytest.raises(MediaError) as caught:
        _fetch(upstream, row, stored_checksum(original))
    assert caught.value.code == code


# -- the worker's origin (unit) ------------------------------------------------------------------


class ThumbnailClient:
    def __init__(self, fault=None):
        self.fault, self.calls = fault, []

    async def thumbnail(self, upstream_id, revision):
        self.calls.append((upstream_id, revision))
        if self.fault:
            raise self.fault
        return reencode_thumbnail(jpeg_with_metadata())

    async def close(self):
        pass


SECRET = ConnectionSecret(connection_ref="fixture", base_url="https://immich.invalid/api",
                          api_key="synthetic-thumbnail-key")


def _origin(servable, client, secret=SECRET):
    return LibraryThumbnailOrigin(lambda _id: servable, lambda _ref: secret, lambda _s: client)


def _pool_row(member):
    return PoolThumbnail(connection_ref="fixture", upstream_id=member.upstream_id,
                         revision=member.checksum)


def test_the_origin_refuses_an_id_no_wanted_pool_holds_without_calling_the_library(tmp_path):
    client = ThumbnailClient()
    with pytest.raises(TerminalFailure) as caught:
        asyncio.run(_origin(None, client).thumbnail("asset-" + "1" * 64, tmp_path / "t"))
    assert caught.value.reason == "thumbnail_unknown" and client.calls == []
    assert not (tmp_path / "t").exists()


@pytest.mark.parametrize(("fault", "failure"), [
    (MediaError("upstream_unavailable"), OriginUnavailable),
    (MediaError("asset_changed", "incompatible"), TerminalFailure),
])
def test_library_failures_map_to_retry_or_terminal(tmp_path, fault, failure):
    member = stored_member(3)
    with pytest.raises(failure):
        asyncio.run(_origin(_pool_row(member), ThumbnailClient(fault)).thumbnail(
            member.asset_id, tmp_path / "t"))
    assert not (tmp_path / "t").exists()


def test_the_origin_writes_a_private_new_file_from_the_pool_rows_identity(tmp_path):
    member = stored_member(4)
    client = ThumbnailClient()
    asyncio.run(_origin(_pool_row(member), client).thumbnail(member.asset_id, tmp_path / "t"))
    assert client.calls == [(member.upstream_id, member.checksum)]
    assert stat.S_IMODE((tmp_path / "t").stat().st_mode) == 0o600


def test_the_origin_refuses_a_connection_with_no_key_and_rekeys_its_client(tmp_path):
    member = stored_member(5)
    with pytest.raises(TerminalFailure) as caught:
        asyncio.run(_origin(_pool_row(member), ThumbnailClient(), secret=None).thumbnail(
            member.asset_id, tmp_path / "t"))
    assert caught.value.reason == "connection_unknown"
    secrets, made = [SECRET], []
    origin = LibraryThumbnailOrigin(lambda _id: _pool_row(member), lambda _ref: secrets[-1],
                                    lambda secret: made.append(secret) or ThumbnailClient())
    asyncio.run(origin.thumbnail(member.asset_id, tmp_path / "a"))
    asyncio.run(origin.thumbnail(member.asset_id, tmp_path / "b"))
    secrets.append(ConnectionSecret(connection_ref="fixture", base_url=SECRET.base_url,
                                    api_key="another-synthetic-key"))
    asyncio.run(origin.thumbnail(member.asset_id, tmp_path / "c"))
    assert len(made) == 2   # a new key gets a new client


# -- DB: servability, prefetch, retirement --------------------------------------------------------


def _complete(repo, *members):
    receipt = repo.request_source_preview(SourcePreviewQuery(connection_ref="fixture"))
    assert repo.finish_source_preview(receipt.request_id, sample(*members))
    return receipt.request_id


def test_servable_means_a_member_of_a_live_preview(worker_storage):  # noqa: F811
    repo = worker_storage.repository
    live, other = stored_member(1), stored_member(2)
    _complete(repo, live)
    pending = repo.request_source_preview(SourcePreviewQuery(connection_ref="fixture"))
    servable = repo.servable_thumbnail(live.asset_id)
    assert servable == ServableThumbnail("fixture", live)
    assert repo.servable_thumbnail(other.asset_id) is None
    assert repo.servable_thumbnail("asset-" + "f" * 64) is None
    assert pending.request_id  # a pending preview has no members to serve
    worker_storage.clock.advance(601)  # expired by the (test) transaction clock
    assert repo.servable_thumbnail(live.asset_id) is None


def _thumbnail_jobs(db):
    with db.transaction() as conn:
        return conn.execute("SELECT count(*) AS n FROM procrastinate_jobs WHERE task_name LIKE "
                            "'%%library_thumbnail.fetch' AND status='todo'").fetchone()["n"]


def _drop_thumbnail_jobs(db):
    with db.transaction() as conn:
        conn.execute("DELETE FROM procrastinate_jobs WHERE task_name LIKE "
                     "'%%library_thumbnail.fetch' AND status='todo'")


def _thumbnail_records(db):
    with db.transaction() as conn:
        return {row["identity"] for row in conn.execute(
            "SELECT identity FROM assets WHERE kind='library-thumbnail'").fetchall()}


def test_maintenance_keeps_only_the_files_the_photo_library_still_records(
        worker_storage, tmp_path):  # noqa: F811
    previews = ensure_previews_directory(tmp_path)
    old, kept = stored_member(1), stored_member(2)
    for member in (old, kept):
        (previews / f"{member.asset_id}.jpg").write_bytes(b"jpeg")
    (previews / ".tmp-inflight").write_bytes(b"partial")
    library = FakeLibraryStore(worker_storage.clock)
    library.retire_unwanted = lambda: frozenset({kept.asset_id})
    instance = MediaWorker(worker_storage.repository, worker_storage, library,
                           preparer=FakePreparer(), previews=previews)
    instance.store.recover = lambda: None
    asyncio.run(instance.maintain())
    assert sorted(p.name for p in previews.iterdir()) == [".tmp-inflight", f"{kept.asset_id}.jpg"]


# -- DB: the route -------------------------------------------------------------------------------


class FakeOrigin:
    async def thumbnail(self, asset_id, into):
        into.write_bytes(reencode_thumbnail(jpeg_with_metadata()))


@pytest.fixture
def route(registry, tmp_path, monkeypatch):
    root = tmp_path / "cache"
    root.mkdir()  # an existing volume made before previews/ existed
    monkeypatch.setenv("PHOTO_WALL_CACHE_ROOT", str(root))
    apply_procrastinate_schema(registry.db.dsn)
    app = create_app(registry.db, registry.clock, ADMIN, media_queue=RecordingMediaQueue())
    return app, root


def _url(asset_id):
    return f"/v1/operator/library/thumbnails/{asset_id}"


def _nothing_queued(db):
    assert _thumbnail_jobs(db) == 0 and _thumbnail_records(db) == set()


def test_a_servable_thumbnail_answers_within_about_two_seconds_then_serves(route, registry):
    app, root = route
    member = stored_member(5)
    _complete(app.state.media_repository, member)
    with TestClient(app) as client:
        started = time.monotonic()
        cold = client.get(_url(member.asset_id), headers={**AUTH, "Sec-Fetch-Site": "same-origin"})
        elapsed = time.monotonic() - started
        assert cold.status_code == 503 and cold.json() == {"error": "thumbnail_timeout"}
        assert cold.headers["retry-after"] == "5" and 1.5 < elapsed < 4
        assert cold.headers["cross-origin-resource-policy"] == "same-origin"
        assert _thumbnail_jobs(registry.db) == 1
        # The worker boots on this existing volume, creates previews/ and runs the fetch.
        assert not (root / "previews").exists()
        assert stat.S_IMODE(ensure_previews_directory(root).stat().st_mode) == 0o700
        runtime = build_job_runtime(registry.db, registry.clock, cache_root=root, env={},
                                    thumbnails=FakeOrigin())
        job = FetchLibraryThumbnail(asset_id=member.asset_id)
        assert asyncio.run(runtime._executor.execute(job, 0)) == "ok"
        served = client.get(_url(member.asset_id), headers=AUTH)
    assert served.status_code == 200 and served.headers["content-type"] == "image/jpeg"
    assert served.headers["cross-origin-resource-policy"] == "same-origin"
    assert served.headers["x-content-type-options"] == "nosniff"
    assert served.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert served.headers["cache-control"] == "no-store"
    assert b"SyntheticCam" not in served.content


class RegeneratingOrigin:
    """The library's thumbnail for one item, regenerated (new bytes) between fetches."""

    def __init__(self):
        self.colour, self.calls = (200, 30, 30), 0

    async def thumbnail(self, asset_id, into):
        self.calls += 1
        output = io.BytesIO()
        Image.new("RGB", (400, 300), self.colour).save(output, "JPEG")
        into.write_bytes(reencode_thumbnail(output.getvalue()))


def test_a_purged_then_regenerated_thumbnail_serves_again_and_is_not_refetched(route, registry):
    """A thumbnail is keyed by its original, so after a cache purge the library's regenerated
    bytes REPLACE its produced facts (`AssetKind.keyed_by_content`) instead of turning it
    terminal `not_reproducible` while previews keep selecting it (B5-FC2-4); once served, a
    tile request is a hit that publishes no fetch. Covers a repeated preview keeping the record
    past the first one's expiry. Mutation probe: raise `not_reproducible` for every kind in
    `AssetProduction.produce` and the second fetch is terminal."""
    app, root = route
    repo, member = app.state.media_repository, stored_member(9)
    origin = RegeneratingOrigin()
    runtime = build_job_runtime(registry.db, registry.clock, cache_root=root, env={},
                                thumbnails=origin)
    job = FetchLibraryThumbnail(asset_id=member.asset_id)
    previews = ensure_previews_directory(root)
    _complete(repo, member)
    asyncio.run(build_library_thumbnails(registry.db, registry.clock, cache_root=root)
                .prefetch([member.asset_id]))
    with TestClient(app) as client:
        for colour in ((200, 30, 30), (30, 200, 30), (30, 30, 200)):
            origin.colour = colour
            for stale in previews.iterdir():  # the purge
                stale.unlink()
            assert asyncio.run(runtime._executor.execute(job, 0)) == "ok"
            # The prefetch's queued job was run directly above; drop it so any publish by a
            # GET below shows up as a new queued job (a merge into it would hide one).
            _drop_thumbnail_jobs(registry.db)
            served = [client.get(_url(member.asset_id), headers=AUTH) for _ in range(3)]
            assert [response.status_code for response in served] == [200, 200, 200]
            with Image.open(io.BytesIO(served[0].content)) as image:
                assert image.convert("RGB").getpixel((10, 10))[colour.index(200)] > 150
            assert _thumbnail_jobs(registry.db) == 0  # hits: nothing published
            # A repeated preview keeps the record past the first one's expiry.
            registry.clock.advance(500)
            _complete(repo, member)
            registry.clock.advance(200)
            assert member.asset_id in repo.maintain_source_previews()
    assert origin.calls == 3


def test_a_fetch_for_a_retired_record_never_blocks_the_next_preview(route, registry):
    """A tile job that runs after its preview expired and maintenance retired the record is
    terminal `thumbnail_unknown`, never a transient retry window: the next preview's prefetch
    is a new ask (`retry_terminal`), which a window would silently drop (PB2), leaving the
    GET refused `thumbnail_asset_not_recorded` for up to the last retry delay. Mutation probe:
    make the missing record transient again in `AssetProduction.produce` and the prefetch
    below queues nothing."""
    app, root = route
    repo, member = app.state.media_repository, stored_member(11)
    runtime = build_job_runtime(registry.db, registry.clock, cache_root=root, env={},
                                thumbnails=FakeOrigin())
    thumbnails = build_library_thumbnails(registry.db, registry.clock, cache_root=root)
    job = FetchLibraryThumbnail(asset_id=member.asset_id)
    _complete(repo, member)
    asyncio.run(thumbnails.prefetch([member.asset_id]))
    registry.clock.advance(700)  # the preview expires before its tile job runs
    assert member.asset_id not in repo.maintain_source_previews()  # the record is retired
    assert asyncio.run(runtime._executor.execute(job, 0)) == "terminal"
    _drop_thumbnail_jobs(registry.db)
    _complete(repo, member)  # a new preview selects the same item
    asyncio.run(thumbnails.prefetch([member.asset_id]))
    assert _thumbnail_jobs(registry.db) == 1  # the new ask is queued at once
    assert asyncio.run(runtime._executor.execute(job, 0)) == "ok"
    with TestClient(app) as client:
        served = client.get(_url(member.asset_id), headers=AUTH)
    assert served.status_code == 200 and served.headers["content-type"] == "image/jpeg"


@pytest.mark.parametrize("asset_id", ["asset-" + "e" * 64, "not-an-asset", "asset-" + "E" * 64])
def test_an_id_no_live_preview_selects_is_404_and_queues_nothing(route, registry, asset_id):
    app, _ = route
    _complete(app.state.media_repository, stored_member(6))
    with TestClient(app) as client:
        response = client.get(_url(asset_id), headers=AUTH)  # no Sec-Fetch-Site (plain http)
    assert response.status_code == 404 and response.json() == {"error": "thumbnail_unknown"}
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    _nothing_queued(registry.db)


@pytest.mark.parametrize("site", ["cross-site", "same-site", "none"])
def test_a_cross_site_request_is_refused_before_servability(route, registry, site):
    app, _ = route
    member = stored_member(7)
    _complete(app.state.media_repository, member)
    with TestClient(app) as client:
        response = client.get(_url(member.asset_id), headers={**AUTH, "Sec-Fetch-Site": site})
    assert response.status_code == 403 and response.json() == {"error": "origin_mismatch"}
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    _nothing_queued(registry.db)


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer player-" + "z" * 40}])
def test_signed_out_and_player_credentials_get_one_401_for_any_id(route, registry, headers):
    app, _ = route
    member = stored_member(8)
    _complete(app.state.media_repository, member)
    with TestClient(app) as client:
        known = client.get(_url(member.asset_id), headers=headers)
        unknown = client.get(_url("asset-" + "d" * 64), headers=headers)
    assert known.status_code == unknown.status_code == 401
    assert known.json() == unknown.json() == {"error": "unauthorized"}
    assert known.headers["cross-origin-resource-policy"] == "same-origin"  # every answer
    _nothing_queued(registry.db)


def test_an_unhandled_library_500_still_carries_corp_and_no_store(route, monkeypatch):
    """Starlette answers an unhandled exception outside every user middleware; the 500 handler
    applies the same prefix table (`add_prefix_headers`), so CORP holds on every answer.
    Mutation probe: register the library's CORP as a bare middleware and this fails."""
    app, _ = route
    member = stored_member(10)
    _complete(app.state.media_repository, member)

    async def boom(_asset_id):
        raise RuntimeError("boom")
    monkeypatch.setattr(app.state.content.thumbnails, "resolve", boom)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(_url(member.asset_id), headers=AUTH)
    assert response.status_code == 500 and "boom" not in response.text
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert response.headers["cache-control"] == "no-store"


def test_create_app_keeps_library_query_strings_out_of_the_access_log(route):
    import logging
    access = logging.getLogger("uvicorn.access")

    def line(path):
        record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                                   '%s - "%s %s HTTP/%s" %d',
                                   ("10.0.0.9:5", "GET", path, "1.1", 200), None)
        assert access.filter(record)
        return record.getMessage()

    assert "?" not in line("/v1/operator/library/tags?connection=home&q=Christmas")
    assert "Christmas" not in line("/v1/operator/library/thumbnails/asset-1?x=Christmas")
    assert line("/v1/operator/media?x=1").endswith('/v1/operator/media?x=1 HTTP/1.1" 200')


def test_the_preview_member_ids_are_canonical_uuids():
    # The stored identity the origin sends upstream is a validated UUID, never a path segment.
    with pytest.raises(ValueError):
        stored_member(1).model_validate({**stored_member(1).model_dump(), "upstream_id": "../x"})
    assert str(uuid.UUID(stored_member(1).upstream_id)) == stored_member(1).upstream_id
