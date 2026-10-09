"""The content routes and the operator content routes over HTTP.

What is tested here is the HTTP layer: status codes, headers, bodies and the wiring to the
catalog and the reader. Selection, fallback and slot rules are the catalog's and the reader's
own suites. `create_app(db=<stub>, content=...)`, where the content services are the real
`ReleaseCatalog` and `AssetReader` over the real records (PostgreSQL, the `registry` fixture),
`RecordingPublisher` and a `CacheStore` on `tmp_path`. `RecordingPublisher.record_outcome` stands
in for the worker finishing a fetch; the stub database only answers `/readyz`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import socket
import threading
import time
from datetime import timedelta

import pytest
from content_db import Reads, put_file, seed_releases
from fakes.publisher import RecordingPublisher
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from central.app import create_app
from central.assets.layout import CacheLayout
from central.assets.reader import AssetReader, WaiterSlots
from central.assets.store import CacheStore
from central.content_catalog.catalog import ReleaseCatalog
from central.content_catalog.ports import ReleaseRow
from central.content_wiring import ContentServices
from central.db import Database
from central.health.probe import PodProbe
from central.infra.asset_records import PgAssetRecords
from central.infra.catalog_records import PgReleaseRecords
from central.infra.stored_assets import DiskStoredAssets
from central.infra.transactions import PgTransactions
from central.kernel.assets import AssetReady, AssetReference, OriginLocator
from central.kernel.job_types import FetchPackage, Prefetch, SyncReleases
from central.kernel.jobs import asset_key
from central.kernel.publishing import Failed, Ready
from contracts.equipment import equipment_device_id
from contracts.time import ManualClock

ADMIN = "content-routes-operator-" + "x" * 32
AUTH = {"Authorization": "Bearer " + ADMIN}
T1, T2 = "v1.0.0", "v2.0.0"
SERIAL = "10000000c0ffee01"
DEVICE_ID = equipment_device_id("pi", SERIAL.encode())


def image(tag: str) -> bytes:
    return f"squashfs of {tag} ".encode() * 64


def deb(tag: str) -> bytes:
    return f"player deb of {tag} ".encode() * 32


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def package_job(tag: str) -> FetchPackage:
    return FetchPackage(sha256=sha(deb(tag)))


def digest(data: bytes) -> str:
    return "sha-256=" + base64.b64encode(hashlib.sha256(data).digest()).decode()


def release(tag: str, *, package: bool = True, os_image: bool = True,
            pre: bool = False) -> ReleaseRow:
    return ReleaseRow(
        tag, pre,
        OriginLocator(f"https://example.test/{tag}.deb", sha(deb(tag)), len(deb(tag)))
        if package else None,
        OriginLocator(f"https://example.test/{tag}.tgz", sha(image(tag)), 64) if os_image else None,
    )


class StubDatabase:
    def __init__(self) -> None:
        self.reachable: bool | Exception = True

    def migrate(self) -> None:
        pass

    def healthy(self) -> bool:
        if isinstance(self.reachable, Exception):
            raise self.reachable
        return self.reachable


class World:
    def __init__(self, registry, tmp_path, *, releases=(), promoted=None, capacity=4,
                 wait: float = 5.0) -> None:
        self.clock = ManualClock(1000.0)
        self.records = PgAssetRecords(self.clock)
        self.transactions = PgTransactions(registry.db)
        self.reads = Reads(self.transactions)
        self.publisher = RecordingPublisher(self.clock, self.records, self.transactions)
        seed_releases(self.transactions, releases, promoted=promoted)
        self.store = CacheStore(CacheLayout(tmp_path))
        self.catalog = ReleaseCatalog(releases=PgReleaseRecords(),
                                      stored=DiskStoredAssets(records=self.records,
                                                              store=self.store),
                                      transactions=self.transactions, publisher=self.publisher,
                                      clock=self.clock)
        self.slots = WaiterSlots(capacity)
        self.reader = AssetReader(store=self.store, records=self.records,
                                  transactions=self.transactions, publisher=self.publisher,
                                  slots=self.slots, clock=self.clock,
                                  wait_timeout=timedelta(seconds=wait))
        self.db = StubDatabase()
        self.content = ContentServices(catalog=self.catalog, reader=self.reader,
                                       probe=PodProbe(self.db.healthy), feed=None)
        self.app = create_app(self.db, self.clock, ADMIN, content=self.content)

    def reference(self, job, *, owner: str) -> None:
        with self.transactions.begin() as tx:
            self.records.reference(tx, asset_key(job), AssetReference(
                owner, OriginLocator("https://example.test/x", asset_key(job).identity, None), None,
                None))

    def produce(self, job, data: bytes) -> AssetReady:
        """The worker's effect: the verified file on disk plus its produced facts."""
        facts = AssetReady(size=len(data), sha256=sha(data))
        put_file(self.store, asset_key(job), data)
        with self.transactions.begin() as tx:
            self.records.record_produced(tx, asset_key(job), facts)
        return facts



