"""Library thumbnails on the one asset layer (console DDD §38 shape A, §40, G10; PR 37 §7-§9).

Unit: the re-encoding drops every metadata block, the client re-checks the item before fetching,
the worker's origin refuses a non-servable id. DB: servability is a member of a LIVE preview; a
completed preview queues its tiles once; maintenance retires records no live preview selects;
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
from test_immich import Upstream
from test_immich import asset as library_asset
from test_media_repository import sample, stored_member
from test_media_worker import FakePreparer, FakeSource, configuration, worker_storage  # noqa: F401

from central.app import create_app
from central.content_wiring import build_job_runtime, build_library_thumbnails
from central.infra.job_queue import _deferrer, build_app
from central.kernel.handling import OriginUnavailable, TerminalFailure
from central.kernel.job_types import CATALOG, FetchLibraryThumbnail, FetchOsImage
from central.kernel.jobs import QueueName
from central.media_repository import ServableThumbnail
from media.immich import THUMBNAIL_EDGE, reencode_thumbnail
from media.library_thumbnails import LibraryThumbnailOrigin
from media.models import ConnectionConfig, MediaError, SourcePreviewQuery
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


def test_the_client_rechecks_the_item_then_returns_its_own_encoding():
    row = library_asset(7)
    upstream = _thumbnail_upstream(row, response=httpx.Response(
        200, content=jpeg_with_metadata(), headers={"content-type": "image/jpeg"}))
    data = _fetch(upstream, row)
    assert b"SyntheticCam" not in data
    paths = [request.url.path for request in upstream.requests]
    assert paths[-2:] == [f"/api/assets/{row['id']}", f"/api/assets/{row['id']}/thumbnail"]


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

    async def thumbnail(self, upstream_id, checksum):
        self.calls.append((upstream_id, checksum))
        if self.fault:
            raise self.fault
        return reencode_thumbnail(jpeg_with_metadata())

    async def close(self):
        pass


def _origin(servable, client):
    config = ConnectionConfig(**configuration())
    return LibraryThumbnailOrigin(lambda _id: servable, {"fixture": config}, lambda _c: client)


def test_the_origin_refuses_an_id_no_live_preview_selects_without_calling_the_library(tmp_path):
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
    servable = ServableThumbnail("fixture", stored_member(3))
    with pytest.raises(failure):
        asyncio.run(_origin(servable, ThumbnailClient(fault)).thumbnail(
            servable.member.asset_id, tmp_path / "t"))
    assert not (tmp_path / "t").exists()


def test_the_origin_writes_a_private_new_file_from_the_stored_identity(tmp_path):
    member = stored_member(4)
    client = ThumbnailClient()
    asyncio.run(_origin(ServableThumbnail("fixture", member), client).thumbnail(
        member.asset_id, tmp_path / "t"))
    assert client.calls == [(member.upstream_id, member.checksum)]
    assert stat.S_IMODE((tmp_path / "t").stat().st_mode) == 0o600


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


class PrefetchSpy:
    def __init__(self, inner):
        self.inner, self.calls = inner, []

    async def prefetch(self, asset_ids):
        self.calls.append(list(asset_ids))
        await self.inner.prefetch(asset_ids)


class TwentyFour(FakeSource):
    members = tuple(stored_member(n, captured_at=900.0 + n) for n in range(1, 25))

    async def preview(self, query):
        self.started = getattr(self, "started", 0) + 1
        while self.started < 2:  # both deliveries hold the pending row before either finishes
            await asyncio.sleep(0.005)
        return sample(*sorted(self.members, key=lambda m: -m.captured_at))


def _thumbnail_jobs(db):
    with db.transaction() as conn:
        return conn.execute("SELECT count(*) AS n FROM procrastinate_jobs WHERE task_name LIKE "
                            "'%%library_thumbnail.fetch' AND status='todo'").fetchone()["n"]


def _thumbnail_records(db):
    with db.transaction() as conn:
        return {row["identity"] for row in conn.execute(
            "SELECT identity FROM assets WHERE kind='library-thumbnail'").fetchall()}


def test_a_completed_preview_queues_its_24_tiles_exactly_once(worker_storage, tmp_path):  # noqa: F811
    db = worker_storage.db
    apply_procrastinate_schema(db.dsn)
    spy = PrefetchSpy(build_library_thumbnails(db, worker_storage.clock, cache_root=tmp_path))
    source = TwentyFour(None)
    instance = MediaWorker(worker_storage.repository, worker_storage,
                           {"fixture": ConnectionConfig(**configuration())},
                           preparer=FakePreparer(), client_factory=lambda _c: source,
                           thumbnails=spy)
    receipt = worker_storage.repository.request_source_preview(
        SourcePreviewQuery(connection_ref="fixture"))

    async def two_deliveries():  # e.g. a rescued job and its original, racing
        await asyncio.gather(instance.preview_source(receipt.request_id),
                             instance.preview_source(receipt.request_id))
    asyncio.run(asyncio.wait_for(two_deliveries(), 10))
    asyncio.run(instance.preview_source(receipt.request_id))  # later: already complete
    ids = {member.asset_id for member in TwentyFour.members}
    assert len(spy.calls) == 1 and set(spy.calls[0]) == ids
    assert _thumbnail_jobs(db) == 24 and _thumbnail_records(db) == ids


def test_maintenance_retires_records_and_files_no_live_preview_selects(worker_storage, tmp_path):  # noqa: F811
    db, repo = worker_storage.db, worker_storage.repository
    apply_procrastinate_schema(db.dsn)
    thumbnails = build_library_thumbnails(db, worker_storage.clock, cache_root=tmp_path)
    previews = ensure_previews_directory(tmp_path)
    old, kept = stored_member(1), stored_member(2)
    _complete(repo, old)
    asyncio.run(thumbnails.prefetch([old.asset_id]))
    worker_storage.clock.advance(601)  # the first preview expires
    _complete(repo, kept)
    asyncio.run(thumbnails.prefetch([kept.asset_id]))
    for member in (old, kept):
        (previews / f"{member.asset_id}.jpg").write_bytes(b"jpeg")
    (previews / ".tmp-inflight").write_bytes(b"partial")
    instance = MediaWorker(repo, worker_storage, {}, preparer=FakePreparer(), previews=previews)
    instance.store.recover = lambda: None
    asyncio.run(instance.maintain())
    assert _thumbnail_records(db) == {kept.asset_id}
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
    app = create_app(registry.db, registry.clock, ADMIN, media_queue=RecordingMediaQueue(),
                     mdns_enabled=False)
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
