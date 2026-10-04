"""The mounted F1/F3 seam serves only frozen offer bytes, with headers from stored facts."""

import base64
import hashlib
import os
from types import SimpleNamespace
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

import central.fleet.routes as routes
from central.assets.reader import Opened, Unavailable
from central.fleet.models import FleetError
from central.fleet.service import OfferAsset
from central.kernel.job_types import FetchPackage, FetchPlayerPayload
from contracts.time import ManualClock

OFFER_ID = UUID(int=1)
BOOT_ID = UUID(int=2)
BLOB = b"exact fleet app package"
DIGEST = hashlib.sha256(BLOB).hexdigest()


class Reader:
    def __init__(self, path, *, unavailable=None):
        self.path = path
        self.jobs = []
        self.unavailable = unavailable

    async def read(self, candidates):
        self.jobs.append(candidates.jobs[0])
        if self.unavailable is not None:
            return self.unavailable
        # The cache's recorded facts (the worker hashed the bytes when it filled the cache).
        return Opened(candidates.jobs[0], os.open(self.path, os.O_RDONLY), len(BLOB), DIGEST)


class Service:
    def __init__(self, _db, _clock):
        self.calls = []

    def create_offer(self, body):
        self.calls.append(("offer", body.serial))
        return {"schema": 1, "offer_id": str(OFFER_ID),
                "base": {"sha256": DIGEST, "size": len(BLOB)},
                "initial_app": None}

    def offer_asset(self, offer_id, kind):
        assert offer_id == OFFER_ID
        if kind == "app":
            raise FleetError("app_unconfigured", 503)
        return OfferAsset("base", "v1", DIGEST, DIGEST, len(BLOB))

    def record_check_in(self, body):
        self.calls.append(("check_in", body.observation_sequence))
        return {"accepted": True}

    def status(self):
        return {"devices": [], "commands_available": False}


def _client(monkeypatch, path, *, content=True, reader=None):
    monkeypatch.setattr(routes, "FleetService", Service)
    app = FastAPI()
    service = routes.mount_fleet_routes(
        app, db=None, clock=ManualClock(100), admin=lambda: None,
        content=SimpleNamespace(reader=reader or Reader(path)) if content else None,
    )
    return TestClient(app), service


def test_offer_preflight_then_exact_stream_with_digest_header(tmp_path, monkeypatch) -> None:
    path = tmp_path / "base.squashfs"
    path.write_bytes(BLOB)
    client, service = _client(monkeypatch, path)
    body = {"schema": 1, "kind": "pi", "serial": "abcdef1234567890",
            "kernel_boot_id": str(BOOT_ID), "boot_nonce": "a" * 32}
    response = client.post("/v1/netboot/offers", json=body)
    assert response.status_code == 200
    assert response.json()["offer_id"] == str(OFFER_ID)
    assert service.calls == [("offer", body["serial"])]
    result = client.get(f"/v1/netboot/offers/{OFFER_ID}/base")
    assert result.status_code == 200
    assert result.content == BLOB
    assert result.headers["content-length"] == str(len(BLOB))
    assert result.headers["digest"] == "sha-256=" + base64.b64encode(
        bytes.fromhex(DIGEST)).decode()
    assert client.get(f"/v1/netboot/offers/{OFFER_ID}/app").json() == {
        "error": "app_unconfigured"}


def test_missing_pod_bytes_do_not_reselect_or_claim_delivery(tmp_path, monkeypatch) -> None:
    path = tmp_path / "absent.squashfs"
    client, service = _client(monkeypatch, path, content=False)
    body = {"schema": 1, "kind": "pi", "serial": "abcdef1234567890",
            "kernel_boot_id": str(BOOT_ID), "boot_nonce": "a" * 32}
    response = client.post("/v1/netboot/offers", json=body)
    assert response.status_code == 503
    assert response.json() == {"error": "content_unavailable"}
    assert service.calls == [("offer", body["serial"])]


def test_payload_offer_uses_payload_cache_job_and_gzip_media(tmp_path, monkeypatch) -> None:
    path = tmp_path / "payload.tar.gz"
    path.write_bytes(BLOB)
    reader = Reader(path)

    class PayloadService(Service):
        def offer_asset(self, offer_id, kind):
            assert offer_id == OFFER_ID and kind == "app"
            return OfferAsset("app", "v1", DIGEST, DIGEST, len(BLOB),
                              "pw-player-data-v1")

    monkeypatch.setattr(routes, "FleetService", PayloadService)
    app = FastAPI()
    routes.mount_fleet_routes(app, db=None, clock=ManualClock(100), admin=lambda: None,
                              content=SimpleNamespace(reader=reader))
    response = TestClient(app).get(f"/v1/netboot/offers/{OFFER_ID}/app")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/gzip"
    assert isinstance(reader.jobs[0], FetchPlayerPayload)


def test_historical_app_offer_uses_package_cache_job(tmp_path, monkeypatch) -> None:
    path = tmp_path / "player.deb"
    path.write_bytes(BLOB)
    reader = Reader(path)

    class LegacyService(Service):
        def offer_asset(self, offer_id, kind):
            assert offer_id == OFFER_ID and kind == "app"
            return OfferAsset("app", "v1", DIGEST, DIGEST, len(BLOB))

    monkeypatch.setattr(routes, "FleetService", LegacyService)
    app = FastAPI()
    routes.mount_fleet_routes(app, db=None, clock=ManualClock(100), admin=lambda: None,
                              content=SimpleNamespace(reader=reader))
    response = TestClient(app).get(f"/v1/netboot/offers/{OFFER_ID}/app")
    assert response.status_code == 200
    assert isinstance(reader.jobs[0], FetchPackage)


def test_read_through_miss_is_503_with_the_readers_retry_after(tmp_path, monkeypatch) -> None:
    reader = Reader(tmp_path / "cold.squashfs", unavailable=Unavailable("timeout", 5))
    client, _ = _client(monkeypatch, tmp_path, reader=reader)
    response = client.get(f"/v1/netboot/offers/{OFFER_ID}/base")
    assert response.status_code == 503
    assert response.json() == {"error": "base_timeout"}
    assert response.headers["retry-after"] == "5"


def test_a_non_transient_503_carries_no_retry_after(tmp_path, monkeypatch) -> None:
    client, _ = _client(monkeypatch, tmp_path, content=False)
    response = client.get(f"/v1/netboot/offers/{OFFER_ID}/base")
    assert response.status_code == 503
    assert "retry-after" not in response.headers


def test_quota_429_retries_after_the_utc_day(tmp_path, monkeypatch) -> None:
    class QuotaService(Service):
        def offer_asset(self, offer_id, kind):
            raise FleetError("offer_quota_exhausted", 429)

    monkeypatch.setattr(routes, "FleetService", QuotaService)
    app = FastAPI()
    routes.mount_fleet_routes(app, db=None, clock=ManualClock(100), admin=lambda: None)
    response = TestClient(app).get(f"/v1/netboot/offers/{OFFER_ID}/base")
    assert response.status_code == 429
    assert response.headers["retry-after"] == str(86400 - 100)