@pytest.fixture
def world(registry, tmp_path):
    return lambda **options: World(registry, tmp_path, **options)


def package(client, tag: str = T1):
    return client.get(f"/v1/app/package/{sha(deb(tag))}.deb")


def until(predicate, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


# -- the deleted V1 boot routes ----------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), [
    ("GET", "/v1/netboot/base"),
    ("GET", "/v1/netboot/manifest"),
    ("GET", "/v1/operator/netboot"),
    ("PUT", f"/v1/operator/devices/{DEVICE_ID}/pin"),
    ("DELETE", f"/v1/operator/devices/{DEVICE_ID}/pin"),
    ("POST", "/v1/player/base-health"),
])
def test_no_v1_boot_route_is_mounted(world, method, path):
    w = world(releases=[release(T1)], promoted=T1)
    with TestClient(w.app) as client:
        response = client.request(method, path, headers=AUTH, json={"tag": T1})
    assert response.status_code == 404
    assert w.publisher.calls == []


# -- GET /v1/app/package/{sha256}.deb --------------------------------------------------------------


def test_a_cached_package_streams_with_todays_headers(world):
    w = world(releases=[release(T1)])
    data = deb(T1)
    job = FetchPackage(sha256=sha(data))
    w.reference(job, owner=T1)
    w.produce(job, data)
    with TestClient(w.app) as client:
        response = client.get(f"/v1/app/package/{sha(data)}.deb")
    assert response.status_code == 200 and response.content == data
    assert response.headers["digest"] == digest(data)
    assert response.headers["content-type"] == "application/vnd.debian.binary-package"
    assert response.headers["cache-control"] == "public, immutable"
    assert response.headers["content-length"] == str(len(data))


@pytest.mark.parametrize("name", ["not-a-sha", "A" * 64, sha(b"unknown package")])
def test_a_malformed_or_unknown_package_is_404(world, name):
    w = world(releases=[release(T1)])
    with TestClient(w.app) as client:
        response = client.get(f"/v1/app/package/{name}.deb")
    assert response.status_code == 404
    assert response.json() == {"error": "app_package_not_found"}
    assert w.publisher.calls == []


def test_a_missing_package_is_503_with_retry_after(world):
    w = world(releases=[release(T1)], promoted=T1, wait=0.2)
    w.reference(package_job(T1), owner=T1)
    with TestClient(w.app) as client:
        response = package(client)
    assert response.status_code == 503
    assert response.json() == {"error": "app_timeout"}
    assert response.headers["retry-after"] == "5"
    assert [(c.job, c.retry_terminal) for c in w.publisher.calls] == [
        (package_job(T1), True)]  # the request path may retry a terminal failure
    assert w.slots.in_use == 0


def test_a_miss_is_served_once_the_fetch_lands(world):
    w = world(releases=[release(T1)], promoted=T1)
    job = package_job(T1)
    w.reference(job, owner=T1)
    with TestClient(w.app) as client:
        result = {}
        request = threading.Thread(target=lambda: result.update(response=package(client)))
        request.start()
        until(lambda: w.slots.in_use == 1)  # the request is waiting on the fetch's handle
        facts = w.produce(job, deb(T1))
        w.publisher.record_outcome(job, Ready(facts))
        request.join(10)
    response = result["response"]
    assert response.status_code == 200 and response.content == deb(T1)
    assert response.headers["digest"] == digest(deb(T1))


