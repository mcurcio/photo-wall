"""The content routes and the operator content routes over HTTP.

What is tested here is the HTTP layer: status codes, bodies and the wiring to the catalog.
`create_app(db=<stub>, content=...)`, where the content services are the real `ReleaseCatalog`
and `AssetReader` over the real records (PostgreSQL, the `registry` fixture),
`RecordingPublisher` and a `CacheStore` on `tmp_path`; the stub database only answers `/readyz`.
"""

from __future__ import annotations

import contextlib
import socket
import threading
from datetime import timedelta

import pytest
from fakes.publisher import RecordingPublisher
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from central.app import create_app
from central.assets.layout import CacheLayout
from central.assets.reader import AssetReader, WaiterSlots
from central.assets.store import CacheStore
from central.content_catalog.catalog import ReleaseCatalog
from central.content_wiring import ContentServices
from central.db import Database
from central.health.probe import PodProbe
from central.infra.asset_records import PgAssetRecords
from central.infra.catalog_records import PgReleaseRecords
from central.infra.transactions import PgTransactions
from central.kernel.job_types import Prefetch, SyncReleases
from contracts.equipment import equipment_device_id
from contracts.time import ManualClock

ADMIN = "content-routes-operator-" + "x" * 32
AUTH = {"Authorization": "Bearer " + ADMIN}
T1 = "v1.0.0"
SERIAL = "10000000c0ffee01"
DEVICE_ID = equipment_device_id("pi", SERIAL.encode())


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
    def __init__(self, registry, tmp_path) -> None:
        self.clock = ManualClock(1000.0)
        self.records = PgAssetRecords(self.clock)
        self.transactions = PgTransactions(registry.db)
        self.publisher = RecordingPublisher(self.clock, self.records, self.transactions)
        self.store = CacheStore(CacheLayout(tmp_path))
        self.catalog = ReleaseCatalog(releases=PgReleaseRecords(),
                                      transactions=self.transactions, publisher=self.publisher,
                                      clock=self.clock)
        self.reader = AssetReader(store=self.store, records=self.records,
                                  transactions=self.transactions, publisher=self.publisher,
                                  slots=WaiterSlots(4), clock=self.clock,
                                  wait_timeout=timedelta(seconds=5))
        self.db = StubDatabase()
        self.content = ContentServices(catalog=self.catalog, reader=self.reader,
                                       probe=PodProbe(self.db.healthy), feed=None)
        self.app = create_app(self.db, self.clock, ADMIN, content=self.content)


@pytest.fixture
def world(registry, tmp_path):
    return lambda: World(registry, tmp_path)


# -- the deleted V1 routes ---------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), [
    ("GET", "/v1/netboot/base"),
    ("GET", "/v1/netboot/manifest"),
    ("GET", "/v1/operator/netboot"),
    ("PUT", f"/v1/operator/devices/{DEVICE_ID}/pin"),
    ("DELETE", f"/v1/operator/devices/{DEVICE_ID}/pin"),
    ("POST", "/v1/player/base-health"),
    ("GET", "/v1/app/manifest"),
    ("GET", f"/v1/app/package/{'ab' * 32}.deb"),
    ("GET", "/v1/operator/app/releases"),
    ("POST", f"/v1/operator/app/releases/{T1}/promote"),
])
def test_no_v1_route_is_mounted(world, method, path):
    w = world()
    with TestClient(w.app) as client:
        response = client.request(method, path, headers=AUTH, json={"tag": T1})
    assert response.status_code == 404
    assert w.publisher.calls == []


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


def test_the_refresh_route_is_admin_gated(world):
    with TestClient(world().app) as client:
        assert client.post("/v1/operator/app/releases/refresh").status_code == 401


def test_the_hand_upload_routes_are_gone(world):
    with TestClient(world().app) as client:
        assert client.post("/v1/operator/app", headers=AUTH, json={
            "version": "1", "sha256": "ab" * 32, "size": 1}).status_code == 404
        assert client.put("/v1/operator/app/current", headers=AUTH,
                          json={"sha256": "ab" * 32}).status_code == 404


def test_refresh_publishes_a_sync(world):
    w = world()
    with TestClient(w.app) as client:
        response = client.post("/v1/operator/app/releases/refresh", headers=AUTH)
    assert response.status_code == 202 and response.json() == {"status": "polling"}
    assert SyncReleases() in w.publisher.inserted and Prefetch() in w.publisher.inserted
