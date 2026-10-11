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
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from media_queue import RecordingMediaQueue
from PIL import Image
from procrastinate.testing import InMemoryConnector
from runtime_fakes import apply_procrastinate_schema
from support.library_store_fake import FakeLibraryStore
from test_media_worker import FakePreparer, worker_storage  # noqa: F401

from central.app import create_app
from central.content_wiring import build_job_runtime, build_library_thumbnails
from central.infra.job_queue import _deferrer, build_app
from central.kernel.handling import OriginUnavailable, TerminalFailure
from central.kernel.job_types import CATALOG, FetchLibraryThumbnail, FetchOsImage
from central.kernel.jobs import QueueName
from central.photo_library.model import (
    ConnectionSecret,
    InPool,
    LibraryAsset,
    PoolThumbnail,
    SourceQuery,
)
from central.photo_library.ports import CheckStep
from central.photo_library.views import ConnectionWrite
from media.immich import THUMBNAIL_EDGE, reencode_thumbnail
from media.library_thumbnails import LibraryThumbnailOrigin
from media.models import MediaError
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


def _row(number):
    """A wanted pool row's library identity, and its asset id."""
    upstream_id, revision = str(uuid.UUID(int=number)), f"{number:040x}"
    row = PoolThumbnail(connection_ref="fixture", upstream_id=upstream_id, revision=revision)
    return row, f"asset-{number:064x}"


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
    row, asset_id = _row(3)
    with pytest.raises(failure):
        asyncio.run(_origin(row, ThumbnailClient(fault)).thumbnail(asset_id, tmp_path / "t"))
    assert not (tmp_path / "t").exists()


def test_the_origin_writes_a_private_new_file_from_the_pool_rows_identity(tmp_path):
    row, asset_id = _row(4)
    client = ThumbnailClient()
    asyncio.run(_origin(row, client).thumbnail(asset_id, tmp_path / "t"))
    assert client.calls == [(row.upstream_id, row.revision)]
    assert stat.S_IMODE((tmp_path / "t").stat().st_mode) == 0o600


def test_the_origin_refuses_a_connection_with_no_key_and_rekeys_its_client(tmp_path):
    row, asset_id = _row(5)
    with pytest.raises(TerminalFailure) as caught:
        asyncio.run(_origin(row, ThumbnailClient(), secret=None).thumbnail(
            asset_id, tmp_path / "t"))
    assert caught.value.reason == "connection_unknown"
    secrets, made = [SECRET], []
    origin = LibraryThumbnailOrigin(lambda _id: row, lambda _ref: secrets[-1],
                                    lambda secret: made.append(secret) or ThumbnailClient())
    asyncio.run(origin.thumbnail(asset_id, tmp_path / "a"))
    asyncio.run(origin.thumbnail(asset_id, tmp_path / "b"))
    secrets.append(ConnectionSecret(connection_ref="fixture", base_url=SECRET.base_url,
                                    api_key="another-synthetic-key"))
    asyncio.run(origin.thumbnail(asset_id, tmp_path / "c"))
    secrets.append(ConnectionSecret(connection_ref="fixture", base_url="https://other.invalid/api",
                                    api_key="another-synthetic-key"))
    asyncio.run(origin.thumbnail(asset_id, tmp_path / "d"))
    assert len(made) == 3   # a new key, then a new address, each gets a new client


# -- DB: servability, prefetch, retirement --------------------------------------------------------


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
    old, kept = (SimpleNamespace(asset_id=f"asset-{digit * 64}") for digit in "12")
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


def _pool_photo(library, number):
    """A wanted pool's `in_pool` row, made through the store's own sides: a draft wants the
    pool, a check begins and records one step holding the photo. Returns (asset id, draft)."""
    refs = {view.name: view.connection_ref for view in library.connections()}
    ref = refs.get("Home") or library.add_connection(ConnectionWrite(
        name="Home", url="http://immich.invalid", key="PUBLIC-FIXTURE-KEY-1234"))
    query = SourceQuery(connection_ref=ref)
    draft = library.create_draft(query, None)
    photo = LibraryAsset(connection_ref=ref, upstream_id=str(uuid.UUID(int=number)),
                         revision=f"{number:040x}", kind="image", taken_at=100.0 + number,
                         width=4000, height=3000)
    lease = library.begin_check(query.pool_key())
    assert library.record_step(lease, CheckStep(progress={"page": 1}, entries=(InPool(
        asset=photo),), seen_so_far=1))
    return photo.asset_id, draft