@pytest.mark.parametrize(("outcome", "code", "retry_after"), [
    (Failed(False, "origin_unreachable", timedelta(seconds=7)), "app_origin_unreachable", "7"),
    (Failed(True, "download_not_found", None), "app_download_not_found", "30"),
])
def test_a_failed_fetch_is_503_with_its_reason(world, outcome, code, retry_after):
    w = world(releases=[release(T1)], promoted=T1)
    job = package_job(T1)
    w.reference(job, owner=T1)
    with TestClient(w.app) as client:
        result = {}
        request = threading.Thread(target=lambda: result.update(response=package(client)))
        request.start()
        until(lambda: w.slots.in_use == 1)
        w.publisher.record_outcome(job, outcome)
        request.join(10)
    response = result["response"]
    assert response.status_code == 503
    assert response.json() == {"error": code}
    assert response.headers["retry-after"] == retry_after


def _asgi_get(path: str, headers: dict[str, str]):
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "GET", "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "server": ("testserver", 80), "client": ("127.0.0.1", 1234),
    }
    return scope


def test_a_disconnect_frees_the_waiter_slot_and_leaves_the_job(world):
    w = world(releases=[release(T1)], promoted=T1, wait=30)
    w.reference(package_job(T1), owner=T1)
    sent: list[dict] = []

    async def main() -> None:
        gone = asyncio.Event()
        requested = False

        async def receive():
            nonlocal requested
            if not requested:
                requested = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        call = asyncio.ensure_future(w.app(_asgi_get(f"/v1/app/package/{sha(deb(T1))}.deb", {}),
                                           receive, send))
        for _ in range(500):
            if w.slots.in_use == 1:
                break
            await asyncio.sleep(0.01)
        assert w.slots.in_use == 1
        gone.set()  # the client gives up long before the 30s deadline
        await asyncio.wait_for(call, 5)

    asyncio.run(main())
    assert w.slots.in_use == 0  # freed at once, not after 30s
    assert sent[0]["status"] == 499
    assert w.publisher.inserted == [package_job(T1)]  # the fetch is never cancelled


# -- manifests -------------------------------------------------------------------------------------


def test_the_app_manifest_is_the_promoted_package_without_its_bytes(world):
    # No longer gated on the `.deb` being present: the bytes route reads through the cache.
    w = world(releases=[release(T1)], promoted=T1)
    with TestClient(w.app) as client:
        response = client.get("/v1/app/manifest")
    assert response.status_code == 200
    assert response.json() == {"version": T1, "sha256": sha(deb(T1)), "size": len(deb(T1))}


def test_nothing_promoted_is_503_app_unconfigured(world):
    w = world(releases=[release(T1)])
    with TestClient(w.app) as client:
        response = client.get("/v1/app/manifest")
    assert response.status_code == 503
    assert response.json() == {"error": "app_unconfigured"}


# -- probes ----------------------------------------------------------------------------------------


def test_livez_needs_nothing_and_readyz_is_the_database(world):
    w = world()
    with TestClient(w.app) as client:
        assert client.get("/livez").json() == {"status": "ok"}
        assert client.get("/readyz").status_code == 200
        w.db.reachable = False
        assert client.get("/readyz").status_code == 503
        w.db.reachable = RuntimeError("secret dsn in message")
        response = client.get("/readyz")
        assert response.status_code == 503 and "secret" not in response.text
        assert client.get("/livez").status_code == 200


class TcpProxy:
    """A local TCP forwarder to PostgreSQL that can be cut, making the database unreachable."""

    def __init__(self, host: str, port: int) -> None:
        self._target = (host, port)
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        self._sockets: list[socket.socket] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self._server.accept()
            except OSError:
                return  # cut
            upstream = socket.create_connection(self._target)
            with self._lock:
                self._sockets += [client, upstream]
            for a, b in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pipe, args=(a, b), daemon=True).start()

    @staticmethod
    def _pipe(source: socket.socket, sink: socket.socket) -> None:
        try:
            while data := source.recv(65536):
                sink.sendall(data)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                sink.shutdown(socket.SHUT_RDWR)

    def cut(self) -> None:
        self._server.close()
        with self._lock:
            for sock in self._sockets:
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
                sock.close()


def test_readyz_follows_the_real_database_through_the_real_content_services(
        registry, tmp_path, monkeypatch):
    # `create_app` over a real `Database` builds the real content services itself
    # (`build_content_services` -> `PodProbe(db.healthy)`); nothing here is stubbed.
    target = conninfo_to_dict(registry.db.dsn)
    proxy = TcpProxy(target.get("host") or "127.0.0.1", int(target.get("port") or 5432))
    monkeypatch.setenv("PHOTO_WALL_CACHE_ROOT", str(tmp_path))
    db = Database(make_conninfo(registry.db.dsn, host="127.0.0.1", port=str(proxy.port)))
    app = create_app(db, ManualClock(1000.0), ADMIN, media_root=tmp_path / "media")
    try:
        with TestClient(app) as client:
            assert client.get("/readyz").status_code == 200
            proxy.cut()  # the database becomes unreachable
            response = client.get("/readyz")
            assert response.status_code == 503
            assert response.json() == {"status": "unavailable"}
            assert client.get("/livez").status_code == 200
    finally:
        proxy.cut()
        db.close()


# -- operator routes -------------------------------------------------------------------------------


def test_operator_content_routes_are_admin_gated(world):
    w = world(releases=[release(T1)])
    with TestClient(w.app) as client:
        for method, path in [("GET", "/v1/operator/app/releases"),
                             ("POST", f"/v1/operator/app/releases/{T1}/promote"),
                             ("POST", "/v1/operator/app/releases/refresh")]:
            assert client.request(method, path, json={"tag": T1}).status_code == 401, path


def test_the_hand_upload_routes_are_gone(world):
    w = world(releases=[release(T1)])
    with TestClient(w.app) as client:
        assert client.post("/v1/operator/app", headers=AUTH, json={
            "version": "1", "sha256": sha(b"x"), "size": 1}).status_code == 404
        assert client.put("/v1/operator/app/current", headers=AUTH,
                          json={"sha256": sha(b"x")}).status_code == 404


def test_releases_view_lists_semver_descending_with_who_promoted(world):
    w = world(releases=[release(T1), release(T2, os_image=False)], promoted=T1)
    with TestClient(w.app) as client:
        response = client.get("/v1/operator/app/releases", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == [
        {"tag": T2, "is_prerelease": False, "deployable": True, "promoted": False,
         "promoted_by": None, "has_os_image": False},
        {"tag": T1, "is_prerelease": False, "deployable": True, "promoted": True,
         "promoted_by": "operator", "has_os_image": True},
    ]


def test_promote_publishes_the_fetch_and_answers_promoted(world):
    w = world(releases=[release(T1)])
    with TestClient(w.app) as client:
        response = client.post(f"/v1/operator/app/releases/{T1}/promote", headers=AUTH)
    assert response.status_code == 200 and response.json() == {"status": "promoted"}
    assert w.reads.promoted() == T1
    assert FetchPackage(sha256=sha(deb(T1))) in w.publisher.inserted


@pytest.mark.parametrize(("tag", "status", "code"), [
    ("v9.9.9", 404, "release_not_found"),
    (T2, 409, "release_undeployable"),
    ("not-a-tag", 422, "invalid_tag"),
])
def test_promote_refusals_map_catalog_errors(world, tag, status, code):
    w = world(releases=[release(T1), release(T2, package=False)])
    with TestClient(w.app) as client:
        response = client.post(f"/v1/operator/app/releases/{tag}/promote", headers=AUTH)
    assert response.status_code == status
    assert response.json() == {"error": code}
    assert w.reads.promoted() is None


def test_refresh_publishes_a_sync(world):
    w = world()
    with TestClient(w.app) as client:
        response = client.post("/v1/operator/app/releases/refresh", headers=AUTH)
    assert response.status_code == 202 and response.json() == {"status": "polling"}
    assert SyncReleases() in w.publisher.inserted and Prefetch() in w.publisher.inserted