def test_a_servable_thumbnail_answers_within_about_two_seconds_then_serves(route, registry):
    app, root = route
    asset_id, _ = _pool_photo(app.state.photo_library, 5)
    member = SimpleNamespace(asset_id=asset_id)
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
    terminal `not_reproducible` while a wanted pool keeps selecting it (B5-FC2-4); once served,
    a tile request is a hit that publishes no fetch. Covers retirement keeping the record while
    its pool is wanted. Mutation probe: raise `not_reproducible` for every kind in
    `AssetProduction.produce` and the second fetch is terminal."""
    app, root = route
    library = app.state.photo_library
    asset_id, _ = _pool_photo(library, 9)
    member = SimpleNamespace(asset_id=asset_id)
    origin = RegeneratingOrigin()
    runtime = build_job_runtime(registry.db, registry.clock, cache_root=root, env={},
                                thumbnails=origin)
    job = FetchLibraryThumbnail(asset_id=member.asset_id)
    previews = ensure_previews_directory(root)
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
            # Retirement keeps the record while its pool is wanted.
            assert member.asset_id in library.retire_unwanted()
    assert origin.calls == 3


def test_a_fetch_for_a_retired_record_never_blocks_the_next_preview(route, registry):
    """A tile job that runs after its pool stopped being wanted and maintenance retired the
    record is terminal `thumbnail_unknown`, never a transient retry window: the next pool's
    prefetch is a new ask (`retry_terminal`), which a window would silently drop (PB2), leaving
    the GET refused `thumbnail_asset_not_recorded` for up to the last retry delay. Mutation
    probe: make the missing record transient again in `AssetProduction.produce` and the
    prefetch below queues nothing."""
    app, root = route
    library = app.state.photo_library
    asset_id, draft = _pool_photo(library, 11)
    member = SimpleNamespace(asset_id=asset_id)
    runtime = build_job_runtime(registry.db, registry.clock, cache_root=root, env={},
                                thumbnails=FakeOrigin())
    thumbnails = build_library_thumbnails(registry.db, registry.clock, cache_root=root)
    job = FetchLibraryThumbnail(asset_id=member.asset_id)
    asyncio.run(thumbnails.prefetch([member.asset_id]))
    with library.db.transaction() as conn:   # the draft goes before its tile job runs
        library.remove_draft_in(conn, draft)
    assert member.asset_id not in library.retire_unwanted()  # the record is retired
    assert asyncio.run(runtime._executor.execute(job, 0)) == "terminal"
    _drop_thumbnail_jobs(registry.db)
    assert _pool_photo(library, 11)[0] == member.asset_id  # a new pool selects the same photo
    asyncio.run(thumbnails.prefetch([member.asset_id]))
    assert _thumbnail_jobs(registry.db) == 1  # the new ask is queued at once
    assert asyncio.run(runtime._executor.execute(job, 0)) == "ok"
    with TestClient(app) as client:
        served = client.get(_url(member.asset_id), headers=AUTH)
    assert served.status_code == 200 and served.headers["content-type"] == "image/jpeg"


@pytest.mark.parametrize("asset_id", ["asset-" + "e" * 64, "not-an-asset", "asset-" + "E" * 64])
def test_an_id_no_wanted_pool_selects_is_404_and_queues_nothing(route, registry, asset_id):
    app, _ = route
    _pool_photo(app.state.photo_library, 6)
    with TestClient(app) as client:
        response = client.get(_url(asset_id), headers=AUTH)  # no Sec-Fetch-Site (plain http)
    assert response.status_code == 404 and response.json() == {"error": "thumbnail_unknown"}
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    _nothing_queued(registry.db)


@pytest.mark.parametrize("site", ["cross-site", "same-site", "none"])
def test_a_cross_site_request_is_refused_before_servability(route, registry, site):
    app, _ = route
    member = SimpleNamespace(asset_id=_pool_photo(app.state.photo_library, 7)[0])
    with TestClient(app) as client:
        response = client.get(_url(member.asset_id), headers={**AUTH, "Sec-Fetch-Site": site})
    assert response.status_code == 403 and response.json() == {"error": "origin_mismatch"}
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    _nothing_queued(registry.db)


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer player-" + "z" * 40}])
def test_signed_out_and_player_credentials_get_one_401_for_any_id(route, registry, headers):
    app, _ = route
    member = SimpleNamespace(asset_id=_pool_photo(app.state.photo_library, 8)[0])
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
    member = SimpleNamespace(asset_id=_pool_photo(app.state.photo_library, 10)[0])

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


def test_a_pool_rows_upstream_id_is_a_canonical_uuid():
    # The stored identity the origin sends upstream is a validated UUID, never a path segment.
    values = dict(connection_ref="fixture", revision="1" * 40, kind="image", taken_at=1.0,
                  width=4, height=3)
    with pytest.raises(ValueError):
        LibraryAsset(upstream_id="../x", **values)
    upstream_id = LibraryAsset(upstream_id=str(uuid.UUID(int=1)), **values).upstream_id
    assert str(uuid.UUID(upstream_id)) == upstream_id
